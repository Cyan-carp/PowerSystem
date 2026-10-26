package api

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/service"
)

const businessPolicy = "v4-s06-local/v1"

type businessGrant struct {
	Audience      string          `json:"aud"`
	Issued        int64           `json:"iat"`
	Expires       int64           `json:"exp"`
	Owner         int64           `json:"owner_id"`
	Run           string          `json:"run_id"`
	Action        string          `json:"action_id"`
	Execution     string          `json:"execution_ref"`
	Capability    string          `json:"capability"`
	Args          json.RawMessage `json:"typed_args"`
	TargetVersion string          `json:"target_version"`
	PlanHash      string          `json:"plan_hash"`
	Policy        string          `json:"policy_version"`
}
type businessReceipt struct {
	ExecutionRef  string `gorm:"primaryKey;type:uuid"`
	OwnerID       int64
	RunID         string `gorm:"type:uuid"`
	ActionID      string `gorm:"type:uuid"`
	Capability    string
	ArgsHash      string
	TargetVersion string
	Result        json.RawMessage `gorm:"type:jsonb"`
	BeforeHash    string
	AfterHash     string
	CreatedAt     time.Time
}

func (businessReceipt) TableName() string { return "business_action_receipts" }
func strictBusinessJSON(raw []byte, v any) error {
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	if err := d.Decode(v); err != nil {
		return err
	}
	if d.Decode(new(any)) != io.EOF {
		return errors.New("invalid JSON")
	}
	return nil
}
func businessJSON(v any) []byte {
	var b bytes.Buffer
	e := json.NewEncoder(&b)
	e.SetEscapeHTML(false)
	_ = e.Encode(v)
	raw := bytes.TrimSpace(b.Bytes())
	raw = bytes.ReplaceAll(raw, []byte(`\u2028`), []byte("\u2028"))
	return bytes.ReplaceAll(raw, []byte(`\u2029`), []byte("\u2029"))
}
func businessHash(v any) string { h := sha256.Sum256(businessJSON(v)); return hex.EncodeToString(h[:]) }
func (s *Server) businessLocal() bool {
	if os.Getenv("V4_PRODUCTION_PROFILE") != "" {
		return s.v4Production()
	}
	if os.Getenv("V4_JOINT_PROFILE") != "" {
		return s.s09Local()
	}
	parsed, err := url.Parse(s.cfg.PostgresDSN)
	if err != nil || parsed.Hostname() != "127.0.0.1" || parsed.Port() != "25436" {
		return false
	}
	if os.Getenv("V4_S06_LOCAL_ENABLED") != "true" || s.cfg.OpsURL != "http://127.0.0.1:28963" || len(s.cfg.MAActorKey) < 32 || len(s.cfg.OpsToken) < 32 {
		return false
	}
	var name string
	if s.db.Raw("SELECT current_database()").Scan(&name).Error != nil {
		return false
	}
	return strings.HasPrefix(name, "v4_s06_") && strings.HasSuffix(name, "_test")
}
func (s *Server) businessInternal(c *gin.Context) bool {
	if !hmac.Equal([]byte(c.GetHeader("X-PowerSystem-Token")), []byte(s.cfg.OpsToken)) || len(s.cfg.OpsToken) < 32 {
		fail(c, 403, 40301, "business service denied")
		return false
	}
	return true
}
func readBusinessBody(c *gin.Context, v any) bool {
	raw, err := io.ReadAll(http.MaxBytesReader(c.Writer, c.Request.Body, 32768))
	if err != nil || strictBusinessJSON(raw, v) != nil {
		fail(c, 400, 40001, "invalid business request")
		return false
	}
	return true
}
func parseBusinessGrant(token, key string) (businessGrant, error) {
	return parseBusinessGrantPolicy(token, key, businessPolicy)
}
func parseBusinessGrantPolicy(token, key, expectedPolicy string) (businessGrant, error) {
	var g businessGrant
	parts := strings.Split(token, ".")
	if len(parts) != 2 || len(token) > 24000 {
		return g, errors.New("invalid grant")
	}
	sig, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return g, err
	}
	m := hmac.New(sha256.New, []byte(key))
	_, _ = m.Write([]byte(parts[0]))
	if !hmac.Equal(sig, m.Sum(nil)) {
		return g, errors.New("invalid grant")
	}
	raw, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return g, err
	}
	if err = strictBusinessJSON(raw, &g); err != nil {
		return g, err
	}
	now := time.Now().Unix()
	if g.Audience != "ma-business-execute/v1" || g.Policy != expectedPolicy || g.Expires <= now || g.Issued > now+5 || g.Expires-g.Issued > 300 || g.Expires <= g.Issued || g.Owner <= 0 || !agentRunUUID.MatchString(g.Run) || !agentRunUUID.MatchString(g.Action) || !agentRunUUID.MatchString(g.Execution) || len(g.PlanHash) != 64 {
		return g, errors.New("invalid binding")
	}
	return g, nil
}
func businessArgs(g businessGrant) (int64, string, int64, map[string]any, error) {
	bad := errors.New("invalid action arguments")
	if g.Capability == "alarm.ack" {
		var a struct {
			ID int64 `json:"alarm_id"`
		}
		if strictBusinessJSON(g.Args, &a) != nil || a.ID <= 0 {
			return 0, "", 0, nil, bad
		}
		return a.ID, "", 0, map[string]any{"alarm_id": a.ID}, nil
	}
	if g.Capability == "device.remark" {
		var a struct {
			ID      int64   `json:"device_id"`
			Remark  *string `json:"remark"`
			Version *int64  `json:"expected_version"`
		}
		if strictBusinessJSON(g.Args, &a) != nil || a.ID <= 0 || a.Remark == nil || a.Version == nil || *a.Version < 0 || !utf8.ValidString(*a.Remark) || utf8.RuneCountInString(*a.Remark) > 1000 {
			return 0, "", 0, nil, bad
		}
		for _, r := range *a.Remark {
			if r < 32 && r != '\n' && r != '\r' && r != '\t' {
				return 0, "", 0, nil, bad
			}
		}
		return a.ID, *a.Remark, *a.Version, map[string]any{"device_id": a.ID, "remark": *a.Remark, "expected_version": *a.Version}, nil
	}
	if g.Capability == "device.remark.read" {
		var a struct {
			ID int64 `json:"device_id"`
		}
		if strictBusinessJSON(g.Args, &a) != nil || a.ID <= 0 {
			return 0, "", 0, nil, bad
		}
		return a.ID, "", 0, map[string]any{"device_id": a.ID}, nil
	}
	return 0, "", 0, nil, bad
}
func alarmBusinessVersion(a model.AlarmRecord) string {
	return businessHash(map[string]any{"status": a.Status, "recovered_at": a.RecoveredAt, "updated_at": a.UpdatedAt})
}
func (s *Server) businessActionSnapshot(c *gin.Context) {
	if !s.businessInternal(c) {
		return
	}
	if !s.businessLocal() {
		fail(c, 403, 40301, "local S06 policy required")
		return
	}
	var input struct {
		Capability string `json:"capability"`
		Target     int64  `json:"target_id"`
	}
	if !readBusinessBody(c, &input) || input.Target <= 0 {
		return
	}
	if input.Capability == "alarm.ack" {
		var a model.AlarmRecord
		if s.db.First(&a, input.Target).Error != nil {
			fail(c, 404, 40401, "alarm not found")
			return
		}
		c.JSON(200, gin.H{"target_version": alarmBusinessVersion(a), "target_summary": "告警 #" + strconv.FormatInt(a.ID, 10) + " · 设备 #" + strconv.FormatInt(a.DeviceID, 10)})
		return
	}
	if input.Capability == "device.remark" || input.Capability == "device.remark.read" {
		var d model.Device
		if s.db.Where("id=? AND deleted_at IS NULL", input.Target).First(&d).Error != nil {
			fail(c, 404, 40401, "device not found")
			return
		}
		c.JSON(200, gin.H{"target_version": strconv.FormatInt(d.RemarkVersion, 10), "target_summary": "设备 #" + strconv.FormatInt(d.ID, 10) + " · " + d.DeviceCode})
		return
	}
	fail(c, 403, 40301, "unregistered capability")
}
func (s *Server) executeBusinessAction(c *gin.Context) {
	if strings.HasPrefix(c.Request.URL.Path, "/internal/") && !s.businessInternal(c) {
		return
	}
	if !s.businessLocal() {
		fail(c, 403, 40301, "local S06 policy required")
		return
	}
	var input struct {
		Grant   string  `json:"grant"`
		Remark  *string `json:"remark,omitempty"`
		Version *int64  `json:"expected_version,omitempty"`
	}
	if !readBusinessBody(c, &input) {
		return
	}
	// The current domain selects the only acceptable policy version. The
	// signature alone never makes a local grant usable in production.
	policy := businessPolicy
	if os.Getenv("V4_PRODUCTION_PROFILE") != "" {
		policy = v4BusinessProductionPolicy
	}
	g, err := parseBusinessGrantPolicy(input.Grant, s.cfg.MAActorKey, policy)
	if err != nil || g.Owner != c.GetInt64("user_id") {
		fail(c, 403, 40301, "invalid business grant")
		return
	}
	target, remark, version, args, err := businessArgs(g)
	if err != nil {
		fail(c, 400, 40001, "invalid typed arguments")
		return
	}
	if c.Request.Method == http.MethodPatch {
		requested, valid := id(c)
		if !valid {
			return
		}
		if g.Capability != "device.remark" || requested != target || input.Remark == nil || input.Version == nil || *input.Remark != remark || *input.Version != version {
			fail(c, 409, 40901, "action parameters changed")
			return
		}
	} else if input.Remark != nil || input.Version != nil {
		fail(c, 400, 40001, "unexpected action arguments")
		return
	}
	hash := businessHash(args)
	var old businessReceipt
	if s.db.First(&old, "execution_ref=?", g.Execution).Error == nil {
		if old.OwnerID != g.Owner || old.RunID != g.Run || old.ActionID != g.Action || old.Capability != g.Capability || old.ArgsHash != hash || old.TargetVersion != g.TargetVersion {
			fail(c, 409, 40901, "receipt binding conflict")
			return
		}
		c.Data(200, "application/json", old.Result)
		return
	}
	// The controller validates the already-consumed approval and grants this exact
	// adapter one claim. A signature alone never authorizes a database mutation.
	raw := businessJSON(map[string]string{"grant": input.Grant})
	req, err := http.NewRequest(http.MethodPost, s.cfg.OpsURL+"/internal/ma-action/v1/claims", bytes.NewReader(raw))
	if err != nil {
		fail(c, 503, 50301, "claim unavailable")
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-PowerSystem-Token", s.cfg.OpsToken)
	req.Header.Set("Authorization", c.GetHeader("Authorization"))
	client := &http.Client{Timeout: 3 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	response, err := client.Do(req)
	if err != nil {
		fail(c, 503, 50301, "claim unconfirmed")
		return
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 4096))
	_ = response.Body.Close()
	if response.StatusCode != 200 {
		fail(c, 403, 40301, "action claim denied")
		return
	}
	var output map[string]any
	var alarm model.AlarmRecord
	conflict := errors.New("target or permission changed")
	err = s.db.Transaction(func(tx *gorm.DB) error {
		var user model.User
		if tx.Clauses(clause.Locking{Strength: "SHARE"}).First(&user, g.Owner).Error != nil || (user.Role != "operator" && user.Role != "admin") {
			return conflict
		}
		if g.Expires <= time.Now().Unix() {
			return conflict
		}
		output = map[string]any{"execution_ref": g.Execution, "action_id": g.Action, "run_id": g.Run, "capability": g.Capability, "status": "succeeded", "target_version": g.TargetVersion, "no_effect": g.Capability == "device.remark.read"}
		before, after := "", ""
		if g.Capability == "alarm.ack" {
			if tx.Clauses(clause.Locking{Strength: "UPDATE"}).First(&alarm, target).Error != nil || alarmBusinessVersion(alarm) != g.TargetVersion || alarm.Status != "unhandled" || alarm.RecoveredAt != nil {
				return conflict
			}
			before = businessHash(alarm)
			timestamp := time.Now().UTC()
			if err := tx.Model(&model.AlarmRecord{}).Where("id=? AND status='unhandled' AND recovered_at IS NULL", target).Updates(map[string]any{"status": "acked", "acked_by": g.Owner, "acked_at": timestamp, "updated_at": timestamp}).Error; err != nil {
				return err
			}
			if err := tx.First(&alarm, target).Error; err != nil {
				return err
			}
			var device model.Device
			if err := tx.First(&device, alarm.DeviceID).Error; err != nil {
				return err
			}
			if err := service.QueueAlarmNotification(tx, device, alarm, "acknowledged"); err != nil {
				return err
			}
			after = businessHash(alarm)
			output["effect_summary"] = "告警已确认；这不代表故障已修复"
			output["new_version"] = alarmBusinessVersion(alarm)
		} else {
			var device model.Device
			if tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id=? AND deleted_at IS NULL", target).First(&device).Error != nil || strconv.FormatInt(device.RemarkVersion, 10) != g.TargetVersion {
				return conflict
			}
			before = businessHash(map[string]any{"remark": device.Remark, "version": device.RemarkVersion})
			if g.Capability == "device.remark" {
				if device.RemarkVersion != version {
					return conflict
				}
				timestamp := time.Now().UTC()
				update := tx.Model(&model.Device{}).Where("id=? AND remark_version=? AND deleted_at IS NULL", target, version).Updates(map[string]any{"remark": remark, "remark_version": version + 1, "remark_updated_by": g.Owner, "remark_updated_at": timestamp})
				if update.Error != nil {
					return update.Error
				}
				if update.RowsAffected != 1 {
					return conflict
				}
				after = businessHash(map[string]any{"remark": remark, "version": version + 1})
				output["new_version"] = strconv.FormatInt(version+1, 10)
				output["effect_summary"] = "设备备注已更新，版本 " + strconv.FormatInt(version+1, 10)
			} else {
				after = before
				output["remark"] = device.Remark
				output["effect_summary"] = "已审查读取设备备注，仅在站内展示"
			}
		}
		receipt := businessReceipt{ExecutionRef: g.Execution, OwnerID: g.Owner, RunID: g.Run, ActionID: g.Action, Capability: g.Capability, ArgsHash: hash, TargetVersion: g.TargetVersion, Result: businessJSON(output), BeforeHash: before, AfterHash: after, CreatedAt: time.Now().UTC()}
		return tx.Create(&receipt).Error
	})
	if errors.Is(err, conflict) {
		c.JSON(409, gin.H{"error": gin.H{"code": "target_or_permission_changed"}, "no_effect": true, "execution_ref": g.Execution, "action_id": g.Action})
		return
	}
	if err != nil {
		fail(c, 500, 50000, "business transaction failed")
		return
	}
	if g.Capability == "alarm.ack" {
		s.broadcast(gin.H{"type": "alarm_acked", "data": alarm})
	}
	c.JSON(200, output)
}
func (s *Server) businessExecutionReceipt(c *gin.Context) {
	if !s.businessInternal(c) {
		return
	}
	ref := c.Param("execution_ref")
	if !agentRunUUID.MatchString(ref) {
		fail(c, 400, 40001, "invalid execution reference")
		return
	}
	var receipt businessReceipt
	if s.db.First(&receipt, "execution_ref=? AND owner_id=?", ref, c.GetInt64("user_id")).Error != nil {
		fail(c, 404, 40401, "receipt not found")
		return
	}
	c.Data(200, "application/json", receipt.Result)
}
