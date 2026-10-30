package api

import (
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"
	"golang.org/x/crypto/bcrypt"
	"gorm.io/gorm"

	"powersystem/backend/internal/service"
)

// 演示台是网页与 demo-alarm.py 之间的等价物：同样调 service 里的事务函数，
// 只是把“进容器敲命令”换成“点按钮”。它没有任何新的业务逻辑，也不碰真实告警——
// 所有写操作都先过 IsManualDemo，非演示告警一律拒绝。
//
// 口令放在 api 容器的 /data 卷里（api-state 是具名卷，重建容器不丢），
// 不新增环境变量、不改 compose——发布时只需要重建 api 镜像。
const demoConsoleDefaultFile = "/data/demo-console.json"

type demoConsoleConf struct {
	Enabled      bool   `json:"enabled"`
	PasswordHash string `json:"password_hash"`
	Actor        string `json:"actor"`
}

func demoConsoleConfig() (demoConsoleConf, error) {
	var conf demoConsoleConf
	path := strings.TrimSpace(os.Getenv("DEMO_CONSOLE_FILE"))
	if path == "" {
		path = demoConsoleDefaultFile
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return conf, err
	}
	if err := json.Unmarshal(raw, &conf); err != nil {
		return conf, err
	}
	if !conf.Enabled || strings.TrimSpace(conf.PasswordHash) == "" || strings.TrimSpace(conf.Actor) == "" {
		return conf, errors.New("demo_console_disabled")
	}
	return conf, nil
}

// 口令只有一把，所以失败次数必须自己数：同一来源 5 分钟内错 8 次就锁。
var demoConsoleAttempts = struct {
	sync.Mutex
	byIP map[string][]time.Time
}{byIP: map[string][]time.Time{}}

func demoConsoleAllowed(ip string) bool {
	demoConsoleAttempts.Lock()
	defer demoConsoleAttempts.Unlock()
	now := time.Now()
	kept := demoConsoleAttempts.byIP[ip][:0]
	for _, t := range demoConsoleAttempts.byIP[ip] {
		if now.Sub(t) < 5*time.Minute {
			kept = append(kept, t)
		}
	}
	demoConsoleAttempts.byIP[ip] = kept
	return len(kept) < 8
}

func demoConsoleRecordFailure(ip string) {
	demoConsoleAttempts.Lock()
	defer demoConsoleAttempts.Unlock()
	demoConsoleAttempts.byIP[ip] = append(demoConsoleAttempts.byIP[ip], time.Now())
}

func demoConsoleReset(ip string) {
	demoConsoleAttempts.Lock()
	defer demoConsoleAttempts.Unlock()
	delete(demoConsoleAttempts.byIP, ip)
}

func (s *Server) demoConsoleLogin(c *gin.Context) {
	conf, err := demoConsoleConfig()
	if err != nil {
		fail(c, 503, 50301, "演示入口未启用，请联系维护人员")
		return
	}
	ip := c.ClientIP()
	if !demoConsoleAllowed(ip) {
		fail(c, 429, 42901, "密码错误次数过多，请 5 分钟后再试")
		return
	}
	var body struct {
		Password string `json:"password"`
	}
	if c.ShouldBindJSON(&body) != nil || utf8.RuneCountInString(body.Password) > 128 {
		fail(c, 400, 40001, "请求格式不正确")
		return
	}
	if bcrypt.CompareHashAndPassword([]byte(conf.PasswordHash), []byte(body.Password)) != nil {
		demoConsoleRecordFailure(ip)
		fail(c, 401, 40101, "密码不正确")
		return
	}
	ttl := 8 * time.Hour
	token, err := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{
		"scope": "demo-console",
		"exp":   time.Now().Add(ttl).Unix(),
		"iat":   time.Now().Unix(),
	}).SignedString([]byte(s.cfg.JWTSecret))
	if err != nil {
		fail(c, 500, 50000, "登录失败")
		return
	}
	demoConsoleReset(ip)
	ok(c, gin.H{"token": token, "actor": conf.Actor, "expires_in": int(ttl.Seconds())})
}

