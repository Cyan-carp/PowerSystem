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
	ID          int64      `json:"id"`
	DeviceCode  string     `json:"device_code"`
	Name        string     `json:"name"`
	DevType     string     `json:"dev_type"`
	Vendor      string     `json:"vendor"`
	StationCode string     `json:"station_code"`
	GroupName   string     `json:"group_name"`
	DeletedAt   *time.Time `json:"-"`
	CreatedAt   time.Time  `json:"created_at"`
	UpdatedAt   time.Time  `json:"updated_at"`
}
type AlarmRule struct {
	ID        int64     `json:"id"`
	DeviceID  int64     `json:"device_id"`
	Metric    string    `json:"metric"`
	Operator  string    `json:"operator"`
	Threshold float64   `json:"threshold"`
	Level     string    `json:"level"`
	Enabled   bool      `json:"enabled"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}
type AlarmRecord struct {
	ID          int64      `json:"id"`
	DeviceID    int64      `json:"device_id"`
	RuleID      *int64     `json:"rule_id"`
	Metric      string     `json:"metric"`
	Level       string     `json:"level"`
	Value       float64    `json:"value"`
	Threshold   float64    `json:"threshold"`
	Status      string     `json:"status"`
	AckedBy     *int64     `json:"acked_by"`
	AckedAt     *time.Time `json:"acked_at"`
	RecoveredAt *time.Time `json:"recovered_at"`
	TriggeredAt time.Time  `json:"triggered_at"`
	CreatedAt   time.Time  `json:"created_at"`
	UpdatedAt   time.Time  `json:"updated_at"`
}
type Inbox struct {
	ID          int64           `json:"id"`
	RunID       string          `json:"run_id"`
	DeviceID    int64           `json:"device_id"`
	Seq         int64           `json:"seq"`
	TS          int64           `gorm:"column:ts_ms" json:"ts_ms"`
	Payload     json.RawMessage `gorm:"type:jsonb" json:"payload"`
	ReceivedAt  time.Time       `json:"received_at"`
	ProcessedAt *time.Time      `json:"processed_at"`
}

func (Inbox) TableName() string { return "telemetry_inbox" }
