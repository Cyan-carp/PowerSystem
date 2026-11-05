package main

import (
	"context"
	"database/sql"
	"fmt"
	"testing"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	_ "modernc.org/sqlite"
)

// 用 postgres 方言跑在 SQLite 内存库上，复用内部 service 测试的同款技巧，
// 使 prediction-label-fix 的查询/写入可在本地隔离验证，不连生产。
func testDB(t *testing.T) *gorm.DB {
	t.Helper()
	conn, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	conn.SetMaxOpenConns(1) // 内存库需单连接共享
	db, err := gorm.Open(postgres.New(postgres.Config{Conn: conn}), &gorm.Config{
		Logger: logger.Default.LogMode(logger.Silent),
	})
	if err != nil {
		t.Fatal(err)
	}
	stmts := []string{
		`CREATE TABLE prediction_outcomes (
			prediction_id BIGINT PRIMARY KEY,
			device_code TEXT,
			station_code TEXT,
			window_end_ms BIGINT,
			model_version TEXT,
			actual_fault BOOLEAN,
			fault_onset_ms BIGINT,
			label_rule TEXT NOT NULL DEFAULT 'legacy'
		)`,
		`CREATE TABLE prediction_outcome_label_correction_runs (
			run_id TEXT PRIMARY KEY,
			started_at TEXT,
			finished_at TEXT,
			actor TEXT NOT NULL,
			reason TEXT NOT NULL,
			epoch_ms BIGINT NOT NULL,
			mature_before_ms BIGINT NOT NULL,
			scanned INT NOT NULL DEFAULT 0,
			updated INT NOT NULL DEFAULT 0,
			changed INT NOT NULL DEFAULT 0,
			unchanged INT NOT NULL DEFAULT 0,
			flipped_positive_to_negative INT NOT NULL DEFAULT 0,
			flipped_negative_to_positive INT NOT NULL DEFAULT 0,
			status TEXT NOT NULL DEFAULT 'running',
			error TEXT
		)`,
		`CREATE TABLE prediction_outcome_label_corrections (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			prediction_id BIGINT NOT NULL,
			run_id TEXT NOT NULL,
			rule_from TEXT NOT NULL,
			rule_to TEXT NOT NULL,
			actual_fault_before BOOLEAN NOT NULL,
			fault_onset_ms_before BIGINT,
			actual_fault_after BOOLEAN NOT NULL,
			fault_onset_ms_after BIGINT,
			changed BOOLEAN NOT NULL,
			corrected_at TEXT
		)`,
		`CREATE UNIQUE INDEX uq_corrections ON prediction_outcome_label_corrections(prediction_id, rule_to)`,
		`CREATE TABLE simulation_alarm_policy_devices (
			station_code TEXT NOT NULL,
			device_code TEXT NOT NULL,
			active BOOLEAN NOT NULL
		)`,
	}
	for _, s := range stmts {
		if err := db.Exec(s).Error; err != nil {
			t.Fatalf("create schema: %v", err)
		}
	}
	return db
}

const seedBaseMS = int64(1793604000000)

func seedRows(t *testing.T, db *gorm.DB, n int) {
	t.Helper()
	for i := 1; i <= n; i++ {
		dev := "ok"
		if i%3 == 0 {
			dev = "fault"
		}
		we := seedBaseMS + int64(i)*600000
		if err := db.Exec(`INSERT INTO prediction_outcomes
			(prediction_id, device_code, station_code, window_end_ms, model_version, actual_fault, fault_onset_ms, label_rule)
			VALUES (?,?,?,?,?,?,?,?)`,
			i, dev, "ST", we, "m", false, nil, "legacy").Error; err != nil {
			t.Fatal(err)
		}
	}
}

