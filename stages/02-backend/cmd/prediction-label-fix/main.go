// 一次性修正 prediction_outcomes 的历史真值口径。
//
// 背景：旧 windowFault 把右端放宽到 window_end+2h，但训练目标与正式契约是
// (window_end, window_end+1h]。本命令只处理 window_end_ms >= epoch 且已成熟的行，
// 默认 dry-run；只有显式 -apply 才写库，并且逐行保留旧值到审计表。
//
// 不连接生产、不自动执行；生产执行步骤见同目录 README。
//
// 确定性约束（修复点）：
//   - dry-run 与 apply 都使用 (window_end_ms, prediction_id) 稳定 keyset 游标，
//     保证每一行最多被处理一次，scanned/changed/flipped/remaining 不再虚高。
//   - remaining 用真实未处理数量（pending - scanned），而非简单 pending-maxRows。
//   - 事务失败时不把未提交批次的计数写进运行表；审计记录只对应成功 UPDATE 的行。
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"

	"powersystem/backend/internal/model"
	"powersystem/backend/internal/service"
)

const dsnEnv = "PREDICTION_QUALITY_DSN"

// windowFaultFunc 判定预测窗口 (windowEndMS, windowEndMS+1h] 内该设备是否被注入越线。
// 默认实现来自 service.StationDeviceCache.WindowFault，测试可注入确定性实现。
type windowFaultFunc func(deviceCode, stationCode string, windowEndMS int64) (bool, *int64, error)

type outcomeRow struct {
	PredictionID int64  `gorm:"column:prediction_id"`
	DeviceCode   string `gorm:"column:device_code"`
	StationCode  string `gorm:"column:station_code"`
	WindowEndMS  int64  `gorm:"column:window_end_ms"`
	ModelVersion string `gorm:"column:model_version"`
	ActualFault  bool   `gorm:"column:actual_fault"`
	FaultOnsetMS *int64 `gorm:"column:fault_onset_ms"`
	LabelRule    string `gorm:"column:label_rule"`
}

type correction struct {
	row     outcomeRow
	actual  bool
	onset   *int64
	changed bool
	flipPos bool
	flipNeg bool
}

type summary struct {
	Mode                      string `json:"mode"`
	RunID                     string `json:"run_id,omitempty"`
	EpochMS                   int64  `json:"epoch_ms"`
	MatureBeforeMS            int64  `json:"mature_before_ms"`
	PendingRows               int64  `json:"pending_rows"`
	Scanned                   int64  `json:"scanned"`
	Updated                   int64  `json:"updated"`
	Changed                   int64  `json:"changed"`
	Unchanged                 int64  `json:"unchanged"`
	FlippedPositiveToNegative int64  `json:"flipped_positive_to_negative"`
	FlippedNegativeToPositive int64  `json:"flipped_negative_to_positive"`
	SkippedConcurrent         int64  `json:"skipped_concurrent"`
	Remaining                 int64  `json:"remaining"`
}

type runConfig struct {
	apply     bool
	actor     string
	reason    string
	epoch     int64
	maxRows   int
	batchSize int
	runID     string
}

