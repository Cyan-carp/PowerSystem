package model

import (
	"encoding/json"
	"time"
)

type PredictionRecord struct {
	ID           int64           `json:"id"`
	DeviceID     int64           `json:"device_id"`
	WindowEndMS  int64           `gorm:"column:window_end_ms" json:"window_end_ms"`
	Probability  float64         `json:"probability"`
	Threshold    float64         `json:"threshold"`
	RiskLevel    string          `json:"risk_level"`
	ModelVersion string          `json:"model_version"`
	TopFactors   json.RawMessage `gorm:"type:jsonb" json:"top_factors"`
	Source       string          `json:"source"`
	CreatedAt    time.Time       `json:"created_at"`
}

func (PredictionRecord) TableName() string { return "prediction_records" }
