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
