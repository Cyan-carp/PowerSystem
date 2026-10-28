//go:build tr_s04 && windows

package api

import (
	"context"
	"encoding/json"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"os"
	"path/filepath"
	"powersystem/backend/internal/model"
	"strings"
	"testing"
	"time"
)

type s04QueryCapture struct {
	logger.Interface
	query string
}

func (c *s04QueryCapture) Trace(ctx context.Context, begin time.Time, fc func() (string, int64), err error) {
	q, _ := fc()
	if strings.HasPrefix(q, "WITH RECURSIVE heads AS") {
		c.query = q
	}
}
func s04IndexRows(v any) float64 {
	switch x := v.(type) {
	case []any:
		sum := 0.0
		for _, i := range x {
			sum += s04IndexRows(i)
		}
		return sum
	case map[string]any:
		sum := 0.0
		node, _ := x["Node Type"].(string)
		if strings.Contains(node, "Index") {
			rows, _ := x["Actual Rows"].(float64)
			loops, _ := x["Actual Loops"].(float64)
			sum = rows * loops
		}
		for _, k := range []string{"Plan", "Plans"} {
			if child, ok := x[k]; ok {
				sum += s04IndexRows(child)
			}
		}
		return sum
	}
	return 0
}

func TestTRS04HeadSelectionPlan(t *testing.T) {
	f := s04new(t)
	d := f.device(1)
	base := time.Now().Add(-17 * time.Hour).UnixMilli()
	sample := f.sample(d, "scale", 1, base)
	payload, _ := json.Marshal(sample)
	// Selection-only scale fixture: these are pending, unconfirmed synthetic
	// receipts. They are never counted as TD/performance/business success.
	s04must(t, f.db.Exec(`INSERT INTO telemetry_inbox(run_id,device_id,seq,ts_ms,payload,received_at) SELECT 'scale',?,g,?::bigint+g*5000,?::jsonb||jsonb_build_object('seq',g,'ts_ms',?::bigint+g*5000),to_timestamp((?::bigint+g*5000)/1000.0) FROM generate_series(1,12000)g`, d.ID, base, string(payload), base, base).Error)
	high := f.item(f.device(2), "scale", 1, time.Now().UnixMilli())
	s04must(t, f.db.Exec("ANALYZE telemetry_inbox").Error)
	capture := &s04QueryCapture{Interface: logger.Discard}
	f.s.db = f.db.Session(&gorm.Session{Logger: capture})
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	heads, e := f.s.inboxHeads(ctx, 0, 0, 500)
	s04must(t, e)
	if len(heads) != 2 || heads[1].ID != high.ID || capture.query == "" {
		t.Fatal("heads/query not observed", len(heads))
	}
	queries := map[string]string{"actual_new": capture.query, "s03_distinct": `SELECT DISTINCT ON(device_id) * FROM telemetry_inbox WHERE processed_at IS NULL AND device_id>0 ORDER BY device_id,id LIMIT 500`}
	plans := map[string]any{}
	work := map[string]float64{}
	for name, q := range queries {
		var raw string
		s04must(t, f.db.WithContext(ctx).Raw("EXPLAIN(ANALYZE,BUFFERS,FORMAT JSON) "+q).Row().Scan(&raw))
		var v any
		s04must(t, json.Unmarshal([]byte(raw), &v))
		plans[name] = v
		work[name] = s04IndexRows(v)
	}
	if work["actual_new"] > 10 || work["s03_distinct"] < 12000 {
		t.Fatal("index skip work not demonstrated", work)
	}
	var pending int64
	f.db.Model(&model.Inbox{}).Where("processed_at IS NULL").Count(&pending)
	if pending != 12001 {
		t.Fatal("selection mutated ledger")
	}
	data, _ := json.MarshalIndent(map[string]any{"pending": pending, "distinct_devices": 2, "index_rows": work, "plans": plans}, "", "  ")
	s04must(t, os.WriteFile(filepath.Join(os.Getenv("TR_S04_EVIDENCE"), os.Getenv("TR_S04_RUN")+"-head-plans.json"), data, 0644))
	t.Logf("actual new selection vs S03 DISTINCT on 12000+1 pending / two devices, index work=%v; all rows retained; no TD capacity claim", work)
}
