package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"

	"powersystem/backend/internal/model"
	"powersystem/backend/internal/service"
)

// 预测质量模块的可调常量集中在此处，便于按现场节奏调整。
const (
	// qualityBackfillDays 是每轮回填的预测时间回看天数。
	qualityBackfillDays = 3
	// qualityEpochMS 是质量统计起点（毫秒）：早于此刻结束的预测不再回填。
	// 2026-11-02T07:20:00Z 切换模拟器故障形态，用它切断新旧数据混算；0 表示不限制。
	qualityEpochMS = int64(1_793_604_000_000)
	// qualityRefreshDays 是每轮重算日指标的天数，覆盖 3 天近期指标与 7 天漂移基线。
	qualityRefreshDays = 10
	// qualityBatchSize 是单批写入的行数，避免长事务。
	qualityBatchSize = 500
	// qualityMaxRows 是单轮回填的最大行数。
	qualityMaxRows = 20000
	// qualityFirstDelay 是 worker 首次执行前的等待，避开启动恢复与预测首轮。
	qualityFirstDelay = 90 * time.Second
	// qualityRoundBudget 是单轮回填与聚合的总预算。
	qualityRoundBudget = 5 * time.Minute
	// predictionWindowMS 是预测窗口长度：标签规则为 end < onset <= end + 1h。
	predictionWindowMS = int64(3_600_000)
	// calibrationBuckets 是概率校准的桶数，按 0.1 等宽划分。
	calibrationBuckets = 10
)

// 漂移阈值。L1 每小时评估最近 24 小时，L2/L3 每小时评估最近 3 个日指标。
const (
	driftL1ObserveWindow = 24 * time.Hour
	driftL1BaselineDays  = 7
	driftL1ProbShift     = 0.15
	driftL1HighRatioMin  = 0.05
	driftL1HighRatioMax  = 0.25
	driftRecentDays      = 3
	driftBaselineDays    = 7
	driftPrecisionDrop   = 0.10
	driftECEGap          = 0.05
	driftMinSamples      = 200
)

// pendingPrediction 是待标注的预测行，已带上台账里的设备与场站编码。
type pendingPrediction struct {
	ID           int64   `gorm:"column:id"`
	DeviceID     int64   `gorm:"column:device_id"`
	WindowEndMS  int64   `gorm:"column:window_end_ms"`
	Probability  float64 `gorm:"column:probability"`
	Threshold    float64 `gorm:"column:threshold"`
	ModelVersion string  `gorm:"column:model_version"`
	DeviceCode   string  `gorm:"column:device_code"`
	StationCode  string  `gorm:"column:station_code"`
}

// dailyBucketRow 是某日某个概率桶的聚合行，日指标与 ECE 都由它推导。
type dailyBucketRow struct {
	Day       string  `gorm:"column:day"`
	Bucket    int     `gorm:"column:bucket"`
	Samples   int64   `gorm:"column:samples"`
	TP        int64   `gorm:"column:tp"`
	FP        int64   `gorm:"column:fp"`
	FN        int64   `gorm:"column:fn"`
	TN        int64   `gorm:"column:tn"`
	ProbSum   float64 `gorm:"column:prob_sum"`
	HighCount int64   `gorm:"column:high_count"`
	Positives int64   `gorm:"column:positives"`
}

// calibrationBucket 是校准曲线的单个概率桶。空桶的比例用 null，
// 前端据此区分"还没有样本"与"0%"。
type calibrationBucket struct {
	Lower         float64  `json:"lower"`
	Upper         float64  `json:"upper"`
	Count         int64    `json:"count"`
	PredictedMean *float64 `json:"predicted_mean"`
	ActualRate    *float64 `json:"actual_rate"`
}

// qualityWorker 在预测持续写入之后做真值回填、日指标与漂移观察。
func (s *Server) qualityWorker(ctx context.Context) {
	first := time.NewTimer(qualityFirstDelay)
	defer first.Stop()
	ticker := time.NewTicker(time.Hour)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-first.C:
			s.qualityRound(ctx)
		case <-ticker.C:
			s.qualityRound(ctx)
		}
	}
}

// qualityRound 是单轮工作：先补真值再算指标，最后记录漂移。任一步失败只告警，不影响下一步。
func (s *Server) qualityRound(parent context.Context) {
	ctx, cancel := context.WithTimeout(parent, qualityRoundBudget)
	defer cancel()
	if err := s.backfillOutcomes(ctx, qualityBackfillDays); err != nil && ctx.Err() == nil {
		s.log.Warn("prediction_quality_backfill_failed", zap.Error(err))
	}
	if err := s.refreshDailyMetrics(ctx, qualityRefreshDays); err != nil && ctx.Err() == nil {
		s.log.Warn("prediction_quality_metrics_failed", zap.Error(err))
	}
	if err := s.detectDrift(ctx); err != nil && ctx.Err() == nil {
		s.log.Warn("prediction_quality_drift_failed", zap.Error(err))
	}
}

// backfillOutcomes 为最近 days 天内尚未标注的预测补写真值。
// 判定完全复用模拟器的确定性规则，不读遥测、不看告警。
func (s *Server) backfillOutcomes(ctx context.Context, days int) error {
	if days < 1 {
		days = qualityBackfillDays
	}
	now := time.Now()
	cutoff := now.Add(-time.Duration(days) * 24 * time.Hour).UnixMilli()
	if qualityEpochMS > cutoff {
		cutoff = qualityEpochMS
	}
	// 窗口右端未到期时不判定，否则会把还没发生的事写成"无故障"。
	closed := now.Add(-time.Duration(predictionWindowMS) * time.Millisecond).UnixMilli()
	var rows []pendingPrediction
	if err := s.db.WithContext(ctx).Raw(`SELECT p.id, p.device_id, p.window_end_ms, p.probability, p.threshold,
		p.model_version, d.device_code, d.station_code
		FROM prediction_records AS p
		JOIN devices AS d ON d.id = p.device_id
		WHERE p.window_end_ms >= ? AND p.window_end_ms <= ?
			AND NOT EXISTS (SELECT 1 FROM prediction_outcomes AS o WHERE o.prediction_id = p.id)
		ORDER BY p.window_end_ms ASC, p.id ASC
		LIMIT ?`, cutoff, closed, qualityMaxRows).Scan(&rows).Error; err != nil {
		return err
	}
	if len(rows) == 0 {
		return nil
	}
	labeledAt := time.Now().UTC()
	stations := newStationDeviceCache(s, ctx)
	outcomes := make([]model.PredictionOutcome, 0, qualityBatchSize)
	for _, row := range rows {
		if err := ctx.Err(); err != nil {
			return err
		}
		fault, onset, err := stations.windowFault(row)
		if err != nil {
			return err
		}
		outcomes = append(outcomes, model.PredictionOutcome{
			PredictionID:    row.ID,
			DeviceID:        row.DeviceID,
			DeviceCode:      row.DeviceCode,
			StationCode:     row.StationCode,
			WindowEndMS:     row.WindowEndMS,
			ModelVersion:    row.ModelVersion,
			Probability:     row.Probability,
			Threshold:       row.Threshold,
			PredictedHigh:   row.Probability >= row.Threshold,
			ActualFault:     fault,
			FaultOnsetMS:    onset,
			LabelSource:     "window_rule",
			LabelConfidence: "confirmed",
			LabeledAt:       labeledAt,
		})
		if len(outcomes) >= qualityBatchSize {
			if err := s.insertOutcomes(ctx, outcomes); err != nil {
				return err
			}
			outcomes = outcomes[:0]
		}
	}
	return s.insertOutcomes(ctx, outcomes)
}

