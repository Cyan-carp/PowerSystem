package api

import (
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"

	"powersystem/backend/internal/model"
)

type feedbackRow struct {
	ID           int64      `json:"id"`
	PredictionID int64      `json:"prediction_id"`
	ActorName    string     `json:"actor_name"`
	Status       string     `json:"status"`
	OccurredAt   *time.Time `json:"occurred_at"`
	Note         string     `json:"note"`
	CreatedAt    time.Time  `json:"created_at"`
}

type feedbackInput struct {
	RequestID  string     `json:"request_id"`
	PreviousID *int64     `json:"previous_id"`
	Status     string     `json:"status"`
	OccurredAt *time.Time `json:"occurred_at"`
	Note       string     `json:"note"`
}

const feedbackColumns = `SELECT f.id,f.prediction_id,u.username AS actor_name,f.status,f.occurred_at,f.note,f.created_at
FROM prediction_feedback f JOIN users u ON u.id=f.actor_id`

func feedbackProblem(input feedbackInput, prediction model.PredictionRecord, now time.Time) string {
	start := time.UnixMilli(prediction.WindowEndMS)
	end := start.Add(time.Duration(model.PredictionHorizonMS) * time.Millisecond)
	switch input.Status {
	case "confirmed_fault":
		if input.OccurredAt == nil || input.OccurredAt.After(now) {
			return "请填写有效且已发生的故障时间"
		}
	case "not_occurred":
		if now.Before(end) {
			return "观察窗口结束后才能标记未发生"
		}
	case "watching":
	default:
		return "请选择有效反馈状态"
	}
	if input.Status != "confirmed_fault" && input.OccurredAt != nil {
		return "仅确认故障时填写实际发生时间"
	}
	return ""
}

func (s *Server) predictionFeedback(c *gin.Context) {
	predictionID, valid := id(c)
	if !valid {
		return
	}
	db := s.requestDB(c)
	if c.Request.Method == http.MethodGet {
		var prediction model.PredictionRecord
		if err := db.Select("id", "window_end_ms").First(&prediction, predictionID).Error; err != nil {
			recordTargetError(c, err, "预测记录不存在")
			return
		}
		before, valid := recordCursor(c)
		if !valid {
			return
		}
		rows := make([]feedbackRow, 0)
		if err := db.Raw(feedbackColumns+" WHERE f.prediction_id=? AND f.id<? ORDER BY f.id DESC LIMIT 51", predictionID, before).Scan(&rows).Error; err != nil {
			fail(c, 503, 50301, "反馈记录暂不可读")
			return
		}
		var next *int64
		if len(rows) > 50 {
			rows, next = rows[:50], &rows[49].ID
		}
		ok(c, gin.H{"list": rows, "next_before_id": next, "window_ends_at_ms": prediction.WindowEndMS + model.PredictionHorizonMS})
		return
	}
	var input feedbackInput
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 16<<10)
	if c.ShouldBindJSON(&input) != nil || !agentRunUUID.MatchString(input.RequestID) {
		fail(c, 400, 40001, "反馈格式不正确")
		return
	}
	input.Note = strings.TrimSpace(input.Note)
	if !recordText(input.Note) {
		fail(c, 400, 40001, "请填写 1–2000 字的说明")
		return
	}
	if input.OccurredAt != nil {
		at := input.OccurredAt.UTC().Truncate(time.Microsecond)
		input.OccurredAt = &at
	}
	actor := c.GetInt64("user_id")
	var row feedbackRow
	problem := ""
	conflict := errors.New("feedback conflict")
	invalid := errors.New("invalid feedback")
	err := db.Transaction(func(tx *gorm.DB) error {
		var prediction model.PredictionRecord
		// Serialize new revisions by prediction. Look up retries before checking the revision.
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Select("id", "window_end_ms").First(&prediction, predictionID).Error; err != nil {
			return err
		}
		lookup := func() error {
			return tx.Raw(feedbackColumns+" WHERE f.actor_id=? AND f.request_id=?", actor, input.RequestID).Scan(&row).Error
		}
		matches := func() bool {
			sameTime := row.OccurredAt == nil && input.OccurredAt == nil || row.OccurredAt != nil && input.OccurredAt != nil && row.OccurredAt.Equal(*input.OccurredAt)
			return row.PredictionID == predictionID && row.Status == input.Status && row.Note == input.Note && sameTime
		}
		if err := lookup(); err != nil {
			return err
		}
		if row.ID != 0 {
			if matches() {
				return nil
			}
			return conflict
		}
		if problem = feedbackProblem(input, prediction, time.Now()); problem != "" {
			return invalid
		}
		var latest int64
		if err := tx.Raw(`SELECT COALESCE((SELECT id FROM prediction_feedback WHERE prediction_id=? ORDER BY id DESC LIMIT 1),0)`, predictionID).Scan(&latest).Error; err != nil {
			return err
		}
		if (input.PreviousID == nil && latest != 0) || (input.PreviousID != nil && *input.PreviousID != latest) {
			return conflict
		}
		if err := tx.Exec(`INSERT INTO prediction_feedback(prediction_id,actor_id,request_id,status,occurred_at,note)
VALUES(?,?,?,?,?,?) ON CONFLICT(actor_id,request_id) DO NOTHING`, predictionID, actor, input.RequestID, input.Status, input.OccurredAt, input.Note).Error; err != nil {
			return err
		}
		if err := lookup(); err != nil {
			return err
		}
		if !matches() {
			return conflict
		}
		return nil
	})
	switch {
	case err == nil:
		ok(c, row)
	case errors.Is(err, invalid):
		fail(c, 400, 40001, problem)
	case errors.Is(err, conflict):
		fail(c, 409, 40901, "反馈已变化或提交编号冲突，请刷新核对")
	default:
		recordTargetError(c, err, "预测记录不存在")
	}
}

