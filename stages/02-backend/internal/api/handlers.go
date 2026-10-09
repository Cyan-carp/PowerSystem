package api

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
	"go.uber.org/zap"
	"golang.org/x/crypto/bcrypt"
	"gorm.io/gorm"

	"powersystem/backend/internal/energy"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/telemetry"
)

func (s *Server) register(c *gin.Context) {
	var body struct {
		Username string `json:"username"`
		Password string `json:"password"`
		RealName string `json:"real_name"`
	}
	if c.ShouldBindJSON(&body) != nil || len(body.Username) < 3 || !telemetry.Code.MatchString(body.Username) || len(body.Password) < 8 || len(body.Password) > 72 || len(body.RealName) > 64 {
		fail(c, 400, 40001, "invalid username or password")
		return
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(body.Password), bcrypt.DefaultCost)
	if err != nil {
		fail(c, 500, 50000, "registration failed")
		return
	}
	user := model.User{Username: body.Username, PasswordHash: string(hash), RealName: body.RealName, Role: "operator"}
	if err = s.db.Create(&user).Error; err != nil {
		fail(c, 409, 40001, "username already exists")
		return
	}
	c.JSON(201, response{0, "success", user})
}
func (s *Server) login(c *gin.Context) {
	var body struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if c.ShouldBindJSON(&body) != nil {
		fail(c, 400, 40001, "invalid credentials")
		return
	}
	var user model.User
	if err := s.db.Where("username=?", body.Username).First(&user).Error; err != nil || bcrypt.CompareHashAndPassword([]byte(user.PasswordHash), []byte(body.Password)) != nil {
		fail(c, 401, 40101, "invalid credentials")
		return
	}
	token, err := s.sign(user)
	if err != nil {
		fail(c, 500, 50000, "login failed")
		return
	}
	ok(c, gin.H{"token": token, "token_type": "Bearer", "expires_in": int(configTokenSeconds()), "user": user})
}
func configTokenSeconds() int64 { return int64((8 * time.Hour).Seconds()) }

type deviceInput struct {
	DeviceCode  string `json:"device_code"`
	Name        string `json:"name"`
	DevType     string `json:"dev_type"`
	Vendor      string `json:"vendor"`
	StationCode string `json:"station_code"`
	GroupName   string `json:"group_name"`
}

