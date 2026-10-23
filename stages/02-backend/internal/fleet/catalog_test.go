package fleet

import (
	"path/filepath"
	"strings"
	"testing"
)

const testManifest = `{
  "schema_version": 1,
  "fleet_id": "test-fleet",
  "sampling_seconds": 5,
  "stations": [
    {"station_code":"PV-01","station_name":"A","scope":"business","kind":"pv","device_prefix":"INV","device_count":48,"rated_power_kw":100,"profile":"generic-pv-100kw","daylight_mode":"real","group_size":8},
    {"station_code":"ST-01","station_name":"B","scope":"baseline","kind":"pv","explicit":["INV-1001","INV-1002","INV-1003"],"daylight_mode":"loop","daylight_loop_minutes":60}
  ]
}`

func TestFleetCatalogReadsSharedManifestInOrder(t *testing.T) {
	catalog, err := Load(filepath.Join("..", "..", "..", "01-data-chain", "simulator", "fleet.json"))
	if err != nil {
		t.Fatalf("load shared manifest: %v", err)
	}
	if catalog.FleetID != "pv-fleet-140-v1" || catalog.SamplingSeconds != 5 {
		t.Fatalf("unexpected fleet header: %+v", catalog)
	}
	wantCodes := []string{"PV-01", "PV-02", "PV-03", "PV-04", "ST-01"}
	wantCounts := []int{48, 40, 32, 20, 3}
	if len(catalog.Stations) != len(wantCodes) {
		t.Fatalf("stations=%d, want %d", len(catalog.Stations), len(wantCodes))
	}
	for i, station := range catalog.Stations {
		if station.Code != wantCodes[i] {
			t.Fatalf("stations[%d]=%q, want %q (manifest order)", i, station.Code, wantCodes[i])
		}
		if station.ExpectedDevices != wantCounts[i] {
			t.Fatalf("%s expected devices=%d, want %d", station.Code, station.ExpectedDevices, wantCounts[i])
		}
	}
	if got := catalog.ExpectedDevices(ScopeAll); got != 143 {
		t.Fatalf("all expected devices=%d, want 143", got)
	}
	if got := catalog.ExpectedDevices(ScopeBusiness); got != 140 {
		t.Fatalf("business expected devices=%d, want 140", got)
	}
	if got := catalog.ExpectedDevices(ScopeBaseline); got != 3 {
		t.Fatalf("baseline expected devices=%d, want 3", got)
	}
	if got := strings.Join(catalog.CodesForScope(ScopeBusiness), ","); got != "PV-01,PV-02,PV-03,PV-04" {
		t.Fatalf("business codes=%s", got)
	}
	if got := strings.Join(catalog.CodesForScope(ScopeBaseline), ","); got != "ST-01" {
		t.Fatalf("baseline codes=%s", got)
	}
	if scope, ok := catalog.ScopeOf("PV-03"); !ok || scope != ScopeBusiness {
		t.Fatalf("PV-03 scope=%q ok=%v", scope, ok)
	}
	if _, ok := catalog.Station("PV-05"); ok {
		t.Fatal("unexpected station PV-05")
	}
}

func TestFleetCatalogRejectsInvalidManifests(t *testing.T) {
	cases := []struct {
		name string
		raw  string
	}{
		{"unknown top-level field", strings.Replace(testManifest, `"fleet_id"`, `"extra_top": 1, "fleet_id"`, 1)},
		{"unknown station field", strings.Replace(testManifest, `"kind":"pv"`, `"kind":"pv","extra":1`, 1)},
		{"missing schema_version", strings.Replace(testManifest, `"schema_version": 1,`, "", 1)},
		{"unsupported schema_version", strings.Replace(testManifest, `"schema_version": 1`, `"schema_version": 2`, 1)},
		{"empty fleet_id", strings.Replace(testManifest, `"test-fleet"`, `""`, 1)},
		{"zero sampling_seconds", strings.Replace(testManifest, `"sampling_seconds": 5`, `"sampling_seconds": 0`, 1)},
		{"duplicate station code", strings.Replace(testManifest, `"ST-01"`, `"PV-01"`, 1)},
		{"invalid station code", strings.Replace(testManifest, `"PV-01"`, `"PV 01"`, 1)},
		{"invalid scope", strings.Replace(testManifest, `"scope":"business"`, `"scope":"ops"`, 1)},
		{"business without device_count", strings.Replace(testManifest, `"device_count":48,`, "", 1)},
		{"business with explicit", strings.Replace(testManifest, `"device_count":48,`, `"device_count":48,"explicit":["INV-1"],`, 1)},
		{"business without profile", strings.Replace(testManifest, `"profile":"generic-pv-100kw",`, "", 1)},
		{"baseline without explicit", strings.Replace(testManifest, `"explicit":["INV-1001","INV-1002","INV-1003"],`, "", 1)},
		{"baseline with device_count", strings.Replace(testManifest, `"explicit":["INV-1001","INV-1002","INV-1003"],`, `"explicit":["INV-1001","INV-1002","INV-1003"],"device_count":3,`, 1)},
		{"duplicate explicit device", strings.Replace(testManifest, `"INV-1002"`, `"INV-1001"`, 1)},
		{"invalid explicit device", strings.Replace(testManifest, `"INV-1002"`, `"INV 1002"`, 1)},
		{"invalid daylight_mode", strings.Replace(testManifest, `"daylight_mode":"real"`, `"daylight_mode":"fast"`, 1)},
		{"loop without minutes", strings.Replace(testManifest, `"daylight_loop_minutes":60`, `"daylight_loop_minutes":0`, 1)},
		{"non-integer device_count", strings.Replace(testManifest, `"device_count":48`, `"device_count":1.5`, 1)},
		{"trailing json", testManifest + `{}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := Parse(strings.NewReader(tc.raw)); err == nil {
				t.Fatalf("expected error for %s", tc.name)
			}
		})
	}
	if _, err := Parse(strings.NewReader(`{"schema_version":1,"fleet_id":"f","sampling_seconds":5,"stations":[]}`)); err == nil {
		t.Fatal("expected error for empty stations")
	}
	if _, err := Parse(strings.NewReader("")); err == nil {
		t.Fatal("expected error for empty document")
	}
}

func TestFleetCatalogMissingFile(t *testing.T) {
	if _, err := Load(filepath.Join(t.TempDir(), "missing.json")); err == nil {
		t.Fatal("expected error for missing manifest")
	}
}

func TestFleetCatalogAcceptsValidTestManifest(t *testing.T) {
	catalog, err := Parse(strings.NewReader(testManifest))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if catalog.ExpectedDevices(ScopeAll) != 51 {
		t.Fatalf("expected devices=%d, want 51", catalog.ExpectedDevices(ScopeAll))
	}
	if got := catalog.ExpectedDevices(ScopeBusiness); got != 48 {
		t.Fatalf("business devices=%d, want 48", got)
	}
	if got := catalog.ExpectedDevices(ScopeBaseline); got != 3 {
		t.Fatalf("baseline devices=%d, want 3", got)
	}
}

func TestFleetScopeNormalization(t *testing.T) {
	for raw, want := range map[string]string{"": ScopeAll, "all": ScopeAll, "business": ScopeBusiness, "baseline": ScopeBaseline} {
		got, err := NormalizeScope(raw)
		if err != nil || got != want {
			t.Fatalf("NormalizeScope(%q)=%q,%v want %q", raw, got, err, want)
		}
	}
	for _, raw := range []string{"ops", "Business", " all", "business "} {
		if _, err := NormalizeScope(raw); err == nil {
			t.Fatalf("NormalizeScope(%q) should fail", raw)
		}
	}
}