// TestDryRunKeysetNoDuplicate 证明：batch-size 小于总行数时，dry-run 每行最多一次，
// scanned 等于总行数（若游标失效会反复读同一批直到 maxRows，scanned 将远大于 n），
// changed/remaining 计数真实。
func TestDryRunKeysetNoDuplicate(t *testing.T) {
	db := testDB(t)
	const n = 25
	seedRows(t, db, n)

	wf := func(deviceCode, stationCode string, windowEndMS int64) (bool, *int64, error) {
		if deviceCode == "fault" {
			o := windowEndMS + 1000
			return true, &o, nil
		}
		return false, nil, nil
	}

	cfg := runConfig{apply: false, epoch: 0, maxRows: 1000, batchSize: 10, runID: "dry-keyset"}
	res, err := runCorrection(context.Background(), db, cfg, wf)
	if err != nil {
		t.Fatal(err)
	}
	if res.Scanned != n {
		t.Fatalf("scanned=%d, want %d (游标失效会重复读取同一批)", res.Scanned, n)
	}
	var wantChanged int64
	for i := 1; i <= n; i++ {
		if i%3 == 0 {
			wantChanged++
		}
	}
	if res.Changed != wantChanged {
		t.Fatalf("changed=%d, want %d", res.Changed, wantChanged)
	}
	if res.Updated != 0 {
		t.Fatalf("dry-run updated=%d, want 0", res.Updated)
	}
	if res.Remaining != 0 {
		t.Fatalf("remaining=%d, want 0 (已全部处理)", res.Remaining)
	}

	// 验证 prediction_outcomes 未被写入（dry-run 不写库）。
	var legacy int64
	db.Table("prediction_outcomes").Where("label_rule <> 'window_1h'").Count(&legacy)
	if legacy != n {
		t.Fatalf("dry-run 不应改变标签，仍有 %d 行非 window_1h", legacy)
	}
}

// TestDryRunRemainingUsesRealScan 证明 remaining = pending - 真实已扫描去重行数。
func TestDryRunRemainingUsesRealScan(t *testing.T) {
	db := testDB(t)
	const n = 25
	seedRows(t, db, n)

	wf := func(string, string, int64) (bool, *int64, error) { return false, nil, nil }

	cfg := runConfig{apply: false, epoch: 0, maxRows: 15, batchSize: 10, runID: "dry-remain"}
	res, err := runCorrection(context.Background(), db, cfg, wf)
	if err != nil {
		t.Fatal(err)
	}
	if res.Scanned != 15 {
		t.Fatalf("scanned=%d, want 15", res.Scanned)
	}
	if res.Remaining != 10 {
		t.Fatalf("remaining=%d, want 10 (真实未处理行数)", res.Remaining)
	}
}

// TestApplyIsIdempotent 证明 apply 幂等：第二次运行 updated=0、审计记录不增长。
func TestApplyIsIdempotent(t *testing.T) {
	db := testDB(t)
	const n = 25
	seedRows(t, db, n)

	wf := func(deviceCode, stationCode string, windowEndMS int64) (bool, *int64, error) {
		if deviceCode == "fault" {
			o := windowEndMS + 1000
			return true, &o, nil
		}
		return false, nil, nil
	}

	cfg1 := runConfig{apply: true, actor: "tester", reason: "idempotency", epoch: 0, maxRows: 1000, batchSize: 10, runID: "apply-1"}
	res1, err := runCorrection(context.Background(), db, cfg1, wf)
	if err != nil {
		t.Fatal(err)
	}
	if res1.Scanned != n {
		t.Fatalf("apply1 scanned=%d, want %d", res1.Scanned, n)
	}
	if res1.Updated != n {
		t.Fatalf("apply1 updated=%d, want %d", res1.Updated, n)
	}

	var stillLegacy int64
	db.Table("prediction_outcomes").Where("label_rule <> 'window_1h'").Count(&stillLegacy)
	if stillLegacy != 0 {
		t.Fatalf("apply1 后仍有 %d 行非 window_1h", stillLegacy)
	}
	var audit1 int64
	db.Table("prediction_outcome_label_corrections").Count(&audit1)
	if audit1 != n {
		t.Fatalf("apply1 审计行数=%d, want %d", audit1, n)
	}

	// 同库二次 apply：幂等，updated=0，审计不增长。
	cfg2 := runConfig{apply: true, actor: "tester", reason: "idempotency", epoch: 0, maxRows: 1000, batchSize: 10, runID: "apply-2"}
	res2, err := runCorrection(context.Background(), db, cfg2, wf)
	if err != nil {
		t.Fatal(err)
	}
	if res2.Scanned != 0 {
		t.Fatalf("apply2 scanned=%d, want 0 (幂等)", res2.Scanned)
	}
	if res2.Updated != 0 {
		t.Fatalf("apply2 updated=%d, want 0", res2.Updated)
	}
	var audit2 int64
	db.Table("prediction_outcome_label_corrections").Count(&audit2)
	if audit2 != n {
		t.Fatalf("apply2 后审计行数=%d, want %d (不应增长)", audit2, n)
	}
}