func main() {
	apply := flag.Bool("apply", false, "写库并生成审计记录；默认只做 dry-run")
	actor := flag.String("actor", "", "apply 必填：操作者标识")
	reason := flag.String("reason", "", "apply 必填：修正原因")
	epoch := flag.Int64("epoch", model.PredictionQualityEpochMS, "只处理 window_end_ms >= epoch 的行；不得早于冻结起点")
	maxRows := flag.Int("max-rows", 100000, "单次最多扫描的行数，必须大于 0")
	batchSize := flag.Int("batch-size", 500, "单事务行数，1..2000")
	runID := flag.String("run-id", "", "可选：显式运行编号，默认时间戳+PID")
	flag.Parse()

	if *apply && (*actor == "" || *reason == "") {
		fatalf("-apply requires -actor and -reason")
	}
	if *maxRows <= 0 {
		fatalf("-max-rows must be > 0")
	}
	if *batchSize < 1 || *batchSize > 2000 {
		fatalf("-batch-size must be 1..2000")
	}
	if *epoch < model.PredictionQualityEpochMS {
		fatalf("-epoch must be >= %d (frozen quality epoch)", model.PredictionQualityEpochMS)
	}
	if *runID == "" {
		*runID = fmt.Sprintf("%s-%d", time.Now().UTC().Format("20060102T150405Z"), os.Getpid())
	}

	dsn := os.Getenv(dsnEnv)
	if dsn == "" {
		fatalf("%s is not set", dsnEnv)
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{
		DisableAutomaticPing: true,
		Logger:               logger.Default.LogMode(logger.Silent),
	})
	if err != nil {
		fatalf("open postgres: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()
	if err := db.WithContext(ctx).Exec("SELECT 1").Error; err != nil {
		fatalf("ping postgres: %v", err)
	}
	if !db.Migrator().HasColumn("prediction_outcomes", "label_rule") ||
		!db.Migrator().HasTable("prediction_outcome_label_corrections") ||
		!db.Migrator().HasTable("prediction_outcome_label_correction_runs") {
		fatalf("019_prediction_outcome_label_rule.sql is not applied")
	}

	cfg := runConfig{
		apply:     *apply,
		actor:     *actor,
		reason:    *reason,
		epoch:     *epoch,
		maxRows:   *maxRows,
		batchSize: *batchSize,
		runID:     *runID,
	}
	cache := service.NewStationDeviceCache(db, ctx)
	result, runErr := runCorrection(ctx, db, cfg, cache.WindowFault)
	if runErr != nil {
		// 失败时 result 只含已提交批次的计数（未提交批次未并入），便于交接核对。
		printSummary(result)
		fatalf("%v", runErr)
	}
	printSummary(result)
}

// runCorrection 执行一次修正扫描。dry-run 不写库；apply 逐批事务写库并留审计。
// 返回 (summary, error)；apply 失败时 summary 含已成功提交批次的计数，error 非 nil。
func runCorrection(ctx context.Context, db *gorm.DB, cfg runConfig, wf windowFaultFunc) (summary, error) {
	now := time.Now()
	matureBefore := now.Add(-time.Duration(service.PredictionHorizonMS) * time.Millisecond).UnixMilli()
	if matureBefore < cfg.epoch {
		return summary{}, fmt.Errorf("epoch is not mature yet: epoch=%d mature_before=%d", cfg.epoch, matureBefore)
	}

	var pending int64
	if err := db.WithContext(ctx).Table("prediction_outcomes").
		Where("window_end_ms >= ? AND window_end_ms <= ? AND label_rule <> ?", cfg.epoch, matureBefore, model.PredictionLabelRule).
		Count(&pending).Error; err != nil {
		return summary{}, fmt.Errorf("count pending rows: %w", err)
	}

	result := summary{
		Mode: "dry-run", RunID: cfg.runID, EpochMS: cfg.epoch, MatureBeforeMS: matureBefore, PendingRows: pending,
	}
	if cfg.apply {
		result.Mode = "apply"
		if err := db.WithContext(ctx).Exec(`INSERT INTO prediction_outcome_label_correction_runs
			(run_id, actor, reason, epoch_ms, mature_before_ms, status) VALUES (?,?,?,?,?,'running')`,
			cfg.runID, cfg.actor, cfg.reason, cfg.epoch, matureBefore).Error; err != nil {
			return result, fmt.Errorf("create correction run: %w", err)
		}
	}

	rows := make([]outcomeRow, 0, cfg.batchSize)
	corrections := make([]correction, 0, cfg.batchSize)
	// 稳定 keyset 游标：每批从 (cursorWE, cursorPID) 之后取下一页，保证每行最多一次。
	cursorWE := cfg.epoch
	cursorPID := int64(-1)
	for int(result.Scanned) < cfg.maxRows {
		rows = rows[:0]
		limit := cfg.batchSize
		if left := cfg.maxRows - int(result.Scanned); left < limit {
			limit = left
		}
		if err := db.WithContext(ctx).Table("prediction_outcomes").
			Select("prediction_id, device_code, station_code, window_end_ms, model_version, actual_fault, fault_onset_ms, label_rule").
			Where("window_end_ms >= ? AND window_end_ms <= ? AND label_rule <> ?", cfg.epoch, matureBefore, model.PredictionLabelRule).
			Where("(window_end_ms > ? OR (window_end_ms = ? AND prediction_id > ?))", cursorWE, cursorWE, cursorPID).
			Order("window_end_ms ASC, prediction_id ASC").Limit(limit).Scan(&rows).Error; err != nil {
			if cfg.apply {
				_ = finishRun(db, cfg.runID, result, err.Error(), time.Now().UTC())
			}
			return result, fmt.Errorf("load batch: %w", err)
		}
		if len(rows) == 0 {
			break
		}
		corrections = corrections[:0]
		for _, row := range rows {
			actual, onset, err := wf(row.DeviceCode, row.StationCode, row.WindowEndMS)
			if err != nil {
				if cfg.apply {
					_ = finishRun(db, cfg.runID, result, err.Error(), time.Now().UTC())
				}
				return result, fmt.Errorf("window fault %d: %w", row.PredictionID, err)
			}
			changed := row.ActualFault != actual || !sameOnset(row.FaultOnsetMS, onset)
			item := correction{row: row, actual: actual, onset: onset, changed: changed}
			if changed {
				item.flipPos = row.ActualFault && !actual
				item.flipNeg = !row.ActualFault && actual
			}
			corrections = append(corrections, item)
		}

		if cfg.apply {
			if err := applyBatch(db, cfg.runID, corrections, &result); err != nil {
				_ = finishRun(db, cfg.runID, result, err.Error(), time.Now().UTC())
				return result, err
			}
		} else {
			for _, item := range corrections {
				countCorrection(item, &result)
			}
		}
		result.Scanned += int64(len(rows))
		last := rows[len(rows)-1]
		cursorWE = last.WindowEndMS
		cursorPID = last.PredictionID
	}

	// remaining = 真实尚未处理的行数（pending 减去本轮实际已扫描去重行数）。
	if pending > result.Scanned {
		result.Remaining = pending - result.Scanned
	}
	if cfg.apply {
		if err := finishRun(db, cfg.runID, result, "", time.Now().UTC()); err != nil {
			return result, fmt.Errorf("finish correction run: %w", err)
		}
	}
	return result, nil
}

// applyBatch 在一个事务里写审计并更新标签；任何一行失败整批回滚。
// 审计记录在成功 UPDATE（RowsAffected>0）之后写入，保证审计只对应成功更新。
// 仅在本函数返回 nil（事务已提交）后才把本批计数并入 result，
// 因此事务失败时未提交批次的计数不会进入运行表。
func applyBatch(db *gorm.DB, runID string, items []correction, result *summary) error {
	var batch summary
	err := db.Transaction(func(tx *gorm.DB) error {
		for _, item := range items {
			updated := tx.Exec(`UPDATE prediction_outcomes
				SET actual_fault = ?, fault_onset_ms = ?, label_rule = ?
				WHERE prediction_id = ? AND label_rule <> ?`,
				item.actual, item.onset, model.PredictionLabelRule, item.row.PredictionID, model.PredictionLabelRule)
			if updated.Error != nil {
				return fmt.Errorf("update prediction %d: %w", item.row.PredictionID, updated.Error)
			}
			if updated.RowsAffected == 0 {
				// 并发已将该行改为 window_1h，跳过且不留审计。
				batch.SkippedConcurrent++
				continue
			}
			if err := tx.Exec(`INSERT INTO prediction_outcome_label_corrections
				(prediction_id, run_id, rule_from, rule_to, actual_fault_before, fault_onset_ms_before,
				 actual_fault_after, fault_onset_ms_after, changed)
				VALUES (?,?,?,?,?,?,?,?,?)
				ON CONFLICT (prediction_id, rule_to) DO NOTHING`,
				item.row.PredictionID, runID, item.row.LabelRule, model.PredictionLabelRule,
				item.row.ActualFault, item.row.FaultOnsetMS, item.actual, item.onset, item.changed).Error; err != nil {
				return fmt.Errorf("audit prediction %d: %w", item.row.PredictionID, err)
			}
			batch.Updated++
			countCorrection(item, &batch)
		}
		return nil
	})
	if err != nil {
		return err
	}
	result.Updated += batch.Updated
	result.Changed += batch.Changed
	result.Unchanged += batch.Unchanged
	result.FlippedPositiveToNegative += batch.FlippedPositiveToNegative
	result.FlippedNegativeToPositive += batch.FlippedNegativeToPositive
	result.SkippedConcurrent += batch.SkippedConcurrent
	return nil
}

func countCorrection(item correction, result *summary) {
	if item.changed {
		result.Changed++
	} else {
		result.Unchanged++
	}
	if item.flipPos {
		result.FlippedPositiveToNegative++
	}
	if item.flipNeg {
		result.FlippedNegativeToPositive++
	}
}

// finishRun 写入运行表收尾计数；finishedAt 由调用方传入（DB 无关，便于 SQLite 测试）。
func finishRun(db *gorm.DB, runID string, result summary, failure string, finishedAt time.Time) error {
	var errValue *string
	if failure != "" {
		errValue = &failure
	}
	status := "completed"
	if failure != "" {
		status = "failed"
	}
	return db.Exec(`UPDATE prediction_outcome_label_correction_runs
		SET finished_at = ?, scanned = ?, updated = ?, changed = ?, unchanged = ?,
			flipped_positive_to_negative = ?, flipped_negative_to_positive = ?,
			status = ?, error = ?
		WHERE run_id = ?`,
		finishedAt, result.Scanned, result.Updated, result.Changed, result.Unchanged,
		result.FlippedPositiveToNegative, result.FlippedNegativeToPositive,
		status, errValue, runID).Error
}

func sameOnset(a, b *int64) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return *a == *b
}

func printSummary(result summary) {
	raw, _ := json.Marshal(result)
	fmt.Println(string(raw))
}

func fatalf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(1)
}
