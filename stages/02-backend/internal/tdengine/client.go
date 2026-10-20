package tdengine

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"powersystem/backend/internal/config"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/telemetry"
)

type Client struct {
	cfg  config.Config
	http *http.Client
}
type Result struct {
	Code       int     `json:"code"`
	Desc       string  `json:"desc"`
	Rows       int     `json:"rows"`
	ColumnMeta [][]any `json:"column_meta"`
	Data       [][]any `json:"data"`
}

func New(cfg config.Config) *Client {
	return &Client{cfg: cfg, http: &http.Client{Timeout: 10 * time.Second}}
}
func (c *Client) SQL(ctx context.Context, sql string) (Result, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(c.cfg.TDURL, "/")+"/rest/sql", strings.NewReader(sql))
	if err != nil {
		return Result{}, err
	}
	req.SetBasicAuth(c.cfg.TDUser, c.cfg.TDPassword)
	req.Header.Set("Content-Type", "text/plain")
	resp, err := c.http.Do(req)
	if err != nil {
		return Result{}, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return Result{}, err
	}
	if resp.StatusCode != 200 {
		return Result{}, fmt.Errorf("TDengine HTTP %d: %s", resp.StatusCode, string(body))
	}
	var out Result
	if err = json.Unmarshal(body, &out); err != nil {
		return Result{}, err
	}
	if out.Code != 0 {
		return Result{}, fmt.Errorf("TDengine %d: %s", out.Code, out.Desc)
	}
	return out, nil
}
func (c *Client) Init(ctx context.Context) error {
	content, err := os.ReadFile("deploy/tdengine/init.sql")
	if err != nil {
		return err
	}
	script := strings.ReplaceAll(string(content), "powersystem_stage2", c.cfg.TDDatabase)
	for _, statement := range strings.Split(script, ";") {
		if sql := strings.TrimSpace(statement); sql != "" {
			if _, err := c.SQL(ctx, sql); err != nil {
				return err
			}
		}
	}
	return nil
}
func (c *Client) Table(deviceID int64) string {
	return fmt.Sprintf("%s.t_device_%d", c.cfg.TDDatabase, deviceID)
}
func (c *Client) EnsureDevice(ctx context.Context, d model.Device) error {
	if d.ID < 1 || !telemetry.Code.MatchString(d.DeviceCode) || !telemetry.Code.MatchString(d.StationCode) {
		return errors.New("invalid device tags")
	}
	_, err := c.SQL(ctx, fmt.Sprintf("CREATE TABLE IF NOT EXISTS %s USING %s.telemetry TAGS ('%s','%s')", c.Table(d.ID), c.cfg.TDDatabase, d.DeviceCode, d.StationCode))
	return err
}
func (c *Client) Insert(ctx context.Context, d model.Device, samples []telemetry.Sample) error {
	if len(samples) == 0 {
		return nil
	}
	if err := c.EnsureDevice(ctx, d); err != nil {
		return err
	}
	var b strings.Builder
	b.WriteString("INSERT INTO ")
	b.WriteString(c.Table(d.ID))
	b.WriteString(" VALUES")
	for _, s := range samples {
		fmt.Fprintf(&b, " (%d,%d,%.6f,%.6f,%.6f,%.6f,%d,%d)", s.TS, s.Seq, s.Voltage, s.Current, s.Temperature, s.Power, s.Status, s.FaultCode)
	}
	_, err := c.SQL(ctx, b.String())
	return err
}
func (c *Client) HasSample(ctx context.Context, deviceID, ts, seq int64) (bool, error) {
	if deviceID < 1 {
		return false, errors.New("invalid device")
	}
	result, err := c.SQL(ctx, fmt.Sprintf("SELECT seq FROM %s WHERE ts=%d AND seq=%d LIMIT 1", c.Table(deviceID), ts, seq))
	if err != nil {
		return false, err
	}
	return len(result.Data) > 0, nil
}
func (c *Client) History(ctx context.Context, deviceID int64, metric string, start, end int64) ([][]any, error) {
	if deviceID < 1 || !telemetry.Metrics[metric] {
		return nil, errors.New("invalid history query")
	}
	result, err := c.SQL(ctx, fmt.Sprintf("SELECT ts,%s FROM %s WHERE ts >= %d AND ts <= %d ORDER BY ts ASC LIMIT 5001", metric, c.Table(deviceID), start, end))
	if err != nil {
		return nil, err
	}
	if len(result.Data) > 5000 {
		return nil, errors.New("history exceeds 5000 points; narrow time range")
	}
	return result.Data, nil
}