// TestApplyExcludesAlreadyCorrected 证明：apply 前已为 window_1h 的行被 SELECT 过滤排除，
// 不会进入扫描/写入，也不产生审计记录（审计只对应成功更新）。
func TestApplyExcludesAlreadyCorrected(t *testing.T) {
	db := testDB(t)
	const n = 25
	seedRows(t, db, n)

	// 模拟他人已先行修正第 1 行。
	if err := db.Exec(`UPDATE prediction_outcomes SET label_rule = 'window_1h' WHERE prediction_id = ?`, 1).Error; err != nil {
		t.Fatal(err)
	}

	wf := func(string, string, int64) (bool, *int64, error) { return false, nil, nil }

	cfg := runConfig{apply: true, actor: "tester", reason: "skip", epoch: 0, maxRows: 1000, batchSize: 10, runID: "apply-skip"}
	res, err := runCorrection(context.Background(), db, cfg, wf)
	if err != nil {
		t.Fatal(err)
	}
	if res.Scanned != n-1 {
		t.Fatalf("scanned=%d, want %d (第 1 行被 SELECT 排除)", res.Scanned, n-1)
	}
	if res.Updated != n-1 {
		t.Fatalf("updated=%d, want %d", res.Updated, n-1)
	}
	if res.SkippedConcurrent != 0 {
		t.Fatalf("skipped=%d, want 0 (排除发生在 SELECT，非批内跳过)", res.SkippedConcurrent)
	}
	var audit int64
	db.Table("prediction_outcome_label_corrections").Count(&audit)
	if audit != n-1 {
		t.Fatalf("审计行数=%d, want %d (排除行无审计)", audit, n-1)
	}
	// 审计行数与成功写入数一致：审计只对应成功更新。
	if audit != res.Updated {
		t.Fatalf("审计行数 %d 与成功写入数 %d 不一致", audit, res.Updated)
	}
}

// TestApplyTransactionFailureKeepsRunCommitted 证明：某批事务失败后，运行表只记录
// 已提交批次的计数，未提交批次的计数不写入。
func TestApplyTransactionFailureKeepsRunCommitted(t *testing.T) {
	db := testDB(t)
	const n = 25
	seedRows(t, db, n)

	// 注入会在第 2 批（prediction_id=11，fault+3 命中）抛错的 windowFault。
	wf := func(deviceCode, stationCode string, windowEndMS int64) (bool, *int64, error) {
		if windowEndMS == seedBaseMS+int64(11)*600000 {
			return false, nil, fmt.Errorf("injected failure at 11")
		}
		if deviceCode == "fault" {
			o := windowEndMS + 1000
			return true, &o, nil
		}
		return false, nil, nil
	}

	cfg := runConfig{apply: true, actor: "tester", reason: "fail", epoch: 0, maxRows: 1000, batchSize: 10, runID: "apply-fail"}
	res, err := runCorrection(context.Background(), db, cfg, wf)
	if err == nil {
		t.Fatal("expected error from injected failure")
	}
	// 第 1 批（id 1..10）已提交：全部 updated；第 2 批失败回滚，不计入。
	if res.Updated != 10 {
		t.Fatalf("committed updated=%d, want 10 (失败批次未提交)", res.Updated)
	}

	var status string
	var scanned, updated int
	row := db.Raw(`SELECT status, scanned, updated FROM prediction_outcome_label_correction_runs WHERE run_id = ?`, "apply-fail").Row()
	if err := row.Scan(&status, &scanned, &updated); err != nil {
		t.Fatal(err)
	}
	if status != "failed" {
		t.Fatalf("run status=%q, want failed", status)
	}
	if updated != 10 {
		t.Fatalf("run.updated=%d, want 10 (未提交批次计数未写入运行表)", updated)
	}

	// 失败批次的行（id 11..25）label_rule 仍为 legacy（回滚）。
	var legacy int64
	db.Table("prediction_outcomes").Where("label_rule <> 'window_1h'").Count(&legacy)
	if legacy != 15 {
		t.Fatalf("失败回滚后仍有 %d 行非 window_1h, want 15", legacy)
	}
	var audit int64
	db.Table("prediction_outcome_label_corrections").Count(&audit)
	if audit != 10 {
		t.Fatalf("审计行数=%d, want 10 (失败批次审计随事务回滚)", audit)
	}
}