func validDevice(in deviceInput) bool {
	return telemetry.Code.MatchString(in.DeviceCode) && telemetry.Code.MatchString(in.StationCode) && strings.TrimSpace(in.Name) != "" && len(in.Name) <= 128 && strings.TrimSpace(in.DevType) != "" && len(in.DevType) <= 32 && len(in.Vendor) <= 64 && len(in.GroupName) <= 64
}
func (s *Server) findDevice(id int64) (model.Device, error) {
	var device model.Device
	err := s.db.Where("id=? AND deleted_at IS NULL", id).First(&device).Error
	return device, err
}
func (s *Server) listDevices(c *gin.Context) {
	p, size, valid := page(c)
	if !valid {
		return
	}
	query := s.db.Model(&model.Device{}).Where("deleted_at IS NULL")
	if keyword := strings.TrimSpace(c.Query("keyword")); keyword != "" {
		if len(keyword) > 128 {
			fail(c, 400, 40001, "keyword exceeds 128 bytes")
			return
		}
		query = query.Where("strpos(lower(name),lower(?)) > 0 OR strpos(lower(device_code),lower(?)) > 0", keyword, keyword)
	}
	if group := c.Query("group_name"); group != "" {
		query = query.Where("group_name=?", group)
	}
	var total int64
	if err := query.Count(&total).Error; err != nil {
		fail(c, 500, 50000, "device query failed")
		return
	}
	var devices []model.Device
	if err := query.Order("id ASC").Offset((p - 1) * size).Limit(size).Find(&devices).Error; err != nil {
		fail(c, 500, 50000, "device query failed")
		return
	}
	ok(c, list(devices, p, size, total))
}
func (s *Server) createDevice(c *gin.Context) {
	var in deviceInput
	if c.ShouldBindJSON(&in) != nil || !validDevice(in) {
		fail(c, 400, 40001, "invalid device")
		return
	}
	device := model.Device{DeviceCode: in.DeviceCode, Name: in.Name, DevType: in.DevType, Vendor: in.Vendor, StationCode: in.StationCode, GroupName: in.GroupName}
	if err := s.db.Create(&device).Error; err != nil {
		fail(c, 409, 40001, "device code already exists")
		return
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), 3*time.Second)
	defer cancel()
	if err := s.td.EnsureDevice(ctx, device); err != nil {
		s.log.Warn("device_table_deferred", zap.Error(err))
	}
	c.JSON(201, response{0, "success", device})
}
func (s *Server) getDevice(c *gin.Context) {
	deviceID, valid := id(c)
	if !valid {
		return
	}
	device, err := s.findDevice(deviceID)
	if err != nil {
		fail(c, 404, 40401, "device not found")
		return
	}
	ok(c, device)
}
func (s *Server) updateDevice(c *gin.Context) {
	deviceID, valid := id(c)
	if !valid {
		return
	}
	device, err := s.findDevice(deviceID)
	if err != nil {
		fail(c, 404, 40401, "device not found")
		return
	}
	var in struct {
		Name      string `json:"name"`
		DevType   string `json:"dev_type"`
		Vendor    string `json:"vendor"`
		GroupName string `json:"group_name"`
	}
	if c.ShouldBindJSON(&in) != nil || strings.TrimSpace(in.Name) == "" || strings.TrimSpace(in.DevType) == "" || len(in.Name) > 128 || len(in.DevType) > 32 || len(in.Vendor) > 64 || len(in.GroupName) > 64 {
		fail(c, 400, 40001, "invalid device update")
		return
	}
	if device.IsSimulated && in.Vendor != "synthetic" {
		fail(c, 409, 40001, "simulated device requires separate real-device onboarding")
		return
	}
	device.Name = in.Name
	device.DevType = in.DevType
	device.Vendor = in.Vendor
	device.GroupName = in.GroupName
	if err = s.db.Save(&device).Error; err != nil {
		fail(c, 500, 50000, "device update failed")
		return
	}
	ok(c, device)
}
func (s *Server) deleteDevice(c *gin.Context) {
	deviceID, valid := id(c)
	if !valid {
		return
	}
	_, err := s.findDevice(deviceID)
	if err != nil {
		fail(c, 404, 40401, "device not found")
		return
	}
	now := time.Now().UTC()
	err = s.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Model(&model.Device{}).Where("id=?", deviceID).Update("deleted_at", now).Error; err != nil {
			return err
		}
		if err := tx.Model(&model.AlarmRule{}).Where("device_id=?", deviceID).Update("enabled", false).Error; err != nil {
			return err
		}
		var metrics []string
		if err := tx.Model(&model.AlarmRecord{}).Where("device_id=? AND recovered_at IS NULL", deviceID).Distinct("metric").Pluck("metric", &metrics).Error; err != nil {
			return err
		}
		for _, metric := range metrics {
			if err := s.closeRuleAlarms(tx, deviceID, metric, now); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		fail(c, 500, 50000, "device delete failed")
		return
	}
	ok(c, gin.H{"id": deviceID, "deleted": true})
}

