package telemetry

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"math"
	"regexp"
	"time"
)

type Sample struct {
	SchemaVersion int     `json:"schema_version"`
	RunID         string  `json:"run_id"`
	DeviceCode    string  `json:"device_id"`
	StationCode   string  `json:"station_id"`
	Seq           int64   `json:"seq"`
	TS            int64   `json:"ts_ms"`
	Voltage       float64 `json:"voltage"`
	Current       float64 `json:"current"`
	Temperature   float64 `json:"temperature"`
	Power         float64 `json:"power"`
	Status        int     `json:"status"`
	FaultCode     int     `json:"fault_code"`
}

var Code = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)
var Metrics = map[string]bool{"voltage": true, "current": true, "temperature": true, "power": true}

func Parse(payload []byte) (Sample, error) {
	var s Sample
	if len(payload) > 64*1024 {
		return s, errors.New("payload too large")
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(payload, &fields); err != nil {
		return s, err
	}
	for _, field := range []string{"schema_version", "run_id", "device_id", "station_id", "seq", "ts_ms", "voltage", "current", "temperature", "power", "status", "fault_code"} {
		value, ok := fields[field]
		if !ok || string(value) == "null" {
			return s, errors.New("missing field: " + field)
		}
	}
	dec := json.NewDecoder(bytes.NewReader(payload))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&s); err != nil {
		return s, err
	}
	var extra any
	if err := dec.Decode(&extra); err != io.EOF {
		return s, errors.New("trailing JSON")
	}
	if s.SchemaVersion != 1 || !Code.MatchString(s.RunID) || !Code.MatchString(s.DeviceCode) || !Code.MatchString(s.StationCode) || s.Seq < 0 {
		return s, errors.New("invalid identity or sequence")
	}
	now := time.Now()
	ts := time.UnixMilli(s.TS)
	if ts.Before(now.Add(-48*time.Hour)) || ts.After(now.Add(48*time.Hour)) {
		return s, errors.New("timestamp outside 48-hour window")
	}
	for _, v := range []float64{s.Voltage, s.Current, s.Temperature, s.Power} {
		if math.IsNaN(v) || math.IsInf(v, 0) {
			return s, errors.New("non-finite value")
		}
	}
	if s.Voltage < 320 || s.Voltage > 460 || s.Current < 0 || s.Current > 160.4 || s.Temperature < -30 || s.Temperature > 85 || s.Power < 0 || s.Power > 110 {
		return s, errors.New("value outside physical range")
	}
	if (s.Status != 0 && s.Status != 1 && s.Status != 2) || s.FaultCode < 0 || s.FaultCode > 9999 {
		return s, errors.New("invalid status or fault code")
	}
	if math.Abs(s.Power-math.Sqrt(3)*s.Voltage*s.Current*0.99/1000) > 2 {
		return s, errors.New("inconsistent power")
	}
	return s, nil
}

func (s Sample) Value(metric string) float64 {
	switch metric {
	case "voltage":
		return s.Voltage
	case "current":
		return s.Current
	case "temperature":
		return s.Temperature
	case "power":
		return s.Power
	}
	return 0
}