func (s *Server) humanReview(c *gin.Context) {
	type reviewRow struct {
		Feedback     feedbackRow `gorm:"embedded" json:"-"`
		DeviceCode   string      `json:"device_code"`
		ModelVersion string      `json:"model_version"`
		WindowEndMS  int64       `json:"window_end_ms"`
		StatusCount  int64       `json:"-"`
	}
	// Include all three totals even when one status has no rows in the recent list.
	rows := make([]reviewRow, 0)
	err := s.requestDB(c).Raw(`WITH latest AS (
 SELECT DISTINCT ON (prediction_id) * FROM prediction_feedback ORDER BY prediction_id,id DESC
), ranked AS (
 SELECT *,count(*) OVER(PARTITION BY status) AS status_count,
 row_number() OVER(PARTITION BY status ORDER BY id DESC) AS status_rank FROM latest
)
SELECT f.id,f.prediction_id,u.username AS actor_name,f.status,f.occurred_at,f.note,f.created_at,
 d.device_code,p.model_version,p.window_end_ms,f.status_count
FROM ranked f JOIN users u ON u.id=f.actor_id JOIN prediction_records p ON p.id=f.prediction_id
JOIN devices d ON d.id=p.device_id WHERE f.status_rank<=20 ORDER BY f.id DESC`).Scan(&rows).Error
	if err != nil {
		fail(c, 503, 50301, "人工复核统计暂不可读")
		return
	}
	counts := map[string]int64{"confirmed_fault": 0, "not_occurred": 0, "watching": 0}
	for _, row := range rows {
		counts[row.Feedback.Status] = row.StatusCount
	}
	if len(rows) > 20 {
		rows = rows[:20]
	}
	type reviewItem struct {
		feedbackRow
		DeviceCode   string `json:"device_code"`
		ModelVersion string `json:"model_version"`
		WindowEndMS  int64  `json:"window_end_ms"`
	}
	items := make([]reviewItem, 0, len(rows))
	for _, row := range rows {
		items = append(items, reviewItem{row.Feedback, row.DeviceCode, row.ModelVersion, row.WindowEndMS})
	}
	ok(c, gin.H{"counts": counts, "list": items})
}
