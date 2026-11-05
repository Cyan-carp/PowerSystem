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
	// qualityEpochMS 是质量统计起点（毫秒）：早于此刻结束的预测不再回填，也不进入新口径统计。
	// 2026-11-02T07:20:00Z 切换模拟器故障形态，用它切断新旧数据混算。
	qualityEpochMS = model.PredictionQualityEpochMS
	// qualityLabelRule 是当前真值规则的版本标记。旧 2 小时口径行保持 legacy，
	// 只有显式修正或按新规则写入的行才带 window_1h，避免旧数据冒作新口径。
	qualityLabelRule = model.PredictionLabelRule
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
	// predictionWindowMS 是预测窗口长度：标签规则为 window_end < onset <= window_end + 1h。
	// 与训练和真值服务共用同一常量，不再接受前兆容差。
	predictionWindowMS = service.PredictionHorizonMS
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
// 带上 model_version，日指标只取当日样本最多的版本，避免同日多版本混算后
// 又用一个版本名标注。
type dailyBucketRow struct {
	Day          string  `gorm:"column:day"`
	ModelVersion string  `gorm:"column:model_version"`
	Bucket       int     `gorm:"column:bucket"`
	Samples      int64   `gorm:"column:samples"`
	TP           int64   `gorm:"column:tp"`
	FP           int64   `gorm:"column:fp"`
	FN           int64   `gorm:"column:fn"`
	TN           int64   `gorm:"column:tn"`
	ProbSum      float64 `gorm:"column:prob_sum"`
	HighCount    int64   `gorm:"column:high_count"`
	Positives    int64   `gorm:"column:positives"`
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
	stations := service.NewStationDeviceCache(s.db, ctx)
	outcomes := make([]model.PredictionOutcome, 0, qualityBatchSize)
	for _, row := range rows {
		if err := ctx.Err(); err != nil {
			return err
		}
		fault, onset, err := stations.WindowFault(row.DeviceCode, row.StationCode, row.WindowEndMS)
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
			LabelRule:       qualityLabelRule,
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

// refreshDailyMetrics 按上海日历日重算混淆矩阵、校准误差与均值，并按 day upsert。
// 只统计 1 小时口径且已成熟的标签；日聚合在修正后由下一轮 worker 覆盖。
func (s *Server) refreshDailyMetrics(ctx context.Context, days int) error {
	if days < 1 {
		days = qualityRefreshDays
	}
	now := time.Now()
	start := service.SimulationDay(now).AddDate(0, 0, -(days - 1))
	startMS := start.UnixMilli()
	if startMS < qualityEpochMS {
		startMS = qualityEpochMS
	}
	closed := now.Add(-time.Duration(predictionWindowMS) * time.Millisecond).UnixMilli()
	var buckets []dailyBucketRow
	if err := s.db.WithContext(ctx).Raw(`SELECT to_char((to_timestamp(window_end_ms/1000.0) AT TIME ZONE 'Asia/Shanghai')::date, 'YYYY-MM-DD') AS day,
		model_version,
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
		WHERE window_end_ms >= ? AND window_end_ms <= ? AND label_rule = ?
		GROUP BY 1, 2, 3
		ORDER BY 1, 2, 3`, startMS, closed, qualityLabelRule).Scan(&buckets).Error; err != nil {
		return err
	}
	if len(buckets) == 0 {
		return nil
	}
	// 先按日+版本算样本量，选出当日主导版本；后面只累计该版本，避免
	// “数值是 A+B 混算、版本名却写 A”的旧问题。
	type dayVersion struct{ day, version string }
	counts := make(map[dayVersion]int64)
	for _, row := range buckets {
		counts[dayVersion{row.Day, row.ModelVersion}] += row.Samples
	}
	dominant := make(map[string]string)
	best := make(map[string]int64)
	for key, count := range counts {
		if best[key.day] == 0 || count > best[key.day] ||
			(count == best[key.day] && key.version < dominant[key.day]) {
			best[key.day] = count
			dominant[key.day] = key.version
		}
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
	aggregates := make(map[string]*dailyAggregate, len(dominant))
	order := make([]string, 0, len(dominant))
	for _, row := range buckets {
		if row.ModelVersion != dominant[row.Day] {
			continue
		}
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
		if row.ModelVersion != dominant[row.Day] {
			continue
		}
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
			LabelRule:    qualityLabelRule,
			ModelVersion: dominant[key],
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
		Columns: []clause.Column{{Name: "day"}, {Name: "label_rule"}},
		DoUpdates: clause.AssignmentColumns([]string{"model_version", "samples", "tp", "fp", "fn", "tn",
			"precision", "recall", "f1", "accuracy", "ece", "prob_mean", "high_ratio", "updated_at"}),
	}).Create(&dailies).Error
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

// qualityStationFilter 只拼场站过滤。模型版本由调用方显式指定，
// 或按统计区间内的主导版本收窄，避免“显示 A 版本、指标混算 B 版本”。
func qualityStationFilter(c *gin.Context, where string, args []any) (string, []any) {
	if station := c.Query("station_code"); station != "" {
		where += " AND station_code = ?"
		args = append(args, station)
	}
	return where, args
}

// qualityVersionFilter 解析显式模型版本；未指定时用区间内样本最多的版本。
// 返回空串表示区间内没有可用样本，此时不追加版本条件（结果也会是 0 样本）。
func (s *Server) qualityVersionFilter(ctx context.Context, c *gin.Context, where string, args []any) (string, string, []any, error) {
	version := c.Query("model_version")
	if version != "" {
		return where + " AND model_version = ?", version, append(args, version), nil
	}
	var row struct {
		ModelVersion string `gorm:"column:model_version"`
	}
	if err := s.db.WithContext(ctx).Raw(`SELECT model_version FROM prediction_outcomes
		WHERE `+where+` GROUP BY model_version ORDER BY count(*) DESC, model_version ASC LIMIT 1`, args...).
		Scan(&row).Error; err != nil {
		return "", "", nil, err
	}
	if row.ModelVersion == "" {
		return where, "", args, nil
	}
	return where + " AND model_version = ?", row.ModelVersion, append(args, row.ModelVersion), nil
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

// batchStat 是「按故障批次」的口径：同一台设备同一次故障起点算一批，
// 只要起点之前任意一条预测窗口报过高，就算这批被提前叫出来。
//
// 为什么需要它：窗口级 precision/recall 的分母里包含大量「离故障还有一两个小时」
// 的窗口，那些窗口本就该是低分，却被算成漏报，于是页面长期显示「命中率 3%」，
// 与实际「有没有提前报警」不是一回事。运维关心的是后者。
type batchStat struct {
	Total   int64   `gorm:"column:total"`
	Hit     int64   `gorm:"column:hit"`
	AvgLead float64 `gorm:"column:avg_lead"`
	P50Lead float64 `gorm:"column:p50_lead"`
}

func (s *Server) batchStats(ctx context.Context, where string, args []any) (batchStat, error) {
	var row batchStat
	err := s.db.WithContext(ctx).Raw(`SELECT count(*) AS total,
		count(*) FILTER (WHERE hit) AS hit,
		coalesce(avg(CASE WHEN hit THEN lead_minutes END), 0) AS avg_lead,
		coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY CASE WHEN hit THEN lead_minutes END), 0) AS p50_lead
		FROM (
			SELECT device_id, fault_onset_ms,
				bool_or(predicted_high) AS hit,
				(max(fault_onset_ms) - min(window_end_ms) FILTER (WHERE predicted_high)) / 60000.0 AS lead_minutes
			FROM prediction_outcomes
			WHERE `+where+` AND actual_fault AND fault_onset_ms IS NOT NULL
			GROUP BY device_id, fault_onset_ms
		) AS b`, args...).Scan(&row).Error
	return row, err
}

// predictionQualitySummary 汇总区间内的混淆矩阵与校准指标。
// 只统计已修正/新写入的 1 小时标签，且只统计已成熟的窗口；默认收窄到区间内主导模型版本。
func (s *Server) predictionQualitySummary(c *gin.Context) {
	start, end, valid := qualityWindow(c)
	if !valid {
		return
	}
	if start < qualityEpochMS {
		start = qualityEpochMS
	}
	closed := time.Now().Add(-time.Duration(predictionWindowMS) * time.Millisecond).UnixMilli()
	where, args := qualityStationFilter(c,
		"window_end_ms >= ? AND window_end_ms < ? AND window_end_ms <= ? AND label_rule = ?",
		[]any{start, end, closed, qualityLabelRule})
	where, version, args, err := s.qualityVersionFilter(c.Request.Context(), c, where, args)
	if err != nil {
		fail(c, 503, 50000, "prediction quality query failed")
		return
	}
	var agg struct {
		Samples   int64   `gorm:"column:samples"`
		TP        int64   `gorm:"column:tp"`
		FP        int64   `gorm:"column:fp"`
		FN        int64   `gorm:"column:fn"`
		TN        int64   `gorm:"column:tn"`
		ProbMean  float64 `gorm:"column:prob_mean"`
		HighRatio float64 `gorm:"column:high_ratio"`
	}
	err = s.db.WithContext(c.Request.Context()).Raw(`SELECT count(*) AS samples,
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
	var probMean, highRatio *float64
	if agg.Samples > 0 {
		probMean = ptrFloat(agg.ProbMean)
		highRatio = ptrFloat(agg.HighRatio)
	}
	batchWhere, batchArgs := qualityStationFilter(c,
		"fault_onset_ms >= ? AND fault_onset_ms < ? AND label_rule = ?",
		[]any{start, end, qualityLabelRule})
	if version != "" {
		batchWhere += " AND model_version = ?"
		batchArgs = append(batchArgs, version)
	}
	batch, err := s.batchStats(c.Request.Context(), batchWhere, batchArgs)
	if err != nil {
		fail(c, 503, 50000, "prediction quality query failed")
		return
	}
	var versionPtr *string
	if version != "" {
		versionPtr = &version
	}
	ok(c, gin.H{
		"samples": agg.Samples, "tp": agg.TP, "fp": agg.FP, "fn": agg.FN, "tn": agg.TN,
		"precision": ratio(agg.TP, agg.TP+agg.FP), "recall": ratio(agg.TP, agg.TP+agg.FN),
		"f1": f1Score(agg.TP, agg.FP, agg.FN), "accuracy": ratio(agg.TP+agg.TN, agg.Samples),
		"ece": ece, "prob_mean": probMean, "high_ratio": highRatio, "model_version": versionPtr,
		"batch": gin.H{
			"total": batch.Total, "hit": batch.Hit,
			"rate":             ratio(batch.Hit, batch.Total),
			"avg_lead_minutes": math.Round(batch.AvgLead),
			"p50_lead_minutes": math.Round(batch.P50Lead),
		},
	})
}

// predictionQualitySeries 返回按日的质量曲线。
// 批次命中只取修正后的 1 小时标签，且只取已成熟窗口；默认收窄到区间内的主导模型版本。
func (s *Server) predictionQualitySeries(c *gin.Context) {
	days, valid := qualityDays(c)
	if !valid {
		return
	}
	now := time.Now()
	end := service.SimulationDay(now).AddDate(0, 0, 1)
	start := end.AddDate(0, 0, -days)
	if start.UnixMilli() < qualityEpochMS {
		start = service.SimulationDay(time.UnixMilli(qualityEpochMS))
	}
	startMS := start.UnixMilli()
	if startMS < qualityEpochMS {
		startMS = qualityEpochMS
	}
	closed := now.Add(-time.Duration(predictionWindowMS) * time.Millisecond).UnixMilli()
	where, args := qualityStationFilter(c,
		"window_end_ms >= ? AND window_end_ms <= ? AND label_rule = ?",
		[]any{startMS, closed, qualityLabelRule})
	where, version, args, err := s.qualityVersionFilter(c.Request.Context(), c, where, args)
	if err != nil {
		fail(c, 503, 50000, "prediction quality query failed")
		return
	}
	daily := s.db.WithContext(c.Request.Context()).
		Where("day >= ? AND day < ? AND label_rule = ?", dateOnly(start), dateOnly(end), qualityLabelRule)
	if version != "" {
		daily = daily.Where("model_version = ?", version)
	}
	var rows []model.PredictionQualityDaily
	if err := daily.Order("day ASC").Find(&rows).Error; err != nil {
		fail(c, 503, 50000, "prediction quality query failed")
		return
	}
	// 按故障批次（设备+起点）汇总到天。批次按“故障起点落在区间内”归属，
	// 而不是按预测窗口终点归属，避免跨日边界把首日清晨的批次切掉。
	var batchRows []struct {
		Day   string `gorm:"column:day"`
		Total int64  `gorm:"column:total"`
		Hit   int64  `gorm:"column:hit"`
	}
	batchWhere, batchArgs := qualityStationFilter(c,
		"fault_onset_ms >= ? AND fault_onset_ms < ? AND label_rule = ?",
		[]any{startMS, end.UnixMilli(), qualityLabelRule})
	if version != "" {
		batchWhere += " AND model_version = ?"
		batchArgs = append(batchArgs, version)
	}
	if err := s.db.WithContext(c.Request.Context()).Raw(`SELECT
		to_char((to_timestamp(fault_onset_ms/1000.0) AT TIME ZONE 'Asia/Shanghai')::date, 'YYYY-MM-DD') AS day,
		count(*) AS total,
		count(*) FILTER (WHERE hit) AS hit
		FROM (
			SELECT device_id, fault_onset_ms, bool_or(predicted_high) AS hit
			FROM prediction_outcomes
			WHERE `+batchWhere+` AND actual_fault AND fault_onset_ms IS NOT NULL
			GROUP BY device_id, fault_onset_ms
		) AS b
		GROUP BY 1 ORDER BY 1`, batchArgs...).Scan(&batchRows).Error; err != nil {
		fail(c, 503, 50000, "prediction quality query failed")
		return
	}
	batches := make(map[string][2]int64, len(batchRows))
	for _, row := range batchRows {
		batches[row.Day] = [2]int64{row.Total, row.Hit}
	}
	points := make([]gin.H, 0, len(rows))
	for _, row := range rows {
		day := row.Day.Format("2006-01-02")
		var batchTotal, batchHit int64
		if pair, found := batches[day]; found {
			batchTotal, batchHit = pair[0], pair[1]
		}
		points = append(points, gin.H{
			"day": day, "samples": row.Samples,
			"precision": row.Precision, "recall": row.Recall,
			"f1": row.F1, "accuracy": row.Accuracy,
			"ece": row.ECE, "prob_mean": row.ProbMean,
			"batch_total": batchTotal, "batch_hit": batchHit,
			"batch_rate": ratio(batchHit, batchTotal),
		})
	}
	var versionPtr *string
	if version != "" {
		versionPtr = &version
	}
	ok(c, gin.H{"points": points, "model_version": versionPtr})
}

// predictionQualityCalibration 返回最近 days 天的可靠性曲线。
func (s *Server) predictionQualityCalibration(c *gin.Context) {
	days, valid := qualityDays(c)
	if !valid {
		return
	}
	start := service.SimulationDay(time.Now()).AddDate(0, 0, -(days - 1))
	startMS := start.UnixMilli()
	if startMS < qualityEpochMS {
		startMS = qualityEpochMS
	}
	closed := time.Now().Add(-time.Duration(predictionWindowMS) * time.Millisecond).UnixMilli()
	where, args := qualityStationFilter(c,
		"window_end_ms >= ? AND window_end_ms <= ? AND label_rule = ?",
		[]any{startMS, closed, qualityLabelRule})
	where, version, args, err := s.qualityVersionFilter(c.Request.Context(), c, where, args)
	if err != nil {
		fail(c, 503, 50000, "prediction quality query failed")
		return
	}
	buckets, ece, err := s.calibrationQuery(c.Request.Context(), where, args)
	if err != nil {
		fail(c, 503, 50000, "prediction quality query failed")
		return
	}
	var versionPtr *string
	if version != "" {
		versionPtr = &version
	}
	ok(c, gin.H{"buckets": buckets, "ece": ece, "model_version": versionPtr})
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
	TrialStartedAt  *time.Time      `json:"trial_started_at"`
	PreviousVersion *string         `json:"previous_version"`
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
			TrialStartedAt: row.TrialStartedAt, PreviousVersion: row.PreviousVersion,
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
// 只算已成熟、且按 1 小时规则标注的行；旧口径行不能把重训门槛“垫”过去。
func (s *Server) qualityEligibility(ctx context.Context) (qualityTrainEligibility, error) {
	closed := time.Now().Add(-time.Duration(predictionWindowMS) * time.Millisecond).UnixMilli()
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
		WHERE window_end_ms >= ? AND window_end_ms <= ? AND label_rule = ?`,
		qualityEpochMS, closed, qualityLabelRule).Scan(&row).Error
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

	// 准入未过（eligible=false）的模型只能登记为 rejected，不能被 body.Status 覆盖成
	// active/candidate；否则会绕过准入把不合格模型当成候选甚至现役。
	status := "candidate"
	if !body.Eligible {
		if body.Status != "" && body.Status != "rejected" {
			fail(c, 400, 40001, "ineligible model can only be registered as rejected")
			return
		}
		status = "rejected"
	} else if body.Status != "" {
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
	recordExists := false
	if err != nil && !errors.Is(err, gorm.ErrRecordNotFound) {
		fail(c, 503, 50000, "model import failed")
		return
	}
	if err == nil {
		recordExists = true
		if existing.Status == "active" {
			fail(c, 409, 40901, "active model cannot be overwritten")
			return
		}
	}

	// 登记为现役：必须同时满足两条，否则前端/调用方会误以为已经切到该模型：
	//   1) 台账里没有其它现役版本（自身若是已存在记录则排除）；
	//   2) 推理服务实际加载的正是这个版本（verifyRuntimeModel 严格校验 /health）。
	// 任何一条不满足都拒绝登记，不写台账。
	if record.Status == "active" {
		var otherActive int64
		if err := s.db.WithContext(ctx).Model(&model.ModelRegistry{}).
			Where("status = ? AND model_version <> ?", "active", body.ModelVersion).
			Count(&otherActive).Error; err != nil {
			fail(c, 503, 50000, "model import failed")
			return
		}
		if otherActive > 0 {
			fail(c, 409, 40901, "an active model already exists, promote instead")
			return
		}
		if err := s.verifyRuntimeModel(ctx, body.ModelVersion); err != nil {
			fail(c, 502, 50201, "运行模型实际不是该版本，未登记为现役："+err.Error())
			return
		}
		now := time.Now().UTC()
		record.PromotedAt = &now
		var actor struct {
			Username string `gorm:"column:username"`
		}
		_ = s.db.WithContext(ctx).Table("users").Select("username").
			Where("id = ?", c.GetInt64("user_id")).Scan(&actor).Error
		record.PromotedBy = &actor.Username
	}

	if recordExists {
		record.ID = existing.ID
		updates := map[string]any{
			"algorithm": record.Algorithm, "status": record.Status, "threshold": record.Threshold,
			"calibration": record.Calibration, "metrics": record.Metrics,
			"baseline_metrics": record.BaselineMetrics, "data_source": record.DataSource,
			"train_samples": record.TrainSamples, "positive_samples": record.PositiveSamples,
			"artifact_path": record.ArtifactPath, "trained_at": record.TrainedAt,
			"rejected_reason": record.RejectedReason,
		}
		// 已存在记录转现役时，把现役时间一并保存；非现役更新不触碰这两列，避免误清现役时间。
		if record.Status == "active" {
			updates["promoted_at"] = record.PromotedAt
			updates["promoted_by"] = record.PromotedBy
		}
		if err := s.db.WithContext(ctx).Model(&model.ModelRegistry{}).Where("id = ?", existing.ID).
			Updates(updates).Error; err != nil {
			fail(c, 503, 50000, "model import failed")
			return
		}
		ok(c, gin.H{"model_version": record.ModelVersion, "status": record.Status, "updated": true})
		return
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

// predictionQualityPromote 旧入口已停用：它只改台账、不加载模型，会造成
// 「台账写 A、推理跑 B」。保留路由是为了让旧页面收到明确错误；
// 所有切换统一走 trial（实际加载并核对）→ confirm（再核对后转正）。
func (s *Server) predictionQualityPromote(c *gin.Context) {
	fail(c, 409, 40901, "直接提升已停用，请先试用（会实际切换并核对运行模型），试用期满后再确认转正")
}

// trialHours 是试用观察期。期满前不允许确认，避免刚切过去就下结论。
const trialHours = 24

// lockModelSwitch 用进程内小锁串行化模型切换。并发请求直接收到 409，
// 不排队等待，避免两个切换互相覆盖。跨副本并发仍依赖单实例部署。
func (s *Server) lockModelSwitch(c *gin.Context) bool {
	if s.modelSwitch.TryLock() {
		return true
	}
	fail(c, 409, 40901, "另一个模型切换正在进行，请稍后重试")
	return false
}

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

// readRuntimeModel 读取推理服务实际加载的模型版本；查询失败视为错误，
// 不能像列表页的辅助信息那样静默降级成空串。
func (s *Server) readRuntimeModel(ctx context.Context) (string, error) {
	if s.cfg.AIURL == "" {
		return "", errors.New("未配置推理服务地址")
	}
	reqCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet,
		strings.TrimRight(s.cfg.AIURL, "/")+"/health", nil)
	if err != nil {
		return "", err
	}
	resp, err := (&http.Client{Timeout: 3 * time.Second}).Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("健康检查返回 %d", resp.StatusCode)
	}
	var payload struct {
		ModelVersion string `json:"model_version"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 4096)).Decode(&payload); err != nil {
		return "", err
	}
	if payload.ModelVersion == "" {
		return "", errors.New("健康检查未返回运行模型版本")
	}
	return payload.ModelVersion, nil
}

// verifyRuntimeModel 核对推理服务实际加载的版本是否为目标版本。
func (s *Server) verifyRuntimeModel(ctx context.Context, expected string) error {
	running, err := s.readRuntimeModel(ctx)
	if err != nil {
		return err
	}
	if running != expected {
		return fmt.Errorf("运行模型为 %s，目标是 %s", running, expected)
	}
	return nil
}

// restoreRuntimeModel 在台账写入失败后，尽力把运行模型恢复到台账仍记录的状态。
// 只补偿一次，不重放失败的数据库写入；无法恢复时明确标为未知状态交人工核对。
func (s *Server) restoreRuntimeModel(ctx context.Context, version string) string {
	if version == "" {
		return "没有可恢复的旧版本，运行状态未知，请人工核对"
	}
	if err := s.reloadModel(ctx, version); err != nil {
		return "恢复运行模型 " + version + " 失败（" + err.Error() + "），运行状态未知，请人工核对"
	}
	if err := s.verifyRuntimeModel(ctx, version); err != nil {
		return "恢复运行模型 " + version + " 后核对失败（" + err.Error() + "），运行状态未知，请人工核对"
	}
	return "已恢复运行模型 " + version
}

// predictionQualityTrial 把候选切到试用：先核对当前运行模型与台账现役一致，
// 再 reload 并核对目标版本，全部通过后才改台账。DB 写失败时只补偿一次运行时。
func (s *Server) predictionQualityTrial(c *gin.Context) {
	version := c.Param("version")
	if version == "" || len(version) > 64 {
		fail(c, 400, 40001, "invalid model version")
		return
	}
	if !s.lockModelSwitch(c) {
		return
	}
	defer s.modelSwitch.Unlock()
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
	} else if !errors.Is(err, gorm.ErrRecordNotFound) {
		// 台账查询失败不能吞掉后继续 reload：否则会在不知道当前现役的情况下切走，
		// 后续 verify/补偿都失去基准。
		fail(c, 503, 50000, "台账查询失败，未做切换："+err.Error())
		return
	}
	running, err := s.readRuntimeModel(ctx)
	if err != nil {
		fail(c, 502, 50201, "无法核对当前运行模型，未做切换："+err.Error())
		return
	}
	if previous != "" && running != previous {
		fail(c, 409, 40901, fmt.Sprintf("台账现役为 %s，推理服务实际运行 %s；请先人工核对，未做切换", previous, running))
		return
	}
	if err := s.reloadModel(ctx, version); err != nil {
		fail(c, 502, 50201, "切换失败，已保留原模型："+err.Error())
		return
	}
	if err := s.verifyRuntimeModel(ctx, version); err != nil {
		fallback := previous
		if fallback == "" {
			fallback = running
		}
		note := s.restoreRuntimeModel(ctx, fallback)
		fail(c, 502, 50201, "切换后核对运行模型失败，台账未改动；"+note+"："+err.Error())
		return
	}
	now := time.Now().UTC()
	if err := s.db.WithContext(ctx).Model(&model.ModelRegistry{}).Where("id = ?", target.ID).
		Updates(map[string]any{
			"status": "trial", "trial_started_at": now, "previous_version": nullableString(previous),
		}).Error; err != nil {
		fallback := previous
		if fallback == "" {
			fallback = running
		}
		note := s.restoreRuntimeModel(ctx, fallback)
		fail(c, 503, 50000, "运行模型已切到 "+version+"，但台账写入失败；"+note+"。系统不会自动重试，请核对后再操作")
		return
	}
	ok(c, gin.H{
		"model_version": version, "status": "trial",
		"trial_started_at": now, "previous_version": previous,
	})
}

// predictionQualityConfirm 试用期满后转正；转正前必须核对运行版本仍是试用版本，
// 避免把台账转正到实际没在跑的模型。未满 24 小时直接拒绝并给出剩余时间。
func (s *Server) predictionQualityConfirm(c *gin.Context) {
	version := c.Param("version")
	if !s.lockModelSwitch(c) {
		return
	}
	defer s.modelSwitch.Unlock()
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
	if err := s.verifyRuntimeModel(ctx, version); err != nil {
		fail(c, 409, 40901, "运行模型与试用版本不一致，未转正："+err.Error())
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
		fail(c, 503, 50000, "台账写入失败，版本仍为试用中；运行模型未改动，请核对后再操作")
		return
	}
	ok(c, gin.H{"model_version": version, "status": "active", "promoted_at": now})
}

// predictionQualityRollback 把试用版本退回上一版：先核对运行模型确实是该试用版本，
// 再 reload 回 previous 并核对，成功后才改台账。DB 写失败时只补偿一次运行时。
func (s *Server) predictionQualityRollback(c *gin.Context) {
	version := c.Param("version")
	if !s.lockModelSwitch(c) {
		return
	}
	defer s.modelSwitch.Unlock()
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
	if err := s.verifyRuntimeModel(ctx, version); err != nil {
		fail(c, 409, 40901, "运行模型不是试用版本，未回退："+err.Error())
		return
	}
	previous := *target.PreviousVersion
	if err := s.reloadModel(ctx, previous); err != nil {
		fail(c, 502, 50201, "回退失败，当前模型未改动："+err.Error())
		return
	}
	if err := s.verifyRuntimeModel(ctx, previous); err != nil {
		note := s.restoreRuntimeModel(ctx, version)
		fail(c, 502, 50201, "回退后核对运行模型失败，台账未改动；"+note+"："+err.Error())
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
		note := s.restoreRuntimeModel(ctx, version)
		fail(c, 503, 50000, "运行模型已回退到 "+previous+"，但台账写入失败；"+note+"。系统不会自动重试，请核对后再操作")
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

// operationsQualityRow 是运维口径的聚合结果，一次查询把「台次」与「提前量」都取回来。
type operationsQualityRow struct {
	Devices     int64   `gorm:"column:devices"`
	Samples     int64   `gorm:"column:samples"`
	Faults      int64   `gorm:"column:faults"`
	Captured    int64   `gorm:"column:captured"`
	FalseAlarms int64   `gorm:"column:false_alarms"`
	LeadSamples int64   `gorm:"column:lead_samples"`
	LeadP50     float64 `gorm:"column:lead_p50"`
	LeadP90     float64 `gorm:"column:lead_p90"`
}

// operationsFalseAlarmEventRow 是“误报事件”口径：把相邻高分窗口合并成事件后的统计。
// false_alarm_events 是“不含任何真实故障成员的合并事件”数；event_windows 是所有高分窗口数；
// events_with_fault 是含故障成员的事件数；events 是合并后事件总数。
type operationsFalseAlarmEventRow struct {
	Events             int64 `gorm:"column:events"`
	EventWindows       int64 `gorm:"column:event_windows"`
	EventsWithFault    int64 `gorm:"column:events_with_fault"`
	FalseAlarmEvents   int64 `gorm:"column:false_alarm_events"`
}

// predictionQualityOperations 是值班视角的质量视图。
//
// 原有 summary 按预测记录算 precision/recall，值班人员拿到的是 0.43 这样的数字，
// 既不知道该不该信、也不知道一天被打扰几次。这里换粒度：按设备数误报次数、
// 按故障批次看提前多久被预警，两者都是同一批 prediction_outcomes 的投影，不引入新指标。
func (s *Server) predictionQualityOperations(c *gin.Context) {
	days, err := strconv.Atoi(c.DefaultQuery("days", "1"))
	if err != nil || days < 1 || days > 30 {
		fail(c, 400, 40001, "invalid days")
		return
	}
	// 右端固定为「已成熟」的窗口，避免把还没走完 1 小时的标签算进来。
	now := time.Now()
	start := now.Add(-time.Duration(days) * 24 * time.Hour).UnixMilli()
	if qualityEpochMS > start {
		start = qualityEpochMS
	}
	closed := now.Add(-time.Duration(predictionWindowMS) * time.Millisecond).UnixMilli()
	where, args := qualityStationFilter(c,
		"window_end_ms >= ? AND window_end_ms <= ? AND label_rule = ?",
		[]any{start, closed, qualityLabelRule})
	where, version, args, err := s.qualityVersionFilter(c.Request.Context(), c, where, args)
	if err != nil {
		fail(c, 503, 50000, "prediction quality query failed")
		return
	}
	var row operationsQualityRow
	// 提前量只看「同一故障批次的 1 小时标签」：更晚的预测不能算提前量，
	// 更早的 60–120 分钟报警不属于本目标，不能被算成“提前发现”。
	err = s.db.WithContext(c.Request.Context()).Raw(`WITH base AS (
			SELECT device_id, window_end_ms, predicted_high, actual_fault, fault_onset_ms
			FROM prediction_outcomes WHERE `+where+`
		),
		pairs AS (
			SELECT DISTINCT device_id, fault_onset_ms FROM base
			WHERE actual_fault AND fault_onset_ms IS NOT NULL
		),
		leads AS (
			SELECT p.fault_onset_ms,
				(SELECT min(b.window_end_ms) FROM base AS b
				 WHERE b.device_id = p.device_id AND b.actual_fault
					AND b.fault_onset_ms = p.fault_onset_ms
					AND b.window_end_ms < p.fault_onset_ms
					AND b.predicted_high) AS first_high_ms
			FROM pairs AS p
		),
		totals AS (
			SELECT count(DISTINCT device_id)::bigint AS devices,
				count(*)::bigint AS samples,
				count(*) FILTER (WHERE predicted_high AND NOT actual_fault)::bigint AS false_alarms
			FROM base
		),
		faults AS (
			SELECT count(*)::bigint AS faults,
				count(*) FILTER (WHERE first_high_ms IS NOT NULL)::bigint AS captured
			FROM leads
		),
		lead_stats AS (
			SELECT count(*)::bigint AS lead_samples,
				coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY (fault_onset_ms - first_high_ms) / 60000.0), 0) AS lead_p50,
				coalesce(percentile_cont(0.9) WITHIN GROUP (ORDER BY (fault_onset_ms - first_high_ms) / 60000.0), 0) AS lead_p90
			FROM leads WHERE first_high_ms IS NOT NULL
		)
		SELECT totals.devices, totals.samples, totals.false_alarms,
			faults.faults, faults.captured,
			lead_stats.lead_samples, lead_stats.lead_p50, lead_stats.lead_p90
		FROM totals, faults, lead_stats`, args...).Scan(&row).Error
	if err != nil {
		fail(c, 503, 50000, "prediction quality query failed")
		return
	}
	// 空窗口时 devices 为 0，除法会得到 NaN 并让 JSON 序列化失败，故先判零。
	perDeviceDay := 0.0
	if row.Devices > 0 {
		perDeviceDay = math.Round(float64(row.FalseAlarms)/float64(row.Devices)/float64(days)*100) / 100
	}
	var versionPtr *string
	if version != "" {
		versionPtr = &version
	}
	// 误报事件：按设备+模型版本，相邻高分窗口若 window_end_ms 间隔 <=10 分钟则合并为一个事件；
	// 单个高分窗口也算一次事件。事件中只要有任一 window_1h 且已成熟的成员窗口对应真实故障，
	// 就不算误报，否则计为一次误报事件。只统计高分窗口（predicted_high），
	// 底层 where 已按 window_end_ms 区间+站点+版本限定，走设备时间索引，不做全表扫。
	// 注意：这是“事件”口径，不是窗口口径；窗口误报数见 false_alarms，二者不可混称。
	var ev operationsFalseAlarmEventRow
	err = s.db.WithContext(c.Request.Context()).Raw(`WITH high AS (
			SELECT device_id, model_version, window_end_ms, actual_fault
			FROM prediction_outcomes WHERE predicted_high AND `+where+`
		),
		numbered AS (
			SELECT device_id, model_version, window_end_ms, actual_fault,
				CASE WHEN window_end_ms - lag(window_end_ms)
					OVER (PARTITION BY device_id, model_version ORDER BY window_end_ms) > 600000
					THEN 1 ELSE 0 END AS new_event
			FROM high
		),
		events AS (
			SELECT device_id, model_version,
				sum(new_event) OVER (PARTITION BY device_id, model_version ORDER BY window_end_ms) AS grp,
				actual_fault
			FROM numbered
		),
		event_stats AS (
			SELECT device_id, model_version, grp,
				bool_or(actual_fault) AS has_fault,
				count(*)::bigint AS window_count
			FROM events GROUP BY device_id, model_version, grp
		)
		SELECT count(*)::bigint AS events,
			coalesce(sum(window_count), 0)::bigint AS event_windows,
			count(*) FILTER (WHERE has_fault)::bigint AS events_with_fault,
			count(*) FILTER (WHERE NOT has_fault)::bigint AS false_alarm_events
		FROM event_stats`, args...).Scan(&ev).Error
	if err != nil {
		fail(c, 503, 50000, "prediction quality query failed")
		return
	}

	// 误报事件按设备日摊薄（与窗口口径的 false_alarms_per_device_day 并列，不可混用）。
	falseAlarmEventsPerDeviceDay := 0.0
	if row.Devices > 0 {
		falseAlarmEventsPerDeviceDay = math.Round(float64(ev.FalseAlarmEvents)/float64(row.Devices)/float64(days)*100) / 100
	}

	ok(c, gin.H{
		"days": days, "devices": row.Devices, "samples": row.Samples,
		"faults": row.Faults, "captured": row.Captured, "false_alarms": row.FalseAlarms,
		"false_alarms_per_device_day": perDeviceDay,
		"lead_minutes_p50":            int64(math.Round(row.LeadP50)),
		"lead_minutes_p90":            int64(math.Round(row.LeadP90)),
		"lead_samples":                row.LeadSamples,
		"model_version":               versionPtr,
		"false_alarm_events":          ev.FalseAlarmEvents,
		"false_alarm_events_per_device_day": falseAlarmEventsPerDeviceDay,
		"false_alarm_event_windows":   ev.EventWindows,
		"false_alarm_events_with_fault": ev.EventsWithFault,
		"false_alarm_event_total":     ev.Events,
	})
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
	closed := now.Add(-time.Duration(predictionWindowMS) * time.Millisecond).UnixMilli()
	var recent struct {
		ProbMean  float64 `gorm:"column:prob_mean"`
		HighRatio float64 `gorm:"column:high_ratio"`
		Samples   int64   `gorm:"column:samples"`
	}
	if err := s.db.WithContext(ctx).Raw(`SELECT coalesce(avg(probability),0) AS prob_mean,
		coalesce(avg(CASE WHEN predicted_high THEN 1.0 ELSE 0.0 END),0) AS high_ratio,
		count(*) AS samples
		FROM prediction_outcomes WHERE window_end_ms >= ? AND window_end_ms <= ? AND label_rule = ?`,
		recentStart.UnixMilli(), closed, qualityLabelRule).
		Scan(&recent).Error; err != nil {
		return err
	}
	if recent.Samples == 0 {
		return nil
	}
	var baseline struct {
		ProbMean float64 `gorm:"column:prob_mean"`
		HighMean float64 `gorm:"column:high_mean"`
		Days     int64   `gorm:"column:days"`
	}
	if err := s.db.WithContext(ctx).Raw(`SELECT coalesce(avg(daily),0) AS prob_mean,
		coalesce(avg(daily_high),0) AS high_mean, count(*) AS days
		FROM (SELECT (to_timestamp(window_end_ms/1000.0) AT TIME ZONE 'Asia/Shanghai')::date AS day,
			avg(probability) AS daily,
			avg(CASE WHEN predicted_high THEN 1.0 ELSE 0.0 END) AS daily_high
			FROM prediction_outcomes WHERE window_end_ms >= ? AND window_end_ms < ? AND label_rule = ?
			GROUP BY 1) AS t`, baselineStart.UnixMilli(), recentStart.UnixMilli(), qualityLabelRule).
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
		// 基线优先用此前 7 天的日均高风险占比；新部署没有历史时退化到经验区间中点，
		// 并在 detail 里标明来源，避免读的人把中点当成实测基线。
		highBaseline := (driftL1HighRatioMin + driftL1HighRatioMax) / 2
		baselineSource := "range_midpoint"
		if baseline.Days > 0 && baseline.HighMean > 0 {
			highBaseline = baseline.HighMean
			baselineSource = "trailing_7d"
		}
		if err := s.recordDrift(ctx, model.PredictionDriftEvent{
			Level: "L1", Signal: "high_ratio", Observed: &recent.HighRatio,
			Baseline: &highBaseline, Threshold: ptrFloat(driftL1HighRatioMax), Action: "watch",
			Detail: detailJSON(gin.H{"lower": driftL1HighRatioMin, "upper": driftL1HighRatioMax,
				"baseline_source": baselineSource, "baseline_days": baseline.Days,
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
		WHERE day >= ? AND day < ? AND label_rule = ? ORDER BY day ASC`,
		dateOnly(recentStart), dateOnly(today.AddDate(0, 0, 1)), qualityLabelRule).
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
		FROM prediction_quality_daily WHERE day >= ? AND day < ? AND label_rule = ?`,
		dateOnly(baselineStart), dateOnly(baselineEnd), qualityLabelRule).
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