// 演示台令牌与平台令牌用同一把密钥签名，靠 scope 区分：反过来也进不来，
// 因为 authorize 要求 sub 是真实用户，演示台令牌没有 sub。
func (s *Server) demoConsoleAuth(c *gin.Context) {
	header := c.GetHeader("Authorization")
	if !strings.HasPrefix(header, "Bearer ") {
		fail(c, 401, 40101, "请输入演示台密码")
		return
	}
	token, err := jwt.Parse(strings.TrimPrefix(header, "Bearer "), func(t *jwt.Token) (any, error) {
		if t.Method.Alg() != jwt.SigningMethodHS256.Alg() {
			return nil, errors.New("unexpected signing method")
		}
		return []byte(s.cfg.JWTSecret), nil
	})
	if err != nil || !token.Valid {
		fail(c, 401, 40101, "密码会话已失效，请重新输入")
		return
	}
	claims, valid := token.Claims.(jwt.MapClaims)
	if !valid {
		fail(c, 401, 40101, "密码会话已失效，请重新输入")
		return
	}
	if scope, _ := claims["scope"].(string); scope != "demo-console" {
		fail(c, 401, 40101, "密码会话已失效，请重新输入")
		return
	}
	c.Next()
}

func (s *Server) demoConsoleDevices(c *gin.Context) {
	keyword := strings.TrimSpace(c.Query("keyword"))
	if utf8.RuneCountInString(keyword) > 64 {
		fail(c, 400, 40001, "关键字过长")
		return
	}
	query := s.db.WithContext(c.Request.Context()).Table("devices").Where("deleted_at IS NULL")
	if keyword != "" {
		query = query.Where(
			"strpos(lower(name),lower(?)) > 0 OR strpos(lower(device_code),lower(?)) > 0 OR strpos(lower(station_code),lower(?)) > 0",
			keyword, keyword, keyword,
		)
	}
	var rows []struct {
		ID          int64  `json:"id"`
		DeviceCode  string `json:"device_code"`
		Name        string `json:"name"`
		StationCode string `json:"station_code"`
	}
	if err := query.Select("id, device_code, name, station_code").Order("device_code").Limit(20).Scan(&rows).Error; err != nil {
		fail(c, 500, 50000, "设备查询失败")
		return
	}
	ok(c, gin.H{"list": rows, "total": len(rows)})
}

// demoAlarmSelect 与 demo-alarm.py 的 status 输出同源：告警行 + 最近一条解读。
const demoAlarmSelect = `
SELECT a.id, a.level, a.status, a.rule_version, a.triggered_at, a.acked_at, a.recovered_at,
       COALESCE(a.close_reason,'') AS close_reason, d.device_code, d.name AS device_name, d.station_code,
       COALESCE(u.username,'') AS acked_by,
       COALESCE(i.event_key,'') AS event_key, COALESCE(i.task_status,'') AS task_status,
       COALESCE(i.model_calls,0) AS model_calls, COALESCE(i.reason,'') AS reason,
       i.result AS result, i.updated_at AS interpretation_updated_at
FROM alarm_records a
JOIN devices d ON d.id = a.device_id
LEFT JOIN users u ON u.id = a.acked_by
LEFT JOIN LATERAL (
  SELECT event_key, task_status, model_calls, reason, result, updated_at
  FROM agent_interpretations WHERE alarm_id = a.id ORDER BY id DESC LIMIT 1
) i ON TRUE`

type demoAlarmRow struct {
	ID                      int64           `json:"id"`
	Level                   string          `json:"level"`
	Status                  string          `json:"status"`
	RuleVersion             string          `json:"rule_version"`
	TriggeredAt             time.Time       `json:"triggered_at"`
	AckedAt                 *time.Time      `json:"acked_at"`
	RecoveredAt             *time.Time      `json:"recovered_at"`
	CloseReason             string          `json:"close_reason"`
	DeviceCode              string          `json:"device_code"`
	DeviceName              string          `json:"device_name"`
	StationCode             string          `json:"station_code"`
	AckedBy                 string          `json:"acked_by"`
	EventKey                string          `json:"event_key"`
	TaskStatus              string          `json:"task_status"`
	ModelCalls              int             `json:"model_calls"`
	Reason                  string          `json:"reason"`
	Result                  json.RawMessage `json:"-"`
	InterpretationUpdatedAt *time.Time      `json:"interpretation_updated_at"`
}

