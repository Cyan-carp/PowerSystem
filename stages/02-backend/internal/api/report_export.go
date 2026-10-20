package api

import (
	"bytes"
	"encoding/csv"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
)

func reportNumber(v *float64) string {
	if v == nil {
		return ""
	}
	return strconv.FormatFloat(*v, 'f', -1, 64)
}
func reportInt(v int64) string { return strconv.FormatInt(v, 10) }

func writeReportCSV(c *gin.Context, filename, version, computedAt string, complete bool, header []string, rows [][]string) {
	var buf bytes.Buffer
	buf.Write([]byte{0xef, 0xbb, 0xbf})
	w := csv.NewWriter(&buf)
	if err := w.Write(header); err != nil {
		fail(c, 500, 50000, "report export failed")
		return
	}
	for _, row := range rows {
		for i := range row {
			row[i] = csvCell(row[i])
		}
		if err := w.Write(row); err != nil {
			fail(c, 500, 50000, "report export failed")
			return
		}
	}
	w.Flush()
	if w.Error() != nil {
		fail(c, 500, 50000, "report export failed")
		return
	}
	c.Header("Content-Disposition", "attachment; filename=\""+filename+"\"")
	c.Header("X-Computed-At", computedAt)
	c.Header("X-Rule-Version", version)
	c.Header("X-Data-Complete", strconv.FormatBool(complete))
	c.Data(200, "text/csv; charset=utf-8", buf.Bytes())
}

func (s *Server) availabilityCSV(c *gin.Context, devices []availabilityResult, stations []stationAvailability, start, end time.Time, computedAt string) {
	header := []string{"范围类型", "设备编码", "场站编码", "有效窗口(ms)", "运行(ms)", "停机(ms)", "故障(ms)", "未知(ms)", "覆盖(ms)", "观测可用率(0-1)", "数据覆盖率(0-1)", "数据不足", "规则版本", "开始", "结束", "计算时间", "归组依据"}
	rows := make([][]string, 0, len(devices)+len(stations))
	for _, d := range devices {
		rows = append(rows, []string{"device", d.DeviceCode, d.StationCode, reportInt(d.WindowMs), reportInt(d.RunningMs), reportInt(d.StoppedMs), reportInt(d.FaultMs), reportInt(d.UnknownMs), reportInt(d.CoveredMs), reportNumber(d.ObservedAvail), reportNumber(d.DataCoverage), strconv.FormatBool(d.Insufficient), availabilityRule, start.Format(time.RFC3339Nano), end.Format(time.RFC3339Nano), computedAt, "当前台账（非历史归属）"})
	}
	for _, st := range stations {
		rows = append(rows, []string{"station", "", st.StationCode, reportInt(st.WindowMs), reportInt(st.RunningMs), reportInt(st.StoppedMs), reportInt(st.FaultMs), reportInt(st.UnknownMs), reportInt(st.CoveredMs), reportNumber(st.ObservedAvail), reportNumber(st.DataCoverage), strconv.FormatBool(st.Insufficient), availabilityRule, start.Format(time.RFC3339Nano), end.Format(time.RFC3339Nano), computedAt, "当前台账（非历史归属）"})
	}
	writeReportCSV(c, "availability.csv", availabilityRule, computedAt, true, header, rows)
}

func (s *Server) timelinessCSV(c *gin.Context, res timelinessResult, start, end time.Time, computedAt string) {
	header := []string{"范围", "指标（平台记录时间）", "有效样本数", "平均(ms)", "中位数(ms)", "P90(ms)", "未确认", "未恢复", "管理关闭", "历史或原因未知", "事件总数", "异常生命周期", "规则版本", "开始", "结束", "计算时间"}
	rows := [][]string{}
	for _, metric := range []struct {
		name string
		stat timelinessStat
	}{{"告警确认耗时", res.AckTimeliness}, {"平台告警恢复耗时（合计）", res.RecoverTimeliness}, {"遥测恢复耗时", res.TelemetryRecoverTimeliness}, {"预测风险下降耗时", res.PredictionRecoverTimeliness}} {
		rows = append(rows, []string{res.Scope, metric.name, strconv.Itoa(metric.stat.Count), reportNumber(metric.stat.MeanMs), reportNumber(metric.stat.MedianMs), reportNumber(metric.stat.P90Ms), strconv.Itoa(res.PendingAck), strconv.Itoa(res.PendingRecover), strconv.Itoa(res.ManagedRecover), strconv.Itoa(res.UnknownRecover), strconv.Itoa(res.EventCount), strconv.Itoa(res.InvalidLifecycle), "timeliness-v1", start.Format(time.RFC3339Nano), end.Format(time.RFC3339Nano), computedAt})
	}
	writeReportCSV(c, "alarm-timeliness.csv", "timeliness-v1", computedAt, res.InvalidLifecycle == 0, header, rows)
}