func (s *Server) history(c *gin.Context) {
	deviceID, valid := id(c)
	if !valid {
		return
	}
	device, err := s.findDevice(deviceID)
	if err != nil {
		fail(c, 404, 40401, "device not found")
		return
	}
	metric := c.Query("metric")
	start, err1 := time.Parse(time.RFC3339, c.Query("start"))
	end, err2 := time.Parse(time.RFC3339, c.Query("end"))
	if !telemetry.Metrics[metric] || err1 != nil || err2 != nil || !end.After(start) || end.Sub(start) > 24*time.Hour {
		fail(c, 400, 40001, "metric and RFC3339 start/end within 24 hours required")
		return
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
	defer cancel()
	if err = s.td.EnsureDevice(ctx, device); err != nil {
		fail(c, 503, 50000, "telemetry unavailable")
		return
	}
	rows, err := s.td.History(ctx, deviceID, metric, start.UnixMilli(), end.UnixMilli())
	if err != nil {
		fail(c, 503, 50000, "telemetry query failed or exceeds 5000 points")
		return
	}
	ok(c, gin.H{"metric": metric, "device_id": deviceID, "points": rows})
}
func (s *Server) latest(c *gin.Context) {
	deviceID, valid := id(c)
	if !valid {
		return
	}
	if _, err := s.findDevice(deviceID); err != nil {
		fail(c, 404, 40401, "device not found")
		return
	}
	value, err := s.redis.Get(c.Request.Context(), "device:"+strconv.FormatInt(deviceID, 10)+":latest").Result()
	if err != nil {
		if errors.Is(err, redis.Nil) {
			fail(c, 404, 40401, "latest telemetry unavailable")
		} else {
			fail(c, 503, 50000, "latest telemetry cache unavailable")
		}
		return
	}
	var data any
	if json.Unmarshal([]byte(value), &data) != nil {
		fail(c, 500, 50000, "latest telemetry invalid")
		return
	}
	ok(c, data)
}

func (s *Server) dashboard(c *gin.Context) {
	var devices []model.Device
	if err := s.db.Where("deleted_at IS NULL").Find(&devices).Error; err != nil {
		fail(c, 500, 50000, "dashboard failed")
		return
	}
	var active int64
	if err := s.db.Model(&model.AlarmRecord{}).Where("recovered_at IS NULL").Count(&active).Error; err != nil {
		fail(c, 500, 50000, "dashboard failed")
		return
	}
	var levels []struct {
		DeviceID int64
		Rank     int
	}
	if err := s.db.Raw(`SELECT device_id,MAX(CASE level WHEN 'urgent' THEN 3 WHEN 'major' THEN 2 WHEN 'minor' THEN 1 ELSE 0 END) AS rank
FROM alarm_records WHERE recovered_at IS NULL GROUP BY device_id`).Scan(&levels).Error; err != nil {
		fail(c, 500, 50000, "dashboard failed")
		return
	}
	levelByDevice := make(map[int64]string, len(levels))
	for _, level := range levels {
		levelByDevice[level.DeviceID] = map[int]string{1: "minor", 2: "major", 3: "urgent"}[level.Rank]
	}
	online := 0
	fault := 0
	healthy := 0
	healthPoints := 0
	power := 0.0
	now := time.Now()
	for _, d := range devices {
		raw, err := s.redis.Get(c.Request.Context(), "device:"+strconv.FormatInt(d.ID, 10)+":latest").Bytes()
		if err != nil {
			healthPoints += energy.DeviceScore(false, 0, "")
			continue
		}
		var v struct {
			ReceivedAt time.Time `json:"received_at"`
			Power      float64   `json:"power"`
			Status     int       `json:"status"`
		}
		if json.Unmarshal(raw, &v) != nil || now.Sub(v.ReceivedAt) > 15*time.Second {
			healthPoints += energy.DeviceScore(false, 0, "")
			continue
		}
		healthPoints += energy.DeviceScore(true, v.Status, levelByDevice[d.ID])
		online++
		power += v.Power
		if v.Status == 2 {
			fault++
		} else {
			healthy++
		}
	}
	score := 0.0
	if len(devices) > 0 {
		score = 100 * float64(healthy) / float64(len(devices))
	}
	var healthScore any
	if len(devices) > 0 {
		healthScore = math.Round(float64(healthPoints) / float64(len(devices)))
	}
	e, err := energy.ReadSummary(c.Request.Context(), s.db, now)
	if err != nil {
		fail(c, 500, 50000, "energy summary failed")
		return
	}
	ok(c, gin.H{"device_total": len(devices), "online": online, "offline": len(devices) - online, "fault": fault, "current_power_kw": math.Round(power*100) / 100, "active_alarms": active, "operational_health_percent": score, "health_score_percent": healthScore, "today_energy_kwh": e.TodayKWh, "retained_energy_kwh": e.RetainedKWh, "energy_start_ms": e.StartMS, "energy_updated_at": e.UpdatedAt})
}
