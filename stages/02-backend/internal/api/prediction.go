package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
	"go.uber.org/zap"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"

	"powersystem/backend/internal/model"
	"powersystem/backend/internal/service"
)

const aiMetric = "ai_failure_risk"

type predictionSample struct {
	TS          int64   `json:"ts_ms"`
	Voltage     float64 `json:"voltage"`
	Current     float64 `json:"current"`
	Temperature float64 `json:"temperature"`
	Power       float64 `json:"power"`
}

type predictionResponse struct {
	DeviceID     int64           `json:"device_id"`
	WindowEndMS  int64           `json:"window_end_ms"`
	Probability  float64         `json:"probability"`
	Threshold    float64         `json:"threshold"`
	RiskLevel    string          `json:"risk_level"`
	ModelVersion string          `json:"model_version"`
	TopFactors   json.RawMessage `json:"top_factors"`
	Source       string          `json:"source"`
}

type predictionListRow struct {
	DeviceID     int64    `json:"device_id"`
	DeviceCode   string   `json:"device_code"`
	Name         string   `json:"name"`
	StationCode  string   `json:"station_code"`
	GroupName    string   `json:"group_name"`
	WindowEndMS  *int64   `json:"window_end_ms"`
	Probability  *float64 `json:"probability"`
	Threshold    *float64 `json:"threshold"`
	RiskLevel    *string  `json:"risk_level"`
	ModelVersion *string  `json:"model_version"`
	Source       *string  `json:"source"`
	Stale        bool     `json:"stale"`
}

func (s *Server) listPredictions(c *gin.Context) {
	p, size, valid := page(c)
	if !valid {
		return
	}
	var total int64
	if err := s.db.Model(&model.Device{}).Where("deleted_at IS NULL").Count(&total).Error; err != nil {
		fail(c, 503, 50000, "prediction query failed")
		return
	}
	cutoff := time.Now().Add(-15 * time.Minute).UnixMilli()
	var rows []predictionListRow
	err := s.db.Raw(`SELECT d.id AS device_id, d.device_code, d.name, d.station_code, d.group_name,
		p.window_end_ms, p.probability, p.threshold, p.risk_level, p.model_version, p.source
		FROM devices AS d
		LEFT JOIN LATERAL (
			SELECT window_end_ms, probability, threshold, risk_level, model_version, source
			FROM prediction_records WHERE device_id = d.id
			ORDER BY window_end_ms DESC, id DESC LIMIT 1
		) AS p ON true
		WHERE d.deleted_at IS NULL
		ORDER BY CASE WHEN p.window_end_ms IS NULL THEN 2 WHEN p.window_end_ms < ? THEN 1 ELSE 0 END,
			p.probability DESC NULLS LAST, d.id ASC
		LIMIT ? OFFSET ?`, cutoff, size, (p-1)*size).Scan(&rows).Error
	if err != nil {
		fail(c, 503, 50000, "prediction query failed")
		return
	}
	for i := range rows {
		rows[i].Stale = rows[i].WindowEndMS != nil && *rows[i].WindowEndMS < cutoff
	}
	ok(c, list(rows, p, size, total))
}

func (s *Server) getPrediction(c *gin.Context) {
	deviceID, valid := id(c)
	if !valid {
		return
	}
	if _, err := s.findDevice(deviceID); err != nil {
		fail(c, 404, 40401, "device not found")
		return
	}
	var item model.PredictionRecord
	err := s.db.Where("device_id=?", deviceID).Order("window_end_ms DESC,id DESC").First(&item).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		fail(c, 404, 40401, "prediction unavailable")
		return
	}
	if err != nil {
		fail(c, 503, 50000, "prediction query failed")
		return
	}
	stale := time.Since(time.UnixMilli(item.WindowEndMS)) > 15*time.Minute
	ok(c, gin.H{"device_id": item.DeviceID, "window_end_ms": item.WindowEndMS,
		"probability": item.Probability, "threshold": item.Threshold, "risk_level": item.RiskLevel,
		"model_version": item.ModelVersion, "top_factors": item.TopFactors,
		"source": item.Source, "created_at": item.CreatedAt, "stale": stale})
}

func (s *Server) predictionWorker(ctx context.Context) {
	first := time.NewTimer(15 * time.Second)
	defer first.Stop()
	ticker := time.NewTicker(time.Duration(s.cfg.AIPollSeconds) * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-first.C:
			s.predictAll(ctx)
		case <-ticker.C:
			s.predictAll(ctx)
		}
	}
}

func (s *Server) predictAll(ctx context.Context) {
	var devices []model.Device
	if err := s.db.Where("deleted_at IS NULL").Find(&devices).Error; err != nil {
		s.log.Warn("prediction_devices_unavailable")
		return
	}
	for _, device := range devices {
		if err := s.predictDevice(ctx, device); err != nil {
			s.log.Warn("prediction_skipped", zap.String("device", device.DeviceCode), zap.Error(err))
		}
	}
}

