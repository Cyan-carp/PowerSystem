package api

import (
	"context"
	"encoding/csv"
	"fmt"
	"math"
	"os"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
)

// FR-11：单设备原始遥测 CSV 导出。约束：最长 7 天、最多 20 万条、后端分页生成完整临时文件。
const (
	exportMaxDays     = 7 * 24 * time.Hour
	exportMaxRows     = 200000
	exportPageSize    = 5000
	exportBeijingZone = "Asia/Shanghai"
)

var exportHeaders = []string{"设备编码", "场站编码", "采样时间戳(ms)", "北京时间", "序号", "电压(V)", "电流(A)", "温度(℃)", "功率(kW)", "状态", "故障码"}

func beijingTime(ms int64) string {
	return time.UnixMilli(ms).In(time.FixedZone(exportBeijingZone, 8*60*60)).Format("2006-01-02 15:04:05.000")
}

// csvCell escapes a field and neutralizes formula injection for text values.
func csvCell(value string) string {
	if value != "" && (value[0] == '=' || value[0] == '+' || value[0] == '-' || value[0] == '@') {
		value = "'" + value
	}
	return value
}

func (s *Server) exportTelemetry(c *gin.Context) {
	deviceID, valid := id(c)
	if !valid {
		return
	}
	device, err := s.findDevice(deviceID)
	if err != nil {
		fail(c, 404, 40401, "device not found")
		return
	}
	start, err1 := time.Parse(time.RFC3339, c.Query("start"))
	end, err2 := time.Parse(time.RFC3339, c.Query("end"))
	if err1 != nil || err2 != nil || !end.After(start) {
		fail(c, 400, 40001, "start/end require RFC3339 timestamps with end after start")
		return
	}
	if end.Sub(start) > exportMaxDays {
		fail(c, 400, 40001, "export window exceeds 7 days")
		return
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), 60*time.Second)
	defer cancel()
	if err = s.td.EnsureDevice(ctx, device); err != nil {
		fail(c, 503, 50000, "telemetry unavailable")
		return
	}
	startMs, endMs := start.UnixMilli(), end.UnixMilli()
	total, err := s.td.RawCount(ctx, deviceID, startMs, endMs)
	if err != nil {
		fail(c, 503, 50000, "telemetry count failed")
		return
	}
	if total > exportMaxRows {
		fail(c, 400, 40001, fmt.Sprintf("export exceeds %d rows; narrow the time range", exportMaxRows))
		return
	}
	// 生成完整临时文件后提供下载；下载失败不产生被界面标为成功的文件。
	tmp, err := os.CreateTemp("", "telemetry-export-*.csv")
	if err != nil {
		fail(c, 500, 50000, "export file unavailable")
		return
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath)
	// UTF-8 BOM，便于 Excel 识别中文表头。
	if _, err = tmp.Write([]byte{0xEF, 0xBB, 0xBF}); err != nil {
		tmp.Close()
		fail(c, 500, 50000, "export write failed")
		return
	}
	writer := csv.NewWriter(tmp)
	if err = writer.Write(exportHeaders); err != nil {
		tmp.Close()
		fail(c, 500, 50000, "export write failed")
		return
	}
	written := 0
	var lastTS, lastSeq int64
	for offset := 0; ; offset += exportPageSize {
		var rows [][]any
		if offset == 0 {
			rows, err = s.td.RawExport(ctx, deviceID, startMs, endMs, 0, exportPageSize)
		} else {
			rows, err = s.td.RawExportAfter(ctx, deviceID, startMs, endMs, lastTS, lastSeq, exportPageSize)
		}
		if err != nil {
			tmp.Close()
			fail(c, 503, 50000, "telemetry export query failed")
			return
		}
		if len(rows) == 0 {
			break
		}
		if written+len(rows) > exportMaxRows {
			tmp.Close()
			fail(c, 400, 40001, "export exceeds 200000 rows; narrow the time range")
			return
		}
		for _, row := range rows {
			if len(row) != 8 {
				tmp.Close()
				fail(c, 503, 50000, "telemetry export row invalid")
				return
			}
			ts, err := parseTDTime(row[0])
			seq, seqOK := asInt64(row, 1)
			if err != nil || ts < startMs || ts > endMs || !seqOK || seq < 0 || (written > 0 && (ts < lastTS || (ts == lastTS && seq <= lastSeq))) {
				tmp.Close()
				fail(c, 503, 50000, "telemetry export timestamp invalid")
				return
			}
			rec := make([]string, len(exportHeaders))
			rec[0] = csvCell(device.DeviceCode)
			rec[1] = csvCell(device.StationCode)
			rec[2] = strconv.FormatInt(ts, 10)
			rec[3] = beijingTime(ts)
			// row: ts(0), seq(1), voltage(2), current(3), temperature(4), power(5), status(6), fault_code(7)
			// rec: seq(4), voltage(5), current(6), temperature(7), power(8), status(9), fault_code(10)
			rec[4] = formatCell(row, 1)
			rec[5] = formatCell(row, 2)
			rec[6] = formatCell(row, 3)
			rec[7] = formatCell(row, 4)
			rec[8] = formatCell(row, 5)
			rec[9] = formatCell(row, 6)
			rec[10] = formatCell(row, 7)
			if err = writer.Write(rec); err != nil {
				tmp.Close()
				fail(c, 500, 50000, "export write failed")
				return
			}
			written++
			lastTS, lastSeq = ts, seq
		}
		if len(rows) < exportPageSize {
			break
		}
	}
	// A changed row set is not a complete export. No successful file is sent until checked.
	finalTotal, countErr := s.td.RawCount(ctx, deviceID, startMs, endMs)
	if countErr != nil {
		tmp.Close()
		fail(c, 503, 50000, "telemetry export verification failed")
		return
	}
	if finalTotal != total || finalTotal != written {
		tmp.Close()
		fail(c, 409, 40001, "telemetry changed during export; retry the query")
		return
	}
	writer.Flush()
	if err = writer.Error(); err != nil {
		tmp.Close()
		fail(c, 500, 50000, "export write failed")
		return
	}
	if err = tmp.Close(); err != nil {
		fail(c, 500, 50000, "export close failed")
		return
	}
	if written == 0 {
		// 无数据返回带表头的空文件（含 BOM）。
	}
	filename := fmt.Sprintf("%s_%s_%s.csv", device.DeviceCode, start.UTC().Format("20060102T150405Z"), end.UTC().Format("20060102T150405Z"))
	c.Header("Content-Type", "text/csv; charset=utf-8")
	c.Header("X-Rule-Version", "raw-telemetry-v1")
	c.Header("X-Computed-At", time.Now().UTC().Format(time.RFC3339Nano))
	c.Header("X-Data-Complete", "true")
	c.Header("X-Row-Count", strconv.Itoa(written))
	c.Header("Content-Disposition", fmt.Sprintf("attachment; filename=%q", filename))
	c.File(tmpPath)
}

func formatCell(row []any, i int) string {
	if i >= len(row) || row[i] == nil {
		return ""
	}
	switch value := row[i].(type) {
	case float64:
		if value == float64(int64(value)) {
			return strconv.FormatInt(int64(value), 10)
		}
		return strconv.FormatFloat(value, 'f', -1, 64)
	default:
		return fmt.Sprintf("%v", value)
	}
}

func asInt64(row []any, i int) (int64, bool) {
	if i >= len(row) || row[i] == nil {
		return 0, false
	}
	switch value := row[i].(type) {
	case float64:
		if math.IsNaN(value) || math.IsInf(value, 0) || math.Trunc(value) != value || value < math.MinInt64 || value >= math.MaxInt64 {
			return 0, false
		}
		return int64(value), true
	case string:
		n, err := strconv.ParseInt(value, 10, 64)
		return n, err == nil
	default:
		return 0, false
	}
}