func (s *Server) demoConsoleList(c *gin.Context) {
	scope := c.DefaultQuery("scope", "recent")
	sql := demoAlarmSelect + `
WHERE a.origin = 'simulation' AND a.metric = 'simulation_scenario' AND a.scenario_id IS NULL
  AND a.rule_version IN ('manual-api-demo/v1','manual-fixed-demo/v1')`
	if scope == "active" {
		sql += " AND a.recovered_at IS NULL"
	}
	sql += " ORDER BY (a.recovered_at IS NULL) DESC, a.id DESC LIMIT 20"
	var rows []demoAlarmRow
	if err := s.db.WithContext(c.Request.Context()).Raw(sql).Scan(&rows).Error; err != nil {
		fail(c, 500, 50000, "演示告警查询失败")
		return
	}
	views := make([]gin.H, 0, len(rows))
	for _, row := range rows {
		views = append(views, demoAlarmView(row))
	}
	ok(c, gin.H{"list": views, "total": len(views)})
}

func (s *Server) demoConsoleGet(c *gin.Context) {
	alarmID, valid := id(c)
	if !valid {
		return
	}
	var rows []demoAlarmRow
	if err := s.db.WithContext(c.Request.Context()).Raw(demoAlarmSelect+" WHERE a.id = ?", alarmID).Scan(&rows).Error; err != nil {
		fail(c, 500, 50000, "演示告警查询失败")
		return
	}
	if len(rows) == 0 {
		fail(c, 404, 40401, "告警不存在")
		return
	}
	ok(c, demoAlarmView(rows[0]))
}

func (s *Server) demoConsoleCreate(c *gin.Context) {
	var body struct {
		DeviceCode string `json:"device_code"`
		Level      string `json:"level"`
		Analysis   string `json:"analysis"`
		RequestID  string `json:"request_id"`
	}
	if c.ShouldBindJSON(&body) != nil {
		fail(c, 400, 40001, "请求格式不正确")
		return
	}
	code := strings.TrimSpace(body.DeviceCode)
	requestID := strings.TrimSpace(body.RequestID)
	if requestID == "" {
		requestID = demoConsoleRequestID()
	}
	alarm, changed, err := service.CreateManualDemo(s.requestDB(c), code, requestID, body.Level, body.Analysis)
	if err != nil {
		demoConsoleFailed(c, err)
		return
	}
	if changed {
		s.broadcastAfterWrite(c, service.AlarmEvent{Type: "alarm_created", Data: alarm})
	}
	s.demoConsoleReply(c, alarm.ID, changed)
}

func (s *Server) demoConsoleAck(c *gin.Context) {
	alarmID, valid := id(c)
	if !valid {
		return
	}
	conf, err := demoConsoleConfig()
	if err != nil {
		fail(c, 503, 50301, "演示入口未启用")
		return
	}
	alarm, changed, err := service.AckManualAPIDemo(s.requestDB(c), alarmID, conf.Actor)
	if err != nil {
		demoConsoleFailed(c, err)
		return
	}
	if changed {
		s.broadcastAfterWrite(c, service.AlarmEvent{Type: "alarm_acked", Data: alarm})
	}
	s.demoConsoleReply(c, alarmID, changed)
}

func (s *Server) demoConsoleRecover(c *gin.Context) {
	alarmID, valid := id(c)
	if !valid {
		return
	}
	alarm, changed, err := service.EndManualAPIDemo(s.requestDB(c), alarmID)
	if err != nil {
		demoConsoleFailed(c, err)
		return
	}
	if changed {
		s.broadcastAfterWrite(c, service.AlarmEvent{Type: "alarm_recovered", Data: alarm})
	}
	s.demoConsoleReply(c, alarmID, changed)
}

// 写操作回的是“提交后的那一行”，不是事务里的中间态：页面上看到什么，数据库里就是什么。
func (s *Server) demoConsoleReply(c *gin.Context, alarmID int64, changed bool) {
	var rows []demoAlarmRow
	if err := s.db.WithContext(c.Request.Context()).Raw(demoAlarmSelect+" WHERE a.id = ?", alarmID).Scan(&rows).Error; err != nil || len(rows) == 0 {
		fail(c, 500, 50000, "写入已执行，但读取结果失败，请刷新确认")
		return
	}
	view := demoAlarmView(rows[0])
	view["changed"] = changed
	ok(c, view)
}