// insertOutcomes 幂等写入：同一预测重复标注时保留首次结论。
func (s *Server) insertOutcomes(ctx context.Context, rows []model.PredictionOutcome) error {
	if len(rows) == 0 {
		return nil
	}
	return s.db.WithContext(ctx).
		Clauses(clause.OnConflict{Columns: []clause.Column{{Name: "prediction_id"}}, DoNothing: true}).
		CreateInBatches(&rows, qualityBatchSize).Error
}

// stationDeviceCache 缓存"某站某天被注入的设备编码"。
// 设备列表与模拟器保持一致：按 device_code 升序取当日生效的受控设备，
// 这样 SimulationDeviceIndex 选中的正是实际被注入的那一台。
type stationDeviceCache struct {
	srv      *Server
	ctx      context.Context
	devices  map[string][]string
	selected map[string]string
}

func newStationDeviceCache(s *Server, ctx context.Context) *stationDeviceCache {
	return &stationDeviceCache{srv: s, ctx: ctx, devices: map[string][]string{}, selected: map[string]string{}}
}

// selectedDevice 返回某站某日槽位选中的设备编码，空串表示该站没有可注入设备。
func (c *stationDeviceCache) selectedDevice(station string, day time.Time) (string, error) {
	key := station + "|" + day.Format("2006-01-02")
	if code, ok := c.selected[key]; ok {
		return code, nil
	}
	codes, ok := c.devices[station]
	if !ok {
		codes = make([]string, 0, 32)
		if err := c.srv.db.WithContext(c.ctx).Table("simulation_alarm_policy_devices").
			Where("station_code = ? AND active = ?", station, true).
			Order("device_code ASC").
			Pluck("device_code", &codes).Error; err != nil {
			return "", err
		}
		c.devices[station] = codes
	}
	code := ""
	if len(codes) > 0 {
		code = codes[service.SimulationDeviceIndex(day, len(codes))]
	}
	c.selected[key] = code
	return code, nil
}

// windowFault 判定预测窗口 (window_end, window_end + 1h] 内该设备是否被注入越线。
// 规则与训练标签一致：只在槽位起始时刻落点，窗口左开右闭。
func (c *stationDeviceCache) windowFault(row pendingPrediction) (bool, *int64, error) {
	winEnd := row.WindowEndMS + predictionWindowMS
	day := service.SimulationDay(time.UnixMilli(row.WindowEndMS))
	last := service.SimulationDay(time.UnixMilli(winEnd))
	for !day.After(last) {
		for _, plan := range service.SimulationSchedule(day) {
			if plan.StationCode != row.StationCode {
				continue
			}
			start := service.SimulationScheduledAt(plan.Day, plan.Hour).UnixMilli()
			if start <= row.WindowEndMS || start > winEnd {
				continue
			}
			code, err := c.selectedDevice(plan.StationCode, plan.Day)
			if err != nil {
				return false, nil, err
			}
			if code == row.DeviceCode {
				onset := start
				return true, &onset, nil
			}
		}
		day = day.AddDate(0, 0, 1)
	}
	return false, nil, nil
}

// refreshDailyMetrics 按上海日历日重算混淆矩阵、校准误差与均值，并按 day upsert。
func (s *Server) refreshDailyMetrics(ctx context.Context, days int) error {
	if days < 1 {
		days = qualityRefreshDays
	}
	start := service.SimulationDay(time.Now()).AddDate(0, 0, -(days - 1))
	startMS := start.UnixMilli()
	var buckets []dailyBucketRow
	if err := s.db.WithContext(ctx).Raw(`SELECT to_char((to_timestamp(window_end_ms/1000.0) AT TIME ZONE 'Asia/Shanghai')::date, 'YYYY-MM-DD') AS day,
		LEAST(floor(probability*10),9)::int AS bucket,
		count(*) AS samples,
		count(*) FILTER (WHERE predicted_high AND actual_fault) AS tp,
		count(*) FILTER (WHERE predicted_high AND NOT actual_fault) AS fp,
		count(*) FILTER (WHERE NOT predicted_high AND actual_fault) AS fn,
		count(*) FILTER (WHERE NOT predicted_high AND NOT actual_fault) AS tn,
		sum(probability) AS prob_sum,
		count(*) FILTER (WHERE predicted_high) AS high_count,
		count(*) FILTER (WHERE actual_fault) AS positives
		FROM prediction_outcomes
		WHERE window_end_ms >= ?
		GROUP BY 1, 2
		ORDER BY 1, 2`, startMS).Scan(&buckets).Error; err != nil {
		return err
	}
	if len(buckets) == 0 {
		return nil
	}
	versions, err := s.dailyModelVersions(ctx, startMS)
	if err != nil {
		return err
	}
	type dailyAggregate struct {
		samples   int64
		tp        int64
		fp        int64
		fn        int64
		tn        int64
		probSum   float64
		high      int64
		positives int64
		ece       float64
	}
	aggregates := make(map[string]*dailyAggregate, len(versions))
	order := make([]string, 0, len(versions))
	for _, row := range buckets {
		agg, ok := aggregates[row.Day]
		if !ok {
			agg = &dailyAggregate{}
			aggregates[row.Day] = agg
			order = append(order, row.Day)
		}
		agg.samples += row.Samples
		agg.tp += row.TP
		agg.fp += row.FP
		agg.fn += row.FN
		agg.tn += row.TN
		agg.probSum += row.ProbSum
		agg.high += row.HighCount
		agg.positives += row.Positives
	}
	// ECE 需要当日总量，故在第二轮按桶累加。
	for _, row := range buckets {
		agg := aggregates[row.Day]
		if agg.samples == 0 || row.Samples == 0 {
			continue
		}
		predicted := row.ProbSum / float64(row.Samples)
		actual := float64(row.Positives) / float64(row.Samples)
		agg.ece += math.Abs(predicted-actual) * float64(row.Samples) / float64(agg.samples)
	}
	dailies := make([]model.PredictionQualityDaily, 0, len(order))
	for _, key := range order {
		day, err := time.Parse("2006-01-02", key)
		if err != nil {
			return err
		}
		agg := aggregates[key]
		if agg.samples == 0 {
			continue
		}
		probMean := agg.probSum / float64(agg.samples)
		highRatio := float64(agg.high) / float64(agg.samples)
		ece := agg.ece
		dailies = append(dailies, model.PredictionQualityDaily{
			Day:          day.UTC(),
			ModelVersion: versions[key],
			Samples:      int(agg.samples),
			TP:           int(agg.tp),
			FP:           int(agg.fp),
			FN:           int(agg.fn),
			TN:           int(agg.tn),
			Precision:    ratio(agg.tp, agg.tp+agg.fp),
			Recall:       ratio(agg.tp, agg.tp+agg.fn),
			F1:           f1Score(agg.tp, agg.fp, agg.fn),
			Accuracy:     ratio(agg.tp+agg.tn, agg.samples),
			ECE:          &ece,
			ProbMean:     &probMean,
			HighRatio:    &highRatio,
			UpdatedAt:    time.Now().UTC(),
		})
	}
	if len(dailies) == 0 {
		return nil
	}
	return s.db.WithContext(ctx).Clauses(clause.OnConflict{
		Columns: []clause.Column{{Name: "day"}},
		DoUpdates: clause.AssignmentColumns([]string{"model_version", "samples", "tp", "fp", "fn", "tn",
			"precision", "recall", "f1", "accuracy", "ece", "prob_mean", "high_ratio", "updated_at"}),
	}).Create(&dailies).Error
}

