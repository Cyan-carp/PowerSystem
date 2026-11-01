package model

import (
	"encoding/json"
	"time"
)

// PredictionOutcome 是一条预测的真值标注，prediction_id 与预测记录一一对应。
type PredictionOutcome struct {
	PredictionID    int64     `gorm:"column:prediction_id;primaryKey" json:"prediction_id"`
	DeviceID        int64     `json:"device_id"`
	DeviceCode      string    `json:"device_code"`
	StationCode     string    `json:"station_code"`
	WindowEndMS     int64     `gorm:"column:window_end_ms" json:"window_end_ms"`
	ModelVersion    string    `json:"model_version"`
	Probability     float64   `json:"probability"`
	Threshold       float64   `json:"threshold"`
	PredictedHigh   bool      `json:"predicted_high"`
	ActualFault     bool      `json:"actual_fault"`
	FaultOnsetMS    *int64    `gorm:"column:fault_onset_ms" json:"fault_onset_ms"`
	LabelSource     string    `json:"label_source"`
	LabelConfidence string    `json:"label_confidence"`
	LabeledAt       time.Time `json:"labeled_at"`
	LabeledBy       *string   `json:"labeled_by"`
	Note            *string   `json:"note"`
}

func (PredictionOutcome) TableName() string { return "prediction_outcomes" }

// PredictionQualityDaily 是按上海日历日聚合的混淆矩阵与校准指标。
type PredictionQualityDaily struct {
	Day          time.Time `json:"day"`
	ModelVersion string    `json:"model_version"`
	Samples      int       `json:"samples"`
	TP           int       `json:"tp"`
	FP           int       `json:"fp"`
	FN           int       `json:"fn"`
	TN           int       `json:"tn"`
	Precision    *float64  `json:"precision"`
	Recall       *float64  `json:"recall"`
	F1           *float64  `json:"f1"`
	Accuracy     *float64  `json:"accuracy"`
	ECE          *float64  `json:"ece"`
	ProbMean     *float64  `json:"prob_mean"`
	HighRatio    *float64  `json:"high_ratio"`
	UpdatedAt    time.Time `json:"updated_at"`
}

func (PredictionQualityDaily) TableName() string { return "prediction_quality_daily" }

// ModelRegistry 是模型版本台账。本模块只维护状态流转，不碰容器与模型文件。
type ModelRegistry struct {
	ID              int64           `json:"id"`
	ModelVersion    string          `json:"model_version"`
	Algorithm       string          `json:"algorithm"`
	Status          string          `json:"status"`
	Threshold       *float64        `json:"threshold"`
	Calibration     json.RawMessage `gorm:"type:jsonb" json:"calibration"`
	Metrics         json.RawMessage `gorm:"type:jsonb" json:"metrics"`
	BaselineMetrics json.RawMessage `gorm:"type:jsonb" json:"baseline_metrics"`
	DataSource      *string         `json:"data_source"`
	TrainSamples    *int            `json:"train_samples"`
	PositiveSamples *int            `json:"positive_samples"`
	ArtifactPath    *string         `json:"artifact_path"`
	TrainedAt       *time.Time      `json:"trained_at"`
	PromotedAt      *time.Time      `json:"promoted_at"`
	PromotedBy      *string         `json:"promoted_by"`
	RejectedReason  *string         `json:"rejected_reason"`
	CreatedAt       time.Time       `json:"created_at"`
}

func (ModelRegistry) TableName() string { return "model_registry" }

// PredictionDriftEvent 是一条漂移观察记录，本轮只写事实，不触发训练。
type PredictionDriftEvent struct {
	ID         int64           `json:"id"`
	DetectedAt time.Time       `json:"detected_at"`
	Level      string          `json:"level"`
	Signal     string          `json:"signal"`
	Observed   *float64        `json:"observed"`
	Baseline   *float64        `json:"baseline"`
	Threshold  *float64        `json:"threshold"`
	Action     string          `json:"action"`
	Detail     json.RawMessage `gorm:"type:jsonb" json:"detail"`
}

func (PredictionDriftEvent) TableName() string { return "prediction_drift_events" }