var demoConsolePlaceholder = regexp.MustCompile(`\{\{([A-Za-z0-9_]+)\}\}`)

var demoReasonCN = map[string]string{
	"not_configured":            "服务器未配置解读模型",
	"auth_failed":               "模型鉴权失败",
	"quota_exhausted":           "模型额度已用完",
	"answer_truncated":          "模型回答被截断",
	"call_budget_exhausted":     "本次调用的模型预算已用尽",
	"retry_deadline_exceeded":   "重试超时仍未完成",
	"missing_event_evidence":    "缺少事件证据，无法解读",
	"evidence_unavailable":      "证据采集失败",
	"evidence_budget_exceeded":  "证据过多，超出单次上限",
	"circuit_open":              "解读通道熔断中，稍后自动重试",
	"model_state_unavailable":   "解读通道状态读取失败",
	"call_reservation_rejected": "本轮模型调用配额未抢到",
	"rate_limited":              "模型服务限流",
	"model_timeout":             "模型响应超时",
	"model_unreachable":         "模型服务不可达",
	"model_non_json":            "模型返回的不是合法 JSON",
	"model_invalid_envelope":    "模型返回未通过校验",
	"model_empty_choices":       "模型没有返回内容",
	"model_empty_content":       "模型回答为空",
	"model_invalid_finish":      "模型输出被异常中断",
	"model_response_too_large":  "模型回答过长",
	"answer_validation_failed":  "模型回答未通过校验",
	"answer_schema_invalid":     "模型回答格式不符",
	"thinking_mode_mismatch":    "模型思考模式与约定不符",
	"unavailable":               "解读通道暂不可用",
}

var demoTaskStatusCN = map[string]string{
	"pending":   "排队中",
	"running":   "生成中",
	"completed": "已完成",
	"degraded":  "未完成",
}

func demoAlarmView(row demoAlarmRow) gin.H {
	mode := "fixed"
	if row.RuleVersion == service.ManualAPIDemoVersion {
		mode = "model"
	}
	requestID := strings.TrimPrefix(row.EventKey, service.ManualAPIDemoEventPrefix)
	if requestID == row.EventKey {
		requestID = ""
	}
	return gin.H{
		"id":             row.ID,
		"level":          row.Level,
		"status":         row.Status,
		"mode":           mode,
		"rule_version":   row.RuleVersion,
		"device_code":    row.DeviceCode,
		"device_name":    row.DeviceName,
		"station_code":   row.StationCode,
		"triggered_at":   row.TriggeredAt,
		"acked_at":       row.AckedAt,
		"acked_by":       row.AckedBy,
		"recovered_at":   row.RecoveredAt,
		"close_reason":   row.CloseReason,
		"request_id":     requestID,
		"detail_path":    fmt.Sprintf("/alarms?id=%d", row.ID),
		"interpretation": demoInsight(row),
	}
}

// demoInsight 把 v1/v2 两套结果结构压成页面能直接渲染的几行字，
// 避免把前端 lib/interpretation.ts 那套归一化逻辑再抄一遍。
func demoInsight(row demoAlarmRow) gin.H {
	out := gin.H{
		"task_status":      row.TaskStatus,
		"task_status_text": demoTaskStatusCN[row.TaskStatus],
		"model_calls":      row.ModelCalls,
		"reason":           row.Reason,
		"reason_text":      "",
		"status":           "",
		"model":            "",
		"conclusion":       []string{},
		"suggestions":      []string{},
		"limitations":      []string{},
		"facts":            []gin.H{},
		"empty":            true,
	}
	if row.Reason != "" {
		if text, ok := demoReasonCN[row.Reason]; ok {
			out["reason_text"] = text
		} else {
			out["reason_text"] = "解读未完成（" + row.Reason + "）"
		}
	}
	if len(row.Result) == 0 || string(row.Result) == "null" {
		return out
	}
	var payload map[string]any
	if json.Unmarshal(row.Result, &payload) != nil {
		return out
	}
	values := map[string]string{}
	if facts, ok := payload["facts"].([]any); ok {
		list := make([]gin.H, 0, len(facts))
		for _, item := range facts {
			fact, ok := item.(map[string]any)
			if !ok {
				continue
			}
			factID, _ := fact["fact_id"].(string)
			display, _ := fact["display_value"].(string)
			if display == "" {
				display = fmt.Sprint(fact["value"])
			}
			label, _ := fact["label"].(string)
			if factID != "" {
				values[factID] = display
			}
			if len(list) < 12 {
				list = append(list, gin.H{"id": factID, "label": label, "value": display})
			}
		}
		out["facts"] = list
	}
	out["status"], _ = payload["status"].(string)
	out["model"], _ = payload["model"].(string)

	fill := func(key string) []string {
		texts := demoTextLines(payload[key], values)
		if len(texts) == 0 {
			if analysis, ok := payload["analysis"].(map[string]any); ok {
				texts = demoTextLines(analysis[key], values)
			}
		}
		return texts
	}
	conclusion := fill("findings")
	if text, ok := demoBlockText(payload["conclusion"], values); ok {
		conclusion = append([]string{text}, conclusion...)
	}
	out["conclusion"] = conclusion
	out["suggestions"] = fill("suggestions")
	out["limitations"] = fill("limitations")
	out["empty"] = len(conclusion) == 0 && len(fill("suggestions")) == 0 && row.TaskStatus != "pending" && row.TaskStatus != "running"
	return out
}