// dailyModelVersions 取每日出现次数最多的模型版本，次数相同时取字典序最小者以保证稳定。
func (s *Server) dailyModelVersions(ctx context.Context, startMS int64) (map[string]string, error) {
	var rows []struct {
		Day          string `gorm:"column:day"`
		ModelVersion string `gorm:"column:model_version"`
		Count        int64  `gorm:"column:count"`
	}
	if err := s.db.WithContext(ctx).Raw(`SELECT to_char((to_timestamp(window_end_ms/1000.0) AT TIME ZONE 'Asia/Shanghai')::date, 'YYYY-MM-DD') AS day,
		model_version, count(*) AS count
		FROM prediction_outcomes
		WHERE window_end_ms >= ?
		GROUP BY 1, 2`, startMS).Scan(&rows).Error; err != nil {
		return nil, err
	}
	versions := make(map[string]string, len(rows))
	counts := make(map[string]int64, len(rows))
	for _, row := range rows {
		best, ok := versions[row.Day]
		if !ok || row.Count > counts[row.Day] || (row.Count == counts[row.Day] && row.ModelVersion < best) {
			versions[row.Day] = row.ModelVersion
			counts[row.Day] = row.Count
		}
	}
	return versions, nil
}

func ratio(numerator, denominator int64) *float64 {
	if denominator <= 0 {
		return nil
	}
	value := float64(numerator) / float64(denominator)
	return &value
}

func f1Score(tp, fp, fn int64) *float64 {
	if tp <= 0 {
		value := 0.0
		return &value
	}
	precision := float64(tp) / float64(tp+fp)
	recall := float64(tp) / float64(tp+fn)
	if precision+recall <= 0 {
		value := 0.0
		return &value
	}
	value := 2 * precision * recall / (precision + recall)
	return &value
}

// qualityWindow 解析上海日历日区间，返回 [start, end) 的毫秒边界。
// from/to 既接受 YYYY-MM-DD，也接受前端直接传的 RFC3339 时间戳；
// 时间戳按上海日历日取整，避免跨时区把窗口错切一天。
func qualityWindow(c *gin.Context) (int64, int64, bool) {
	today := service.SimulationDay(time.Now())
	to := today
	if v := c.Query("to"); v != "" {
		parsed, err := parseQualityDay(v)
		if err != nil {
			fail(c, 400, 40001, "to requires YYYY-MM-DD")
			return 0, 0, false
		}
		to = parsed
	}
	from := to.AddDate(0, 0, -6)
	if v := c.Query("from"); v != "" {
		parsed, err := parseQualityDay(v)
		if err != nil {
			fail(c, 400, 40001, "from requires YYYY-MM-DD")
			return 0, 0, false
		}
		from = parsed
	}
	if from.After(to) || to.Sub(from) > 366*24*time.Hour {
		fail(c, 400, 40001, "invalid quality window")
		return 0, 0, false
	}
	return from.UnixMilli(), to.AddDate(0, 0, 1).UnixMilli(), true
}

// parseQualityDay 归一化到上海日历日的零点。
func parseQualityDay(value string) (time.Time, error) {
	if day, err := time.ParseInLocation("2006-01-02", value, service.SimulationLocation()); err == nil {
		return day, nil
	}
	instant, err := time.Parse(time.RFC3339, value)
	if err != nil {
		return time.Time{}, err
	}
	return service.SimulationDay(instant), nil
}

// qualityFilter 把场站与模型版本过滤条件拼进 where，返回条件与参数。
func qualityFilter(c *gin.Context, where string, args []any) (string, []any) {
	if station := c.Query("station_code"); station != "" {
		where += " AND station_code = ?"
		args = append(args, station)
	}
	if version := c.Query("model_version"); version != "" {
		where += " AND model_version = ?"
		args = append(args, version)
	}
	return where, args
}

// qualityDays 解析 days 参数，默认 30 天，上限 365 天。
func qualityDays(c *gin.Context) (int, bool) {
	days, err := strconv.Atoi(c.DefaultQuery("days", "30"))
	if err != nil || days < 1 || days > 365 {
		fail(c, 400, 40001, "invalid days")
		return 0, false
	}
	return days, true
}

// calibrationQuery 返回概率分桶聚合与 ECE。没有样本时 ECE 为 null。
func (s *Server) calibrationQuery(ctx context.Context, where string, args []any) ([]calibrationBucket, *float64, error) {
	var rows []struct {
		Bucket    int     `gorm:"column:bucket"`
		Count     int64   `gorm:"column:count"`
		ProbSum   float64 `gorm:"column:prob_sum"`
		Positives int64   `gorm:"column:positives"`
	}
	if err := s.db.WithContext(ctx).Raw(`SELECT LEAST(floor(probability*10),9)::int AS bucket,
		count(*) AS count, sum(probability) AS prob_sum,
		count(*) FILTER (WHERE actual_fault) AS positives
		FROM prediction_outcomes WHERE `+where+` GROUP BY 1`, args...).Scan(&rows).Error; err != nil {
		return nil, nil, err
	}
	buckets := make([]calibrationBucket, calibrationBuckets)
	for i := 0; i < calibrationBuckets; i++ {
		buckets[i] = calibrationBucket{Lower: float64(i) / 10, Upper: float64(i+1) / 10}
	}
	var total int64
	ece := 0.0
	for _, row := range rows {
		if row.Bucket < 0 || row.Bucket >= calibrationBuckets || row.Count <= 0 {
			continue
		}
		buckets[row.Bucket].Count += row.Count
		buckets[row.Bucket].PredictedMean = ptrFloat(row.ProbSum / float64(row.Count))
		buckets[row.Bucket].ActualRate = ptrFloat(float64(row.Positives) / float64(row.Count))
		total += row.Count
	}
	if total == 0 {
		return buckets, nil, nil
	}
	for _, row := range rows {
		if row.Bucket < 0 || row.Bucket >= calibrationBuckets || row.Count <= 0 {
			continue
		}
		predicted := row.ProbSum / float64(row.Count)
		actual := float64(row.Positives) / float64(row.Count)
		ece += math.Abs(predicted-actual) * float64(row.Count) / float64(total)
	}
	return buckets, ptrFloat(ece), nil
}

