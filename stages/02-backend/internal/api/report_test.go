package api

import (
	"math"
	"testing"
)

func TestComputeAvailabilityKnownWindows(t *testing.T) {
	// 两个样本间隔 10s，窗口 60s，覆盖 20s（首样本向后 10s，次样本向后 15s 上限被窗口截断）。
	samples := []samplePoint{{TS: 1000, Status: 1}, {TS: 11000, Status: 0}}
	start, end := int64(0), int64(60000)
	res := computeAvailability(samples, start, end)
	if res.RunningMs != 10000 {
		t.Fatalf("running=%d, want 10000", res.RunningMs)
	}
	if res.StoppedMs != 15000 {
		t.Fatalf("stopped=%d, want 15000", res.StoppedMs)
	}
	if res.FaultMs != 0 {
		t.Fatalf("fault=%d, want 0", res.FaultMs)
	}
	if res.ObservedAvail == nil || math.Abs(*res.ObservedAvail-1.0) > 1e-9 {
		t.Fatalf("availability=%v, want 1.0", res.ObservedAvail)
	}
	if res.DataCoverage == nil || math.Abs(*res.DataCoverage-25.0/60.0) > 1e-9 {
		t.Fatalf("coverage=%v, want 25/60", res.DataCoverage)
	}
	if !res.Insufficient {
		t.Fatal("coverage<90% must mark insufficient")
	}
}

func TestComputeAvailabilityFaultExcluded(t *testing.T) {
	// 故障样本：非故障时长应排除 status=2。
	samples := []samplePoint{{TS: 0, Status: 2}, {TS: 10000, Status: 1}}
	start, end := int64(0), int64(30000)
	res := computeAvailability(samples, start, end)
	if res.FaultMs != 10000 {
		t.Fatalf("fault=%d, want 10000", res.FaultMs)
	}
	if res.ObservedAvail == nil || math.Abs(*res.ObservedAvail-15.0/25.0) > 1e-9 {
		t.Fatalf("availability=%v, want 15/25", res.ObservedAvail)
	}
}

func TestComputeAvailabilityEmpty(t *testing.T) {
	res := computeAvailability(nil, 0, 60000)
	if res.ObservedAvail != nil {
		t.Fatalf("empty window must have nil availability, got %v", res.ObservedAvail)
	}
	if !res.Insufficient {
		t.Fatal("empty window must be insufficient")
	}
}

func TestComputeTimelinessStats(t *testing.T) {
	stat := computeTimeliness([]float64{100, 200, 300, 400, 500})
	if stat.Count != 5 {
		t.Fatalf("count=%d, want 5", stat.Count)
	}
	if stat.MeanMs == nil || math.Abs(*stat.MeanMs-300) > 1e-9 {
		t.Fatalf("mean=%v, want 300", stat.MeanMs)
	}
	if stat.MedianMs == nil || math.Abs(*stat.MedianMs-300) > 1e-9 {
		t.Fatalf("median=%v, want 300", stat.MedianMs)
	}
	if stat.P90Ms == nil || math.Abs(*stat.P90Ms-500) > 1e-9 {
		t.Fatalf("p90=%v, want 500", stat.P90Ms)
	}
}

func TestNextVersion(t *testing.T) {
	cases := map[string]string{"v1": "v2", "v9": "v10", "": "v1", "abc": "v1"}
	for in, want := range cases {
		if got := nextVersion(in); got != want {
			t.Fatalf("nextVersion(%q)=%q, want %q", in, got, want)
		}
	}
}