// BoundarySample returns the last sample at or before ts, used to open an
// availability interval at a statistics window start.
func (c *Client) BoundarySample(ctx context.Context, deviceID, ts int64) ([][]any, error) {
	if deviceID < 1 || ts <= 0 {
		return nil, errors.New("invalid boundary query")
	}
	result, err := c.SQL(ctx, fmt.Sprintf("SELECT ts,status FROM %s WHERE ts <= %d ORDER BY ts DESC LIMIT 1", c.Table(deviceID), ts))
	if err != nil {
		return nil, err
	}
	return result.Data, nil
}

// StatusAfter reads only the fields needed for availability. Telemetry tables
// have a single millisecond timestamp primary key, so ts is a complete cursor.
// Keeping pages bounded also stays within the REST response size limit.
func (c *Client) StatusAfter(ctx context.Context, deviceID, after, end int64, limit int) ([][]any, error) {
	if deviceID < 1 || after <= 0 || end <= after || limit < 1 || limit > 5000 {
		return nil, errors.New("invalid status cursor")
	}
	result, err := c.SQL(ctx, fmt.Sprintf("SELECT ts,status FROM %s WHERE ts > %d AND ts < %d ORDER BY ts ASC LIMIT %d", c.Table(deviceID), after, end, limit))
	if err != nil {
		return nil, err
	}
	return result.Data, nil
}

// RawExport returns raw multivariate samples in ascending ts/seq order, paged
// by LIMIT/OFFSET for CSV export. It reads stored samples directly, not the
// downsampled history curve.
func (c *Client) RawExport(ctx context.Context, deviceID, start, end int64, offset, limit int) ([][]any, error) {
	if deviceID < 1 || start <= 0 || end < start || limit < 1 || limit > 5000 || offset < 0 {
		return nil, errors.New("invalid export query")
	}
	result, err := c.SQL(ctx, fmt.Sprintf("SELECT ts,seq,voltage,current,temperature,power,status,fault_code FROM %s WHERE ts >= %d AND ts <= %d ORDER BY ts ASC, seq ASC LIMIT %d OFFSET %d", c.Table(deviceID), start, end, limit, offset))
	if err != nil {
		return nil, err
	}
	return result.Data, nil
}

// RawCount returns the number of stored samples in the closed interval.
func (c *Client) RawCount(ctx context.Context, deviceID, start, end int64) (int, error) {
	if deviceID < 1 || start <= 0 || end < start {
		return 0, errors.New("invalid count query")
	}
	result, err := c.SQL(ctx, fmt.Sprintf("SELECT COUNT(*) FROM %s WHERE ts >= %d AND ts <= %d", c.Table(deviceID), start, end))
	if err != nil {
		return 0, err
	}
	if len(result.Data) == 0 || len(result.Data[0]) == 0 {
		return 0, errors.New("count returned no rows")
	}
	switch value := result.Data[0][0].(type) {
	case float64:
		return int(value), nil
	case json.Number:
		n, err := value.Int64()
		return int(n), err
	default:
		return 0, fmt.Errorf("unexpected count type %T", value)
	}
}

// RawExportAfter uses a stable keyset instead of OFFSET while telemetry arrives.
func (c *Client) RawExportAfter(ctx context.Context, deviceID, start, end, afterTS, afterSeq int64, limit int) ([][]any, error) {
	if deviceID < 1 || start <= 0 || end < start || afterTS < start || afterTS > end || afterSeq < 0 || limit < 1 || limit > 5000 {
		return nil, errors.New("invalid export cursor")
	}
	result, err := c.SQL(ctx, fmt.Sprintf("SELECT ts,seq,voltage,current,temperature,power,status,fault_code FROM %s WHERE ts >= %d AND ts <= %d AND (ts > %d OR (ts = %d AND seq > %d)) ORDER BY ts ASC, seq ASC LIMIT %d", c.Table(deviceID), start, end, afterTS, afterTS, afterSeq, limit))
	if err != nil {
		return nil, err
	}
	return result.Data, nil
}
func (c *Client) Window(ctx context.Context, deviceID, start, end int64) ([][]any, error) {
	if deviceID < 1 || start <= 0 || end < start || end-start > 31*60*1000 {
		return nil, errors.New("invalid prediction window")
	}
	result, err := c.SQL(ctx, fmt.Sprintf("SELECT ts,voltage,current,temperature,power FROM %s WHERE ts >= %d AND ts <= %d ORDER BY ts ASC LIMIT 501", c.Table(deviceID), start, end))
	if err != nil {
		return nil, err
	}
	if len(result.Data) > 500 {
		return nil, errors.New("prediction window exceeds 500 samples")
	}
	return result.Data, nil
}
