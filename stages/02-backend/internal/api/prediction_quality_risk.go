package api

// 高风险组实际命中率：按预测概率从高到低切成若干段，看每段里真实故障的占比。
//
// 为什么不沿用校准曲线：正样本率只有 0.06%，等宽分桶后 0–90% 的每一桶都在地面上，
// 曲线贴底是正确结果但读起来像「模型废了」。换个问法——「概率最高的前 1% 里，
// 有多少真的出了故障」——同样一批数据，能直接回答「模型有没有把风险排前面」。

import (
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
)

// riskBandPcts 是要切的分位。1% / 2% / 5% / 10% / 20% 覆盖「最该关注的那一小撮」，
// 50% 用来对照「如果只信模型前一半」的收益。
var riskBandPcts = []float64{0.01, 0.02, 0.05, 0.10, 0.20, 0.50}

type riskBand struct {
	Pct     float64 `json:"pct"`
	Label   string  `json:"label"`
	Samples int64   `json:"samples"`
	Faults  int64   `json:"faults"`
	// HitRate 是段内实际故障率（百分数），MinProb 是这段的入场概率
	HitRate float64 `json:"hit_rate"`
	MinProb float64 `json:"min_prob"`
	// Lift 是段内命中率相对全局故障率的倍数，等于 1 说明和随机挑没区别
	Lift float64 `json:"lift"`
}

// predictionQualityRiskBands GET /predictions/quality/risk-bands?days=7
func (s *Server) predictionQualityRiskBands(c *gin.Context) {
	days, err := strconv.Atoi(c.DefaultQuery("days", "7"))
	if err != nil || days < 1 || days > 90 {
		fail(c, 400, 40001, "invalid days")
		return
	}
	start := time.Now().Add(-time.Duration(days) * 24 * time.Hour).UnixMilli()
	if qualityEpochMS > start {
		start = qualityEpochMS
	}
	ctx := c.Request.Context()

	var totals struct {
		Samples int64
		Faults  int64
	}
	if err := s.db.WithContext(ctx).Raw(`SELECT count(*)::bigint AS samples,
		count(*) FILTER (WHERE actual_fault)::bigint AS faults
		FROM prediction_outcomes WHERE window_end_ms >= ?`, start).Scan(&totals).Error; err != nil {
		fail(c, 503, 50000, "risk bands query failed")
		return
	}
	if totals.Samples == 0 {
		ok(c, gin.H{"items": []any{}, "samples": 0, "faults": 0, "baseline_rate": 0})
		return
	}
	baseline := float64(totals.Faults) * 100 / float64(totals.Samples)

	var rows []struct {
		Pct     float64
		Samples int64
		Faults  int64
		MinProb float64
	}
	// 一条 SQL 切完所有分段：先按概率降序编号，再对每个分位取「排名 <= 总数*pct」的前缀。
	if err := s.db.WithContext(ctx).Raw(`WITH ranked AS (
			SELECT probability, actual_fault,
			       row_number() OVER (ORDER BY probability DESC) AS rn,
			       count(*) OVER () AS total
			FROM prediction_outcomes WHERE window_end_ms >= ?
		), bands(pct) AS (
			VALUES (?::float8),(?::float8),(?::float8),(?::float8),(?::float8),(?::float8)
		)
		SELECT b.pct,
		       count(r.rn)::bigint AS samples,
		       count(*) FILTER (WHERE r.actual_fault)::bigint AS faults,
		       coalesce(min(r.probability), 0)::float8 AS min_prob
		FROM bands AS b
		LEFT JOIN ranked AS r ON r.rn <= ceil(r.total * b.pct)
		GROUP BY b.pct ORDER BY b.pct`,
		start, riskBandPcts[0], riskBandPcts[1], riskBandPcts[2],
		riskBandPcts[3], riskBandPcts[4], riskBandPcts[5]).Scan(&rows).Error; err != nil {
		fail(c, 503, 50000, "risk bands query failed")
		return
	}

	items := make([]riskBand, 0, len(rows))
	for _, row := range rows {
		hit := 0.0
		if row.Samples > 0 {
			hit = float64(row.Faults) * 100 / float64(row.Samples)
		}
		lift := 0.0
		if baseline > 0 {
			lift = hit / baseline
		}
		items = append(items, riskBand{
			Pct: row.Pct, Label: pctLabel(row.Pct), Samples: row.Samples, Faults: row.Faults,
			HitRate: mathRound(hit, 2), MinProb: mathRound(row.MinProb, 4), Lift: mathRound(lift, 1),
		})
	}
	ok(c, gin.H{"items": items, "samples": totals.Samples, "faults": totals.Faults, "baseline_rate": mathRound(baseline, 3)})
}

func pctLabel(pct float64) string {
	switch pct {
	case 0.01:
		return "前 1%"
	case 0.02:
		return "前 2%"
	case 0.05:
		return "前 5%"
	case 0.10:
		return "前 10%"
	case 0.20:
		return "前 20%"
	case 0.50:
		return "前 50%"
	}
	return "—"
}

// mathRound 保留 n 位小数，避免前端拿到一长串浮点尾巴。
func mathRound(value float64, n int) float64 {
	scale := 1.0
	for i := 0; i < n; i++ {
		scale *= 10
	}
	if value >= 0 {
		return float64(int64(value*scale+0.5)) / scale
	}
	return float64(int64(value*scale-0.5)) / scale
}

var _ = http.StatusOK