func demoBlockText(raw any, values map[string]string) (string, bool) {
	block, ok := raw.(map[string]any)
	if !ok {
		return "", false
	}
	text, _ := block["text"].(string)
	text = strings.TrimSpace(demoConsolePlaceholder.ReplaceAllStringFunc(text, func(m string) string {
		return values[demoConsolePlaceholder.FindStringSubmatch(m)[1]]
	}))
	return text, text != ""
}

func demoTextLines(raw any, values map[string]string) []string {
	items, ok := raw.([]any)
	if !ok {
		return nil
	}
	lines := make([]string, 0, len(items))
	for _, item := range items {
		switch v := item.(type) {
		case string:
			if text := strings.TrimSpace(v); text != "" {
				lines = append(lines, text)
			}
		case map[string]any:
			text, _ := v["text"].(string)
			if text == "" {
				text, _ = v["template"].(string)
			}
			text = strings.TrimSpace(demoConsolePlaceholder.ReplaceAllStringFunc(text, func(m string) string {
				return values[demoConsolePlaceholder.FindStringSubmatch(m)[1]]
			}))
			if text != "" {
				lines = append(lines, text)
			}
		}
	}
	return lines
}

func demoConsoleFailed(c *gin.Context, err error) {
	message := err.Error()
	switch {
	case strings.HasPrefix(message, "demo_already_active:"):
		alarmID := strings.TrimPrefix(message, "demo_already_active:")
		fail(c, 409, 40901, "该设备已有活动中的演示告警（#"+alarmID+"），请先恢复它再创建")
	case message == "demo_request_id_options_changed":
		fail(c, 409, 40902, "同一个 request_id 已经用于另一组参数，请换一个标识")
	case message == "invalid_demo_arguments":
		fail(c, 400, 40001, "参数不合法：设备编码为空、级别或解读模式不在允许范围内")
	case message == "invalid_demo_alarm_id":
		fail(c, 400, 40001, "演示告警编号不合法")
	case message == "not_a_manual_api_demo" || message == "not_a_manual_demo":
		fail(c, 400, 40002, "这不是演示告警，演示台不操作真实告警")
	case message == "demo_actor_not_found_or_not_allowed":
		fail(c, 503, 50301, "演示台配置的操作账号不存在或角色不足")
	case errors.Is(err, service.ErrAlarmMaintenance):
		fail(c, 503, 50302, "告警维护开关已打开，写入已暂停")
	case errors.Is(err, service.ErrAlarmAckConflict):
		fail(c, 409, 40903, "这条告警已经确认或已恢复，不能重复确认")
	case errors.Is(err, gorm.ErrRecordNotFound):
		fail(c, 404, 40401, "设备或告警不存在")
	default:
		fail(c, 500, 50000, "数据库操作失败，请查看 api 日志")
	}
}

// request_id 决定幂等：同一条命令重试用同一个值，不会重复建告警、也不会重复消耗模型。
func demoConsoleRequestID() string {
	buf := make([]byte, 8)
	if _, err := rand.Read(buf); err != nil {
		return fmt.Sprintf("demo%d", time.Now().UnixNano())
	}
	return "demo" + fmt.Sprintf("%x", buf)
}
