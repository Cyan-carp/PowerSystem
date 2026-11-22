package api

import (
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"powersystem/backend/internal/model"
)

type fieldRecord struct {
	ID        int64     `json:"id"`
	DeviceID  int64     `json:"device_id"`
	AlarmID   *int64    `json:"alarm_id"`
	ActorID   int64     `json:"actor_id"`
	ActorName string    `json:"actor_name"`
	Symptoms  string    `json:"symptoms"`
	Measures  string    `json:"measures"`
	Result    string    `json:"result"`
	CreatedAt time.Time `json:"created_at"`
}

const recordColumns = `SELECT f.id, f.device_id,f.alarm_id,f.actor_id,u.username AS actor_name,f.symptoms,f.measures,f.result,f.created_at
FROM field_records f JOIN users u ON u.id=f.actor_id`

func (s *Server) deviceFieldRecords(c *gin.Context) { s.fieldRecords(c, false) }
func (s *Server) alarmFieldRecords(c *gin.Context)  { s.fieldRecords(c, true) }

func (s *Server) fieldRecords(c *gin.Context, byAlarm bool) {
	targetID, valid := id(c)
	if !valid {
		return
	}
	db := s.requestDB(c)
	deviceID, key := targetID, "f.device_id"
	var alarmID *int64
	if byAlarm {
		var alarm model.AlarmRecord
		if err := db.First(&alarm, targetID).Error; err != nil {
			recordTargetError(c, err, "告警不存在")
			return
		}
		deviceID, key, alarmID = alarm.DeviceID, "f.alarm_id", &targetID
	}
	var device model.Device
	query := db.Select("id")
	if !byAlarm || c.Request.Method == http.MethodPost {
		query = query.Where("deleted_at IS NULL")
	}
	if err := query.First(&device, deviceID).Error; err != nil {
		recordTargetError(c, err, "设备不存在")
		return
	}
	if c.Request.Method == http.MethodGet {
		before, valid := recordCursor(c)
		if !valid {
			return
		}
		rows := make([]fieldRecord, 0)
		if err := db.Raw(recordColumns+" WHERE "+key+"=? AND f.id<? ORDER BY f.id DESC LIMIT 51", targetID, before).Scan(&rows).Error; err != nil {
			fail(c, 503, 50301, "现场记录暂不可读")
			return
		}
		var next *int64
		if len(rows) > 50 {
			rows, next = rows[:50], &rows[49].ID
		}
		ok(c, gin.H{"list": rows, "next_before_id": next})
		return
	}

	var input struct {
		RequestID string `json:"request_id"`
		Symptoms  string `json:"symptoms"`
		Measures  string `json:"measures"`
		Result    string `json:"result"`
	}
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 64<<10)
	if c.ShouldBindJSON(&input) != nil || !agentRunUUID.MatchString(input.RequestID) {
		fail(c, 400, 40001, "现场记录格式不正确")
		return
	}
	input.Symptoms, input.Measures, input.Result = strings.TrimSpace(input.Symptoms), strings.TrimSpace(input.Measures), strings.TrimSpace(input.Result)
	if !recordText(input.Symptoms) || !recordText(input.Measures) || !recordText(input.Result) {
		fail(c, 400, 40001, "每项需填写 1–2000 字")
		return
	}
	actor := c.GetInt64("user_id")
	if err := db.Exec(`INSERT INTO field_records(device_id,alarm_id,actor_id,request_id,symptoms,measures,result)
VALUES(?,?,?,?,?,?,?) ON CONFLICT(actor_id,request_id) DO NOTHING`, deviceID, alarmID, actor, input.RequestID, input.Symptoms, input.Measures, input.Result).Error; err != nil {
		fail(c, 503, 50301, "现场记录保存失败，请重试")
		return
	}
	var row fieldRecord
	if err := db.Raw(recordColumns+" WHERE f.actor_id=? AND f.request_id=?", actor, input.RequestID).Scan(&row).Error; err != nil || row.ID == 0 {
		fail(c, 503, 50301, "保存结果暂不可读，请用原内容重试")
		return
	}
	sameAlarm := row.AlarmID == nil && alarmID == nil || row.AlarmID != nil && alarmID != nil && *row.AlarmID == *alarmID
	if row.DeviceID != deviceID || !sameAlarm || row.Symptoms != input.Symptoms || row.Measures != input.Measures || row.Result != input.Result {
		fail(c, 409, 40901, "提交编号对应另一条记录")
		return
	}
	ok(c, row)
}

func recordText(text string) bool {
	return text != "" && !strings.ContainsRune(text, 0) && utf8.RuneCountInString(text) <= 2000
}

func recordTargetError(c *gin.Context, err error, missing string) {
	if errors.Is(err, gorm.ErrRecordNotFound) {
		fail(c, 404, 40401, missing)
	} else {
		fail(c, 503, 50301, "记录来源暂不可读")
	}
}

func recordCursor(c *gin.Context) (int64, bool) {
	raw := c.Query("before_id")
	if raw == "" {
		return 1<<63 - 1, true
	}
	before, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || before <= 0 {
		fail(c, 400, 40001, "分页参数错误")
		return 0, false
	}
	return before, true
}