// predictionQualitySummary 汇总区间内的混淆矩阵与校准指标。
func (s *Server) predictionQualitySummary(c *gin.Context) {
	start, end, valid := qualityWindow(c)
	if !valid {
		return
	}
	where, args := qualityFilter(c, "window_end_ms >= ? AND window_end_ms < ?", []any{start, end})
	var agg struct {
		Samples   int64   `gorm:"column:samples"`
		TP        int64   `gorm:"column:tp"`
		FP        int64   `gorm:"column:fp"`
		FN        int64   `gorm:"column:fn"`
		TN        int64   `gorm:"column:tn"`
		ProbMean  float64 `gorm:"column:prob_mean"`
		HighRatio float64 `gorm:"column:high_ratio"`
	}
	err := s.db.WithContext(c.Request.Context()).Raw(`SELECT count(*) AS samples,
		count(*) FILTER (WHERE predicted_high AND actual_fault) AS tp,
		count(*) FILTER (WHERE predicted_high AND NOT actual_fault) AS fp,
		count(*) FILTER (WHERE NOT predicted_high AND actual_fault) AS fn,
		count(*) FILTER (WHERE NOT predicted_high AND NOT actual_fault) AS tn,
		coalesce(avg(probability),0) AS prob_mean,
		coalesce(avg(CASE WHEN predicted_high THEN 1.0 ELSE 0.0 END),0) AS high_ratio
		FROM prediction_outcomes WHERE `+where, args...).Scan(&agg).Error
	if err != nil {
		fail(c, 503, 50000, "prediction quality query failed")
		return
	}
	_, ece, err := s.calibrationQuery(c.Request.Context(), where, args)
	if err != nil {
		fail(c, 503, 50000, "prediction quality query failed")
		return
	}
	var version *string
	if v := c.Query("model_version"); v != "" {
		version = &v
	} else if agg.Samples > 0 {
		var row struct {
			ModelVersion string `gorm:"column:model_version"`
		}
		err := s.db.WithContext(c.Request.Context()).Raw(`SELECT model_version FROM prediction_outcomes
			WHERE `+where+` GROUP BY model_version ORDER BY count(*) DESC, model_version ASC LIMIT 1`, args...).Scan(&row).Error
		if err != nil {
			fail(c, 503, 50000, "prediction quality query failed")
			return
		}
		if row.ModelVersion != "" {
			version = &row.ModelVersion
		}
	}
	var probMean, highRatio *float64
	if agg.Samples > 0 {
		probMean = ptrFloat(agg.ProbMean)
		highRatio = ptrFloat(agg.HighRatio)
	}
	ok(c, gin.H{
		"samples": agg.Samples, "tp": agg.TP, "fp": agg.FP, "fn": agg.FN, "tn": agg.TN,
		"precision": ratio(agg.TP, agg.TP+agg.FP), "recall": ratio(agg.TP, agg.TP+agg.FN),
		"f1": f1Score(agg.TP, agg.FP, agg.FN), "accuracy": ratio(agg.TP+agg.TN, agg.Samples),
		"ece": ece, "prob_mean": probMean, "high_ratio": highRatio, "model_version": version,
	})
}

// predictionQualitySeries 返回按日的质量曲线。
func (s *Server) predictionQualitySeries(c *gin.Context) {
	days, valid := qualityDays(c)
	if !valid {
		return
	}
	end := service.SimulationDay(time.Now()).AddDate(0, 0, 1)
	start := end.AddDate(0, 0, -days)
	var rows []model.PredictionQualityDaily
	err := s.db.WithContext(c.Request.Context()).
		Where("day >= ? AND day < ?", dateOnly(start), dateOnly(end)).
		Order("day ASC").Find(&rows).Error
	if err != nil {
		fail(c, 503, 50000, "prediction quality query failed")
		return
	}
	points := make([]gin.H, 0, len(rows))
	for _, row := range rows {
		points = append(points, gin.H{
			"day": row.Day.Format("2006-01-02"), "samples": row.Samples,
			"precision": row.Precision, "recall": row.Recall,
			"f1": row.F1, "accuracy": row.Accuracy,
			"ece": row.ECE, "prob_mean": row.ProbMean,
		})
	}
	ok(c, gin.H{"points": points})
}

// predictionQualityCalibration 返回最近 days 天的可靠性曲线。
func (s *Server) predictionQualityCalibration(c *gin.Context) {
	days, valid := qualityDays(c)
	if !valid {
		return
	}
	start := service.SimulationDay(time.Now()).AddDate(0, 0, -(days - 1))
	buckets, ece, err := s.calibrationQuery(c.Request.Context(),
		"window_end_ms >= ?", []any{start.UnixMilli()})
	if err != nil {
		fail(c, 503, 50000, "prediction quality query failed")
		return
	}
	ok(c, gin.H{"buckets": buckets, "ece": ece})
}

// qualityModelItem 是模型台账的对外字段，避免把内部列暴露给前端。
type qualityModelItem struct {
	ModelVersion    string          `json:"model_version"`
	Status          string          `json:"status"`
	Threshold       *float64        `json:"threshold"`
	DataSource      *string         `json:"data_source"`
	TrainSamples    *int            `json:"train_samples"`
	PositiveSamples *int            `json:"positive_samples"`
	Metrics         json.RawMessage `json:"metrics"`
	BaselineMetrics json.RawMessage `json:"baseline_metrics"`
	TrainedAt       *time.Time      `json:"trained_at"`
	PromotedAt      *time.Time      `json:"promoted_at"`
	PromotedBy      *string         `json:"promoted_by"`
	RejectedReason  *string         `json:"rejected_reason"`
	ArtifactPath    *string         `json:"artifact_path"`
}

