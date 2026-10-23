package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/gin-gonic/gin"

	"powersystem/backend/internal/fleet"
)

func testFleetCatalog(t *testing.T) *fleet.Catalog {
	t.Helper()
	catalog, err := fleet.Load(filepath.Join("..", "..", "..", "01-data-chain", "simulator", "fleet.json"))
	if err != nil {
		t.Fatalf("load shared manifest: %v", err)
	}
	return catalog
}

func TestScopeFilterRules(t *testing.T) {
	catalog := testFleetCatalog(t)
	businessCodes := []string{"PV-01", "PV-02", "PV-03", "PV-04"}
	cases := []struct {
		name    string
		scope   string
		station string
		want    scopeSelection
		status  int
	}{
		{"omitted scope is all", "", "", scopeSelection{Scope: fleet.ScopeAll}, 0},
		{"explicit all", "all", "", scopeSelection{Scope: fleet.ScopeAll}, 0},
		{"business", "business", "", scopeSelection{Scope: fleet.ScopeBusiness, StationCodes: businessCodes}, 0},
		{"baseline", "baseline", "", scopeSelection{Scope: fleet.ScopeBaseline, StationCodes: []string{"ST-01"}}, 0},
		{"station only", "", "PV-02", scopeSelection{Scope: fleet.ScopeAll, StationCode: "PV-02", StationCodes: []string{"PV-02"}}, 0},
		{"business station", "business", "PV-01", scopeSelection{Scope: fleet.ScopeBusiness, StationCode: "PV-01", StationCodes: []string{"PV-01"}}, 0},
		{"baseline station", "baseline", "ST-01", scopeSelection{Scope: fleet.ScopeBaseline, StationCode: "ST-01", StationCodes: []string{"ST-01"}}, 0},
		{"invalid scope", "ops", "", scopeSelection{}, http.StatusBadRequest},
		{"invalid station code", "all", "bad code", scopeSelection{}, http.StatusBadRequest},
		{"station scope conflict", "business", "ST-01", scopeSelection{}, http.StatusBadRequest},
		{"unknown station with scope", "baseline", "PV-01", scopeSelection{}, http.StatusBadRequest},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := resolveScopeFilter(catalog, tc.scope, tc.station)
			if tc.status != 0 {
				if err == nil || err.status != tc.status {
					t.Fatalf("want status %d, got %+v", tc.status, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("selection=%+v, want %+v", got, tc.want)
			}
		})
	}
}

func TestScopeWithoutCatalog(t *testing.T) {
	for _, scope := range []string{"business", "baseline"} {
		if _, err := resolveScopeFilter(nil, scope, ""); err == nil || err.status != http.StatusServiceUnavailable {
			t.Fatalf("scope=%s without catalog should return 503, got %+v", scope, err)
		}
	}
	if _, err := resolveScopeFilter(nil, "business", "PV-01"); err == nil || err.status != http.StatusServiceUnavailable {
		t.Fatalf("station+scope without catalog should return 503, got %+v", err)
	}
	selection, err := resolveScopeFilter(nil, "all", "PV-01")
	if err != nil || !reflect.DeepEqual(selection.StationCodes, []string{"PV-01"}) {
		t.Fatalf("all+station should work without catalog: %+v %v", selection, err)
	}
	if selection, err := resolveScopeFilter(nil, "", ""); err != nil || selection.StationCodes != nil {
		t.Fatalf("default all should not filter: %+v %v", selection, err)
	}
}

func TestScopeDevicesRequestFailsWhenCatalogUnavailable(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	s := &Server{}
	r.GET("/devices", s.listDevices)
	r.GET("/stations", s.listStations)

	for _, target := range []string{"/devices?scope=business", "/devices?scope=baseline", "/stations"} {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, target, nil))
		if w.Code != http.StatusServiceUnavailable {
			t.Fatalf("%s status=%d, want 503: %s", target, w.Code, w.Body.String())
		}
		var body struct {
			Code int `json:"code"`
		}
		if json.Unmarshal(w.Body.Bytes(), &body) != nil || body.Code != fleetUnavailableCode {
			t.Fatalf("%s body=%s", target, w.Body.String())
		}
	}
}

func TestStationRowsFollowManifestOrder(t *testing.T) {
	catalog := testFleetCatalog(t)
	registered := map[string]int64{"PV-01": 48, "PV-03": 30, "ST-01": 3}

	all := stationRows(catalog, fleet.ScopeAll, registered)
	if len(all) != 5 {
		t.Fatalf("all rows=%d, want 5", len(all))
	}
	wantCodes := []string{"PV-01", "PV-02", "PV-03", "PV-04", "ST-01"}
	wantExpected := []int{48, 40, 32, 20, 3}
	wantRegistered := []int64{48, 0, 30, 0, 3}
	for i, row := range all {
		if row.StationCode != wantCodes[i] || row.ExpectedDeviceCount != wantExpected[i] || row.RegisteredDeviceCount != wantRegistered[i] {
			t.Fatalf("row[%d]=%+v", i, row)
		}
	}

	business := stationRows(catalog, fleet.ScopeBusiness, registered)
	if len(business) != 4 {
		t.Fatalf("business rows=%d, want 4", len(business))
	}
	for _, row := range business {
		if row.Scope != fleet.ScopeBusiness {
			t.Fatalf("business row has scope %q", row.Scope)
		}
	}

	baseline := stationRows(catalog, fleet.ScopeBaseline, registered)
	if len(baseline) != 1 || baseline[0].StationCode != "ST-01" || baseline[0].RegisteredDeviceCount != 3 {
		t.Fatalf("baseline rows=%+v", baseline)
	}
	if baseline[0].StationName != "原三设备演示基线站" {
		t.Fatalf("baseline name=%q", baseline[0].StationName)
	}
}

func TestStationRowFieldsAreFixed(t *testing.T) {
	catalog := testFleetCatalog(t)
	rows := stationRows(catalog, fleet.ScopeBaseline, nil)
	raw, err := json.Marshal(rows[0])
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]any
	if err := json.Unmarshal(raw, &fields); err != nil {
		t.Fatal(err)
	}
	want := []string{"station_code", "station_name", "scope", "kind", "expected_device_count", "registered_device_count"}
	if len(fields) != len(want) {
		t.Fatalf("fields=%v, want %v", fields, want)
	}
	for _, name := range want {
		if _, ok := fields[name]; !ok {
			t.Fatalf("missing field %q in %s", name, raw)
		}
	}
	if fields["registered_device_count"].(float64) != 0 {
		t.Fatalf("unregistered station should report 0: %s", raw)
	}
}
