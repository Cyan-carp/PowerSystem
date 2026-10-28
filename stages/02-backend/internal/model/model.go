package model

import (
	"encoding/json"
	"time"
)

type User struct {
	ID           int64     `json:"id"`
	Username     string    `json:"username"`
	PasswordHash string    `json:"-"`
	Role         string    `json:"role"`
	RealName     string    `json:"real_name"`
	CreatedAt    time.Time `json:"created_at"`
	UpdatedAt    time.Time `json:"updated_at"`
}
type Device struct {
	ID              int64      `json:"id"`
	DeviceCode      string     `json:"device_code"`
	Name            string     `json:"name"`
	DevType         string     `json:"dev_type"`
	Vendor          string     `json:"vendor"`
	IsSimulated     bool       `json:"is_simulated"`
	ReferenceVendor string     `json:"reference_vendor"`
	ReferenceModel  string     `json:"reference_model"`
	StationCode     string     `json:"station_code"`
	GroupName       string     `json:"group_name"`
	Remark          string     `json:"-" gorm:"default:''"`
	RemarkVersion   int64      `json:"remark_version" gorm:"default:0"`
	RemarkUpdatedBy *int64     `json:"-"`
	RemarkUpdatedAt *time.Time `json:"-"`
	DeletedAt       *time.Time `json:"-"`
	CreatedAt       time.Time  `json:"created_at"`
	UpdatedAt       time.Time  `json:"updated_at"`
}
type AlarmRule struct {
	Unit      string    `json:"unit" gorm:"-"`
	ID        int64     `json:"id"`
	DeviceID  int64     `json:"device_id"`
	Metric    string    `json:"metric"`
	Operator  string    `json:"operator"`
	Threshold float64   `json:"threshold"`
	Level     string    `json:"level"`
	Enabled   bool      `json:"enabled"`
	Version   string    `json:"version"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}
type AlarmRecord struct {
	ID           int64      `json:"id"`
	DeviceID     int64      `json:"device_id"`
	RuleID       *int64     `json:"rule_id"`
	Metric       string     `json:"metric"`
	Level        string     `json:"level"`
	Value        float64    `json:"value"`
	Threshold    float64    `json:"threshold"`
	Status       string     `json:"status"`
	Archived     bool       `json:"archived"`
	CloseReason  *string    `json:"close_reason"`
	RuleOperator string     `json:"rule_operator"`
	RuleVersion  string     `json:"rule_version"`
	Origin       string     `json:"origin" gorm:"default:normal"`
	ScenarioID   *int64     `json:"scenario_id"`
	AckedBy      *int64     `json:"acked_by"`
	AckedAt      *time.Time `json:"acked_at"`
	RecoveredAt  *time.Time `json:"recovered_at"`
	TriggeredAt  time.Time  `json:"triggered_at"`
	CreatedAt    time.Time  `json:"created_at"`
	UpdatedAt    time.Time  `json:"updated_at"`
}
type Inbox struct {
	ID               int64           `json:"id"`
	RunID            string          `json:"run_id"`
	DeviceID         int64           `json:"device_id"`
	Seq              int64           `json:"seq"`
	TS               int64           `gorm:"column:ts_ms" json:"ts_ms"`
	Payload          json.RawMessage `gorm:"type:jsonb" json:"payload"`
	ReceivedAt       time.Time       `json:"received_at"`
	ProcessedAt      *time.Time      `json:"processed_at"`
	BusinessState    *string         `json:"business_state" gorm:"default:pending"`
	TDConfirmedAt    *time.Time      `json:"td_confirmed_at"`
	FirstFailedAt    *time.Time      `json:"first_failed_at"`
	NextAttemptAt    *time.Time      `json:"next_attempt_at"`
	Attempts         int64           `json:"attempts"`
	FailureCode      *string         `json:"failure_code"`
	MissingSince     *time.Time      `json:"missing_since"`
	MissingCheckedAt *time.Time      `json:"missing_checked_at"`
	MissingAttempts  int64           `json:"missing_attempts"`
	ReasonSince      *time.Time      `json:"reason_since"`
	ReasonAttempts   int64           `json:"reason_attempts"`
	AlarmEvaluation  *string         `json:"alarm_evaluation"`
}

func (Inbox) TableName() string { return "telemetry_inbox" }