// runtimeModelVersion 读取推理服务实际加载的模型版本。
// 台账提升只改登记状态、不替换模型文件，两者可能长期不一致；
// 列表里一并返回，前端据此提示，避免「台账 A、运行 B」被静默忽略。
// 任何失败都返回空串：这是辅助信息，不能让它拖垮列表接口。
func (s *Server) runtimeModelVersion(ctx context.Context) string {
	if s.cfg.AIURL == "" {
		return ""
	}
	reqCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, strings.TrimRight(s.cfg.AIURL, "/")+"/health", nil)
	if err != nil {
		return ""
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return ""
	}
	var payload struct {
		ModelVersion string `json:"model_version"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return ""
	}
	return payload.ModelVersion
}

// predictionQualityModels 列出模型台账，活动版本与最近训练版本在前。
func (s *Server) predictionQualityModels(c *gin.Context) {
	var rows []model.ModelRegistry
	err := s.db.WithContext(c.Request.Context()).
		Order("promoted_at DESC NULLS LAST, created_at DESC").Find(&rows).Error
	if err != nil {
		fail(c, 503, 50000, "model registry query failed")
		return
	}
	items := make([]qualityModelItem, 0, len(rows))
	for _, row := range rows {
		items = append(items, qualityModelItem{
			ModelVersion: row.ModelVersion, Status: row.Status, Threshold: row.Threshold,
			DataSource: row.DataSource, TrainSamples: row.TrainSamples, PositiveSamples: row.PositiveSamples,
			Metrics: row.Metrics, BaselineMetrics: row.BaselineMetrics,
			TrainedAt: row.TrainedAt, PromotedAt: row.PromotedAt, PromotedBy: row.PromotedBy,
			RejectedReason: row.RejectedReason, ArtifactPath: row.ArtifactPath,
		})
	}
	ok(c, gin.H{"items": items, "runtime_version": s.runtimeModelVersion(c.Request.Context())})
}

// qualityModelImportRequest 是训练作业 retrain-report.json 的提交体。
// 训练在容器外执行，本接口只负责把候选登记进台账，不读模型文件、不碰推理服务。
type qualityModelImportRequest struct {
	ModelVersion    string          `json:"model_version"`
	BaselineVersion string          `json:"baseline_version"`
	DataSource      string          `json:"data_source"`
	Threshold       *float64        `json:"threshold"`
	Calibration     json.RawMessage `json:"calibration"`
	Metrics         json.RawMessage `json:"metrics"`
	BaselineMetrics json.RawMessage `json:"baseline_metrics"`
	TrainSamples    *int            `json:"train_samples"`
	PositiveSamples *int            `json:"positive_samples"`
	ArtifactPath    string          `json:"artifact_path"`
	TrainedAt       string          `json:"trained_at"`
	Eligible        bool            `json:"eligible"`
	RejectionReason string          `json:"rejection_reason"`
	Status          string          `json:"status"`
}

// 重训门槛：天数与样本两个维度都要过。只看样本量会让模型背时刻表——
// 当前一天只有 5 个窗口，样本再多也只是同一批钟点。
const (
	trainRequiredDays      = 10
	trainRequiredSamples   = 5000
	trainRequiredPositives = 50
)

type qualityTrainEligibility struct {
	Days              int64    `json:"days"`
	Samples           int64    `json:"samples"`
	Positives         int64    `json:"positives"`
	RequiredDays      int      `json:"required_days"`
	RequiredSamples   int      `json:"required_samples"`
	RequiredPositives int      `json:"required_positives"`
	Ready             bool     `json:"ready"`
	Blockers          []string `json:"blockers"`
}

// qualityEligibility 统计新形态（qualityEpochMS 之后）的可用样本。
// 只算已结算且有标签的行，未到期的预测不能进训练集。
func (s *Server) qualityEligibility(ctx context.Context) (qualityTrainEligibility, error) {
	var row struct {
		Days      int64
		Samples   int64
		Positives int64
	}
	err := s.db.WithContext(ctx).Raw(`
		SELECT count(DISTINCT date_trunc('day', to_timestamp(window_end_ms/1000) AT TIME ZONE 'Asia/Shanghai')) AS days,
		       count(*) AS samples,
		       COALESCE(sum(CASE WHEN actual_fault THEN 1 ELSE 0 END), 0) AS positives
		FROM prediction_outcomes
		WHERE window_end_ms >= ?`, qualityEpochMS).Scan(&row).Error
	if err != nil {
		return qualityTrainEligibility{}, err
	}
	result := qualityTrainEligibility{
		Days: row.Days, Samples: row.Samples, Positives: row.Positives,
		RequiredDays: trainRequiredDays, RequiredSamples: trainRequiredSamples, RequiredPositives: trainRequiredPositives,
	}
	if result.Days < int64(trainRequiredDays) {
		result.Blockers = append(result.Blockers, fmt.Sprintf("新形态数据仅 %d 天，需 %d 天", result.Days, trainRequiredDays))
	}
	if result.Samples < int64(trainRequiredSamples) {
		result.Blockers = append(result.Blockers, fmt.Sprintf("样本 %d 条，需 %d 条", result.Samples, trainRequiredSamples))
	}
	if result.Positives < int64(trainRequiredPositives) {
		result.Blockers = append(result.Blockers, fmt.Sprintf("正样本 %d 个，需 %d 个", result.Positives, trainRequiredPositives))
	}
	result.Ready = len(result.Blockers) == 0
	return result, nil
}

// predictionQualityTrainEligibility 供页面预判重训按钮是否可点。
func (s *Server) predictionQualityTrainEligibility(c *gin.Context) {
	result, err := s.qualityEligibility(c.Request.Context())
	if err != nil {
		fail(c, 503, 50000, "eligibility query failed")
		return
	}
	ok(c, result)
}

type trainerResponse struct {
	OK              bool            `json:"ok"`
	RunID           string          `json:"run_id"`
	Eligible        bool            `json:"eligible"`
	RejectionReason string          `json:"rejection_reason"`
	ArtifactPath    string          `json:"artifact_path"`
	Seconds         float64         `json:"seconds"`
	Log             string          `json:"log"`
	Report          json.RawMessage `json:"report"`
}

type retrainReport struct {
	ModelVersion    string          `json:"model_version"`
	Threshold       *float64        `json:"threshold"`
	Metrics         json.RawMessage `json:"metrics"`
	TrainSamples    *int            `json:"train_samples"`
	PositiveSamples *int            `json:"positive_samples"`
	TrainedAt       string          `json:"trained_at"`
	Eligible        bool            `json:"eligible"`
}

// predictionQualityTrain 触发一次重训；过准入的候选自动登记进台账。
// 训练本身在 ai-trainer 容器里跑，本接口只做准入校验、转发与登记。
func (s *Server) predictionQualityTrain(c *gin.Context) {
	ctx := c.Request.Context()
	eligibility, err := s.qualityEligibility(ctx)
	if err != nil {
		fail(c, 503, 50000, "eligibility query failed")
		return
	}
	if !eligibility.Ready {
		fail(c, 409, 40902, strings.Join(eligibility.Blockers, "；"))
		return
	}
	if strings.TrimSpace(s.cfg.TrainerURL) == "" {
		fail(c, 503, 50000, "trainer not configured")
		return
	}
	body := strings.NewReader(`{"days":120}`)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(s.cfg.TrainerURL, "/")+"/train", body)
	if err != nil {
		fail(c, 500, 50000, "trainer request build failed")
		return
	}
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 35 * time.Minute}
	resp, err := client.Do(req)
	if err != nil {
		fail(c, 502, 50201, "trainer unreachable: "+err.Error())
		return
	}
	defer resp.Body.Close()
	var payload trainerResponse
	decoderErr := json.NewDecoder(resp.Body).Decode(&payload)
	if resp.StatusCode != http.StatusOK || decoderErr != nil {
		fail(c, 502, 50201, "trainer failed: "+strings.TrimSpace(payload.Log))
		return
	}

	var report retrainReport
	if len(payload.Report) > 0 {
		if err := json.Unmarshal(payload.Report, &report); err != nil {
			fail(c, 502, 50201, "trainer report invalid")
			return
		}
	}
	if !payload.Eligible {
		ok(c, gin.H{"eligible": false, "rejection_reason": payload.RejectionReason, "run_id": payload.RunID})
		return
	}

	now := time.Now().UTC()
	if report.TrainedAt != "" {
		if parsed, err := time.Parse(time.RFC3339, report.TrainedAt); err == nil {
			now = parsed
		}
	}
	var existing model.ModelRegistry
	lookupErr := s.db.WithContext(ctx).Where("model_version = ?", report.ModelVersion).First(&existing).Error
	if lookupErr == nil {
		if existing.Status == "active" {
			fail(c, 409, 40901, "该版本已是现役，不覆盖")
			return
		}
	} else if !errors.Is(lookupErr, gorm.ErrRecordNotFound) {
		fail(c, 503, 50000, "model registry query failed")
		return
	}
	artifact := payload.ArtifactPath
	source := "synthetic_window"
	record := model.ModelRegistry{
		ModelVersion: report.ModelVersion, Algorithm: "xgboost", Status: "candidate",
		Threshold: report.Threshold, Metrics: report.Metrics,
		DataSource: &source, TrainSamples: report.TrainSamples, PositiveSamples: report.PositiveSamples,
		ArtifactPath: &artifact, TrainedAt: &now,
	}
	if len(report.Metrics) == 0 || string(report.Metrics) == "null" {
		record.Metrics = nil
	}
	if err := s.db.WithContext(ctx).Clauses(clause.OnConflict{
		Columns:   []clause.Column{{Name: "model_version"}},
		DoUpdates: clause.AssignmentColumns([]string{"status", "threshold", "metrics", "data_source", "train_samples", "positive_samples", "artifact_path", "trained_at"}),
	}).Create(&record).Error; err != nil {
		fail(c, 503, 50000, "model register failed")
		return
	}
	ok(c, gin.H{
		"eligible": true, "run_id": payload.RunID, "seconds": payload.Seconds,
		"model_version": report.ModelVersion, "artifact_path": artifact,
		"train_samples": report.TrainSamples, "positive_samples": report.PositiveSamples,
	})
}

// predictionQualityImportModel 登记一个模型版本。
// 默认登记为 candidate；eligible=false 时记为 rejected 并保留原因。
// 仅当台账里没有任何 active 版本时，允许直接登记 status=active（首次接入现役模型）。
func (s *Server) predictionQualityImportModel(c *gin.Context) {
	var body qualityModelImportRequest
	if err := c.ShouldBindJSON(&body); err != nil {
		fail(c, 400, 40001, "invalid model payload")
		return
	}
	if body.ModelVersion == "" || len(body.ModelVersion) > 64 {
		fail(c, 400, 40001, "invalid model version")
		return
	}
	ctx := c.Request.Context()

	status := "candidate"
	if !body.Eligible {
		status = "rejected"
	}
	if body.Status != "" {
		if body.Status != "candidate" && body.Status != "active" {
			fail(c, 400, 40001, "invalid model status")
			return
		}
		status = body.Status
	}

	record := model.ModelRegistry{
		ModelVersion: body.ModelVersion, Algorithm: "xgboost", Status: status,
		Threshold: body.Threshold, Metrics: body.Metrics, BaselineMetrics: body.BaselineMetrics,
		DataSource: nullableString(body.DataSource), TrainSamples: body.TrainSamples,
		PositiveSamples: body.PositiveSamples, ArtifactPath: nullableString(body.ArtifactPath),
	}
	if len(body.Calibration) > 0 && string(body.Calibration) != "null" {
		record.Calibration = body.Calibration
	}
	if len(body.Metrics) == 0 || string(body.Metrics) == "null" {
		record.Metrics = nil
	}
	if len(body.BaselineMetrics) == 0 || string(body.BaselineMetrics) == "null" {
		record.BaselineMetrics = nil
	}
	if body.RejectionReason != "" {
		record.RejectedReason = &body.RejectionReason
	} else if status == "rejected" {
		record.RejectedReason = nullableString("not_eligible")
	}
	if body.TrainedAt != "" {
		if parsed, err := time.Parse(time.RFC3339, body.TrainedAt); err == nil {
			record.TrainedAt = &parsed
		}
	}
	if record.TrainedAt == nil {
		now := time.Now().UTC()
		record.TrainedAt = &now
	}

	var existing model.ModelRegistry
	err := s.db.WithContext(ctx).Where("model_version = ?", body.ModelVersion).First(&existing).Error
	if err != nil && !errors.Is(err, gorm.ErrRecordNotFound) {
		fail(c, 503, 50000, "model import failed")
		return
	}
	if err == nil {
		if existing.Status == "active" {
			fail(c, 409, 40901, "active model cannot be overwritten")
			return
		}
		record.ID = existing.ID
		if err := s.db.WithContext(ctx).Model(&model.ModelRegistry{}).Where("id = ?", existing.ID).
			Updates(map[string]any{
				"algorithm": record.Algorithm, "status": record.Status, "threshold": record.Threshold,
				"calibration": record.Calibration, "metrics": record.Metrics,
				"baseline_metrics": record.BaselineMetrics, "data_source": record.DataSource,
				"train_samples": record.TrainSamples, "positive_samples": record.PositiveSamples,
				"artifact_path": record.ArtifactPath, "trained_at": record.TrainedAt,
				"rejected_reason": record.RejectedReason,
			}).Error; err != nil {
			fail(c, 503, 50000, "model import failed")
			return
		}
		ok(c, gin.H{"model_version": record.ModelVersion, "status": record.Status, "updated": true})
		return
	}

	if record.Status == "active" {
		var activeCount int64
		if err := s.db.WithContext(ctx).Model(&model.ModelRegistry{}).
			Where("status = ?", "active").Count(&activeCount).Error; err != nil {
			fail(c, 503, 50000, "model import failed")
			return
		}
		if activeCount > 0 {
			fail(c, 409, 40901, "an active model already exists, promote instead")
			return
		}
		now := time.Now().UTC()
		record.PromotedAt = &now
	}
	if err := s.db.WithContext(ctx).Create(&record).Error; err != nil {
		fail(c, 503, 50000, "model import failed")
		return
	}
	ok(c, gin.H{"model_version": record.ModelVersion, "status": record.Status, "updated": false})
}

// nullableString 把空串转成 NULL，避免台账里出现无意义的空字段。
func nullableString(value string) *string {
	if value == "" {
		return nil
	}
	return &value
}

// predictionQualityPromote 只做数据库状态流转。真实模型切换（容器、镜像、模型文件）
// 由运维按发布流程执行，本接口不触碰运行中的推理服务。
func (s *Server) predictionQualityPromote(c *gin.Context) {
	version := c.Param("version")
	if version == "" || len(version) > 64 {
		fail(c, 400, 40001, "invalid model version")
		return
	}
	ctx := c.Request.Context()
	var actor struct {
		Username string `gorm:"column:username"`
	}
	if err := s.db.WithContext(ctx).Table("users").Select("username").
		Where("id = ?", c.GetInt64("user_id")).Scan(&actor).Error; err != nil {
		fail(c, 503, 50000, "model promote failed")
		return
	}
	username := actor.Username
	conflict := errors.New("model version is not a candidate")
	now := time.Now().UTC()
	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var target model.ModelRegistry
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).
			Where("model_version = ?", version).First(&target).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				return conflict
			}
			return err
		}
		if target.Status != "candidate" {
			return conflict
		}
		if err := tx.Model(&model.ModelRegistry{}).Where("status = ?", "active").
			Update("status", "retired").Error; err != nil {
			return err
		}
		return tx.Model(&model.ModelRegistry{}).Where("id = ?", target.ID).
			Updates(map[string]any{"status": "active", "promoted_at": now, "promoted_by": username}).Error
	})
	if errors.Is(err, conflict) {
		fail(c, 409, 40901, "model version is not a candidate")
		return
	}
	if err != nil {
		fail(c, 503, 50000, "model promote failed")
		return
	}
	ok(c, gin.H{"model_version": version, "status": "active", "promoted_at": now, "promoted_by": username})
}

// trialHours 是试用观察期。期满前不允许确认，避免刚切过去就下结论。
const trialHours = 24

// reloadModel 让推理服务加载指定版本。接口内部会切 current 软链，
// 顺序是「先加载成功、再切软链」，失败时软链与内存都停在旧版本。
func (s *Server) reloadModel(ctx context.Context, version string) error {
	payload := strings.NewReader(fmt.Sprintf(`{"version":%q}`, version))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		strings.TrimRight(s.cfg.AIURL, "/")+"/admin/reload", payload)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := (&http.Client{Timeout: 30 * time.Second}).Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("reload %d: %s", resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	return nil
}

// predictionQualityTrial 把候选切到试用：先 reload，成功后才改台账。
// reload 失败时台账不动，页面会显示「切换失败，已保留原模型」。
func (s *Server) predictionQualityTrial(c *gin.Context) {
	version := c.Param("version")
	if version == "" || len(version) > 64 {
		fail(c, 400, 40001, "invalid model version")
		return
	}
	ctx := c.Request.Context()
	var target model.ModelRegistry
	if err := s.db.WithContext(ctx).Where("model_version = ?", version).First(&target).Error; err != nil {
		fail(c, 404, 40401, "model version not found")
		return
	}
	if target.Status != "candidate" {
		fail(c, 409, 40901, "只有候选版本可以试用")
		return
	}
	previous := ""
	var active model.ModelRegistry
	if err := s.db.WithContext(ctx).Where("status = ?", "active").First(&active).Error; err == nil {
		previous = active.ModelVersion
	}
	if err := s.reloadModel(ctx, version); err != nil {
		fail(c, 502, 50201, "切换失败，已保留原模型："+err.Error())
		return
	}
	now := time.Now().UTC()
	if err := s.db.WithContext(ctx).Model(&model.ModelRegistry{}).Where("id = ?", target.ID).
		Updates(map[string]any{
			"status": "trial", "trial_started_at": now, "previous_version": nullableString(previous),
		}).Error; err != nil {
		fail(c, 503, 50000, "model trial failed")
		return
	}
	ok(c, gin.H{
		"model_version": version, "status": "trial",
		"trial_started_at": now, "previous_version": previous,
	})
}

// predictionQualityConfirm 试用期满后转正；未满 24 小时直接拒绝并给出剩余时间。
func (s *Server) predictionQualityConfirm(c *gin.Context) {
	version := c.Param("version")
	ctx := c.Request.Context()
	var target model.ModelRegistry
	if err := s.db.WithContext(ctx).Where("model_version = ?", version).First(&target).Error; err != nil {
		fail(c, 404, 40401, "model version not found")
		return
	}
	if target.Status != "trial" || target.TrialStartedAt == nil {
		fail(c, 409, 40901, "只有试用中的版本可以确认")
		return
	}
	elapsed := time.Since(*target.TrialStartedAt)
	if elapsed < trialHours*time.Hour {
		fail(c, 409, 40902, fmt.Sprintf("试用未满 %d 小时，还需 %.1f 小时", trialHours, (trialHours*time.Hour-elapsed).Hours()))
		return
	}
	var actor struct {
		Username string `gorm:"column:username"`
	}
	_ = s.db.WithContext(ctx).Table("users").Select("username").
		Where("id = ?", c.GetInt64("user_id")).Scan(&actor).Error
	now := time.Now().UTC()
	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if err := tx.Model(&model.ModelRegistry{}).Where("status = ?", "active").
			Update("status", "retired").Error; err != nil {
			return err
		}
		return tx.Model(&model.ModelRegistry{}).Where("id = ?", target.ID).
			Updates(map[string]any{"status": "active", "promoted_at": now, "promoted_by": actor.Username}).Error
	})
	if err != nil {
		fail(c, 503, 50000, "model confirm failed")
		return
	}
	ok(c, gin.H{"model_version": version, "status": "active", "promoted_at": now})
}

// predictionQualityRollback 把试用版本退回上一版：reload 回 previous 成功后才改台账。
func (s *Server) predictionQualityRollback(c *gin.Context) {
	version := c.Param("version")
	ctx := c.Request.Context()
	var target model.ModelRegistry
	if err := s.db.WithContext(ctx).Where("model_version = ?", version).First(&target).Error; err != nil {
		fail(c, 404, 40401, "model version not found")
		return
	}
	if target.Status != "trial" {
		fail(c, 409, 40901, "只有试用中的版本可以回退")
		return
	}
	if target.PreviousVersion == nil || *target.PreviousVersion == "" {
		fail(c, 409, 40901, "没有可回退的版本")
		return
	}
	previous := *target.PreviousVersion
	if err := s.reloadModel(ctx, previous); err != nil {
		fail(c, 502, 50201, "回退失败，当前模型未改动："+err.Error())
		return
	}
	if err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if err := tx.Model(&model.ModelRegistry{}).Where("model_version = ?", previous).
			Update("status", "active").Error; err != nil {
			return err
		}
		return tx.Model(&model.ModelRegistry{}).Where("id = ?", target.ID).
			Updates(map[string]any{"status": "candidate", "trial_started_at": nil}).Error
	}); err != nil {
		fail(c, 503, 50000, "model rollback failed")
		return
	}
	ok(c, gin.H{"model_version": version, "status": "candidate", "rolled_back_to": previous})
}

// predictionQualityDrift 返回最近的漂移事件，只读事实，不触发训练。
func (s *Server) predictionQualityDrift(c *gin.Context) {
	limit, err := strconv.Atoi(c.DefaultQuery("limit", "50"))
	if err != nil || limit < 1 || limit > 500 {
		fail(c, 400, 40001, "invalid limit")
		return
	}
	var rows []model.PredictionDriftEvent
	if err := s.db.WithContext(c.Request.Context()).
		Order("detected_at DESC, id DESC").Limit(limit).Find(&rows).Error; err != nil {
		fail(c, 503, 50000, "drift query failed")
		return
	}
	items := make([]gin.H, 0, len(rows))
	for _, row := range rows {
		items = append(items, gin.H{
			"id": row.ID, "detected_at": row.DetectedAt, "level": row.Level, "signal": row.Signal,
			"observed": row.Observed, "baseline": row.Baseline, "threshold": row.Threshold,
			"action": row.Action,
		})
	}
	ok(c, gin.H{"items": items})
}

// dateOnly 把上海日历日转成 UTC 零点的日期值。date 参数不携带时区，
// 统一按 UTC 零点表示，避免驱动按本地时区把日期整体挪前一天。
func dateOnly(t time.Time) time.Time {
	return time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC)
}

// detectDrift 记录漂移观察。本轮只写事件，不自动训练、不改阈值；
// 是否重训由人工或外部流水线决定。
func (s *Server) detectDrift(ctx context.Context) error {
	if err := s.detectDistributionDrift(ctx); err != nil {
		return err
	}
	return s.detectMetricDrift(ctx)
}

// detectDistributionDrift 是 L1 信号：最近 24 小时的预测均值相对基线偏移，
// 或高风险占比越出经验区间。基线取此前 7 天的日均概率均值。
func (s *Server) detectDistributionDrift(ctx context.Context) error {
	now := time.Now()
	recentStart := now.Add(-driftL1ObserveWindow)
	baselineStart := recentStart.AddDate(0, 0, -driftL1BaselineDays)
	var recent struct {
		ProbMean  float64 `gorm:"column:prob_mean"`
		HighRatio float64 `gorm:"column:high_ratio"`
		Samples   int64   `gorm:"column:samples"`
	}
	if err := s.db.WithContext(ctx).Raw(`SELECT coalesce(avg(probability),0) AS prob_mean,
		coalesce(avg(CASE WHEN predicted_high THEN 1.0 ELSE 0.0 END),0) AS high_ratio,
		count(*) AS samples
		FROM prediction_outcomes WHERE window_end_ms >= ?`, recentStart.UnixMilli()).
		Scan(&recent).Error; err != nil {
		return err
	}
	if recent.Samples == 0 {
		return nil
	}
	var baseline struct {
		ProbMean float64 `gorm:"column:prob_mean"`
		Days     int64   `gorm:"column:days"`
	}
	if err := s.db.WithContext(ctx).Raw(`SELECT coalesce(avg(daily),0) AS prob_mean, count(*) AS days
		FROM (SELECT (to_timestamp(window_end_ms/1000.0) AT TIME ZONE 'Asia/Shanghai')::date AS day,
			avg(probability) AS daily
			FROM prediction_outcomes WHERE window_end_ms >= ? AND window_end_ms < ?
			GROUP BY 1) AS t`, baselineStart.UnixMilli(), recentStart.UnixMilli()).
		Scan(&baseline).Error; err != nil {
		return err
	}
	if baseline.Days > 0 && baseline.ProbMean > 0 {
		shift := math.Abs(recent.ProbMean-baseline.ProbMean) / baseline.ProbMean
		if shift > driftL1ProbShift {
			if err := s.recordDrift(ctx, model.PredictionDriftEvent{
				Level: "L1", Signal: "probability", Observed: &recent.ProbMean,
				Baseline: &baseline.ProbMean, Threshold: ptrFloat(driftL1ProbShift), Action: "watch",
				Detail: detailJSON(gin.H{"shift_ratio": shift, "samples": recent.Samples,
					"window_hours": int(driftL1ObserveWindow / time.Hour), "baseline_days": baseline.Days}),
			}); err != nil {
				return err
			}
		}
	}
	if recent.HighRatio < driftL1HighRatioMin || recent.HighRatio > driftL1HighRatioMax {
		if err := s.recordDrift(ctx, model.PredictionDriftEvent{
			Level: "L1", Signal: "high_ratio", Observed: &recent.HighRatio,
			Threshold: ptrFloat(driftL1HighRatioMax), Action: "watch",
			Detail: detailJSON(gin.H{"lower": driftL1HighRatioMin, "upper": driftL1HighRatioMax,
				"samples": recent.Samples, "window_hours": int(driftL1ObserveWindow / time.Hour)}),
		}); err != nil {
			return err
		}
	}
	return nil
}

// detectMetricDrift 是 L2/L3 信号：最近 3 个日指标同时劣于此前 7 天基线。
func (s *Server) detectMetricDrift(ctx context.Context) error {
	today := service.SimulationDay(time.Now())
	recentStart := today.AddDate(0, 0, -(driftRecentDays - 1))
	baselineEnd := recentStart
	baselineStart := baselineEnd.AddDate(0, 0, -driftBaselineDays)
	var recent []struct {
		Samples   int      `gorm:"column:samples"`
		Precision *float64 `gorm:"column:precision"`
		ECE       *float64 `gorm:"column:ece"`
	}
	if err := s.db.WithContext(ctx).Raw(`SELECT samples, precision, ece FROM prediction_quality_daily
		WHERE day >= ? AND day < ? ORDER BY day ASC`, dateOnly(recentStart), dateOnly(today.AddDate(0, 0, 1))).
		Scan(&recent).Error; err != nil {
		return err
	}
	if len(recent) != driftRecentDays {
		return nil
	}
	var baseline struct {
		Precision float64 `gorm:"column:precision"`
		ECE       float64 `gorm:"column:ece"`
		Days      int64   `gorm:"column:days"`
	}
	if err := s.db.WithContext(ctx).Raw(`SELECT coalesce(avg(precision),0) AS precision,
		coalesce(avg(ece),0) AS ece, count(*) AS days
		FROM prediction_quality_daily WHERE day >= ? AND day < ?`, dateOnly(baselineStart), dateOnly(baselineEnd)).
		Scan(&baseline).Error; err != nil {
		return err
	}
	if baseline.Days == 0 {
		return nil
	}
	// precision 连续三天低于基线 10 个百分点，属可复现的召回/精度退化。
	precisionDrop := baseline.Precision - driftPrecisionDrop
	lowPrecision := true
	for _, row := range recent {
		if row.Precision == nil || row.Samples < driftMinSamples || *row.Precision >= precisionDrop {
			lowPrecision = false
			break
		}
	}
	if lowPrecision {
		observed := *recent[len(recent)-1].Precision
		if err := s.recordDrift(ctx, model.PredictionDriftEvent{
			Level: "L3", Signal: "precision", Observed: &observed,
			Baseline: &baseline.Precision, Threshold: &precisionDrop, Action: "triggered",
			Detail: detailJSON(gin.H{"recent_days": driftRecentDays, "baseline_days": baseline.Days,
				"min_samples": driftMinSamples}),
		}); err != nil {
			return err
		}
	}
	// ECE 连续三天高于基线 0.05 以上，属校准漂移。
	eceGap := baseline.ECE + driftECEGap
	highECE := true
	for _, row := range recent {
		if row.ECE == nil || *row.ECE <= eceGap {
			highECE = false
			break
		}
	}
	if highECE {
		observed := *recent[len(recent)-1].ECE
		if err := s.recordDrift(ctx, model.PredictionDriftEvent{
			Level: "L2", Signal: "ece", Observed: &observed,
			Baseline: &baseline.ECE, Threshold: &eceGap, Action: "triggered",
			Detail: detailJSON(gin.H{"recent_days": driftRecentDays, "baseline_days": baseline.Days}),
		}); err != nil {
			return err
		}
	}
	return nil
}

// recordDrift 同一天同一 level+signal 只落一条，避免每小时重复刷屏。
func (s *Server) recordDrift(ctx context.Context, event model.PredictionDriftEvent) error {
	day := service.SimulationDay(time.Now())
	var row struct {
		Present bool `gorm:"column:present"`
	}
	if err := s.db.WithContext(ctx).Raw(`SELECT EXISTS(SELECT 1 FROM prediction_drift_events
		WHERE level = ? AND signal = ? AND detected_at >= ? AND detected_at < ?) AS present`,
		event.Level, event.Signal, day.UTC(), day.AddDate(0, 0, 1).UTC()).Scan(&row).Error; err != nil {
		return err
	}
	if row.Present {
		return nil
	}
	event.DetectedAt = time.Now().UTC()
	return s.db.WithContext(ctx).Create(&event).Error
}

func ptrFloat(value float64) *float64 { return &value }

func detailJSON(value gin.H) json.RawMessage {
	raw, err := json.Marshal(value)
	if err != nil {
		return nil
	}
	return raw
}