func (s *Server) predictDevice(ctx context.Context, device model.Device) error {
	raw, err := s.redis.Get(ctx, "device:"+strconv.FormatInt(device.ID, 10)+":latest").Bytes()
	if err != nil {
		if errors.Is(err, redis.Nil) {
			return nil
		}
		return err
	}
	var latest struct {
		TS         int64     `json:"ts_ms"`
		ReceivedAt time.Time `json:"received_at"`
		Status     int       `json:"status"`
		FaultCode  int       `json:"fault_code"`
	}
	if err = json.Unmarshal(raw, &latest); err != nil {
		return err
	}
	if latest.Status == 2 || latest.FaultCode != 0 {
		return nil
	}
	end := time.UnixMilli(latest.TS)
	if time.Since(latest.ReceivedAt) > 2*time.Minute || time.Since(end) > 2*time.Minute || end.After(time.Now().Add(time.Minute)) {
		return nil
	}
	start := (latest.TS/60_000 - 29) * 60_000
	rows, err := s.td.Window(ctx, device.ID, start, latest.TS)
	if err != nil {
		return err
	}
	samples := make([]predictionSample, 0, len(rows))
	for _, row := range rows {
		if len(row) != 5 {
			return errors.New("unexpected TDengine prediction row")
		}
		var sample predictionSample
		values := []*float64{&sample.Voltage, &sample.Current, &sample.Temperature, &sample.Power}
		ts, err := parseTDTime(row[0])
		if err != nil {
			return err
		}
		sample.TS = ts
		for i, target := range values {
			*target, err = strconv.ParseFloat(fmt.Sprint(row[i+1]), 64)
			if err != nil {
				return err
			}
		}
		samples = append(samples, sample)
	}
	if len(samples) < 27 {
		return nil
	}
	body, _ := json.Marshal(gin.H{"device_id": device.ID, "window_end_ms": latest.TS, "samples": samples})
	requestCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	request, err := http.NewRequestWithContext(requestCtx, http.MethodPost, s.cfg.AIURL+"/predict", bytes.NewReader(body))
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := (&http.Client{Timeout: 5 * time.Second}).Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode == 422 {
		return nil
	} // incomplete minute coverage
	if response.StatusCode != 200 {
		return fmt.Errorf("predict HTTP %d", response.StatusCode)
	}
	payload, err := io.ReadAll(io.LimitReader(response.Body, 64<<10))
	if err != nil {
		return err
	}
	var result predictionResponse
	if err = json.Unmarshal(payload, &result); err != nil {
		return err
	}
	if result.DeviceID != device.ID || result.WindowEndMS != latest.TS ||
		math.IsNaN(result.Probability) || result.Probability < 0 || result.Probability > 1 ||
		math.IsNaN(result.Threshold) || result.Threshold < 0 || result.Threshold > 1 ||
		result.ModelVersion == "" || len(result.ModelVersion) > 64 ||
		!json.Valid(result.TopFactors) || result.Source != "synthetic-trained" {
		return errors.New("invalid prediction response")
	}
	result.RiskLevel = "low"
	if result.Probability >= result.Threshold {
		result.RiskLevel = "high"
	}
	return s.savePrediction(device, result)
}

func parseTDTime(value any) (int64, error) {
	if number, ok := value.(float64); ok && number > 1e12 && number < 1e14 {
		return int64(number), nil
	}
	raw := fmt.Sprint(value)
	if ms, err := strconv.ParseInt(raw, 10, 64); err == nil {
		return ms, nil
	}
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02 15:04:05.000", "2006-01-02 15:04:05"} {
		parsed, err := time.ParseInLocation(layout, raw, time.Local)
		if err == nil {
			return parsed.UnixMilli(), nil
		}
	}
	return 0, fmt.Errorf("invalid TDengine timestamp %q", raw)
}

func (s *Server) savePrediction(device model.Device, result predictionResponse) error {
	var event *service.AlarmEvent
	inserted := false
	err := s.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).First(&device, device.ID).Error; err != nil {
			return err
		}
		insert := tx.Exec("INSERT INTO prediction_records(device_id,window_end_ms,probability,threshold,risk_level,model_version,top_factors,source) VALUES(?,?,?,?,?,?,?::jsonb,?) ON CONFLICT(device_id,window_end_ms,model_version) DO NOTHING",
			device.ID, result.WindowEndMS, result.Probability, result.Threshold, result.RiskLevel, result.ModelVersion, string(result.TopFactors), result.Source)
		if insert.Error != nil {
			return insert.Error
		}
		if insert.RowsAffected == 0 {
			return nil
		}
		inserted = true
		var active model.AlarmRecord
		lookup := tx.Where("device_id=? AND metric=? AND recovered_at IS NULL", device.ID, aiMetric).First(&active).Error
		if lookup != nil && !errors.Is(lookup, gorm.ErrRecordNotFound) {
			return lookup
		}
		now := time.Now().UTC()
		if result.RiskLevel == "high" && errors.Is(lookup, gorm.ErrRecordNotFound) {
			active = model.AlarmRecord{DeviceID: device.ID, Metric: aiMetric, Level: "major",
				Value: result.Probability, Threshold: result.Threshold, Status: "unhandled", TriggeredAt: now}
			if err := tx.Create(&active).Error; err != nil {
				return err
			}
			if err := service.QueueAlarmNotification(tx, device, active, "triggered"); err != nil {
				return err
			}
			event = &service.AlarmEvent{Type: "alarm_created", Data: active}
		} else if result.RiskLevel == "low" && lookup == nil {
			if err := tx.Model(&active).Updates(map[string]any{"recovered_at": now, "status": "recovered", "updated_at": now}).Error; err != nil {
				return err
			}
			active.RecoveredAt = &now
			active.Status = "recovered"
			if err := service.QueueAlarmNotification(tx, device, active, "recovered"); err != nil {
				return err
			}
			event = &service.AlarmEvent{Type: "alarm_recovered", Data: active}
		}
		return nil
	})
	if err != nil {
		return err
	}
	if !inserted {
		return nil
	}
	s.broadcast(gin.H{"type": "prediction", "data": result})
	if event != nil {
		s.broadcast(event)
	}
	return nil
}
