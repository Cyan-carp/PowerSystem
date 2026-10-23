package api

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestV2TypedFactsAndSourceBinding(t *testing.T) {
	var evidence []eventEvidence
	err := json.Unmarshal([]byte(`[{"id":"E1","tool":"get_alarm_detail","status":"ok","data":{"metric":"ai_failure_risk","value":0.94,"threshold":0.35,"triggered_at":"2026-10-21T07:48:19Z"}},{"id":"E2","tool":"get_prediction","status":"ok","data":{"probability":0.94,"threshold":0.35,"window_end_ms":1792570800000}},{"id":"E3","tool":"get_telemetry","status":"ok","data":{"metric":"temperature","statistics":{"mean":42.5},"points":[[1,999]]}}]`), &evidence)
	if err != nil {
		t.Fatal(err)
	}
	facts := eventFacts(evidence)
	roles := map[string]interpretationFact{}
	for _, f := range facts {
		roles[f.Role] = f
		if strings.Contains(f.Path, "points") {
			t.Fatal("raw curve flattened into facts")
		}
	}
	if roles["prediction_probability"].Value != .94 || roles["prediction_threshold"].Value != .35 || roles["mean"].Unit != "℃" || roles["triggered_at"].Timezone != "Asia/Shanghai" {
		t.Fatalf("fact roles lost: %#v", roles)
	}
	claim := factTemplate{Template: "原始事实为{{" + roles["prediction_probability"].ID + "}}。", Refs: []string{roles["prediction_probability"].ID}}
	rendered, err := renderFactClaim(claim, facts)
	if err != nil || !strings.Contains(rendered["text"].(string), "预测概率：0.94") {
		t.Fatalf("render failed: %v %v", rendered, err)
	}
}

func TestV2RendererRejectsUnknownAndRawNumericClaims(t *testing.T) {
	facts := []interpretationFact{{ID: "F1", EvidenceID: "E1", Label: "预测概率", Display: "0.94"}}
	for _, claim := range []factTemplate{{"温度99度", []string{"F1"}}, {"{{F2}}", []string{"F1"}}, {"{{F1}}", []string{"F2"}}, {"{{bad}}", []string{"F1"}}} {
		if _, err := renderFactClaim(claim, facts); err == nil {
			t.Fatalf("accepted invalid claim: %+v", claim)
		}
	}
}

func TestV2DerivedFactProvenanceAndRetryBudget(t *testing.T) {
	evidence := []eventEvidence{{ID: "E1", Tool: "get_alarm_detail", Status: "ok", Data: map[string]any{"metric": "temperature", "value": 45.0, "threshold": 40.0}}}
	facts := eventFacts(evidence)
	found := false
	for _, f := range facts {
		if f.Role == "alarm_delta" {
			found = true
			if f.Value != 5.0 || f.Formula != "subtract" || len(f.Inputs) != 2 {
				t.Fatal("derived provenance lost")
			}
		}
	}
	if !found {
		t.Fatal("missing deterministic difference")
	}
	for _, reason := range []string{"model_non_json", "answer_schema_invalid", "rate_limited", "answer_truncated"} {
		if !retryV2(reason, 1) || retryV2(reason, 2) {
			t.Fatal("call budget violated", reason)
		}
	}
	for _, reason := range []string{"unsupported_numeric_claim", "prediction_value_changed", "auth_failed", "invalid_fact_reference"} {
		if retryV2(reason, 1) {
			t.Fatal("unsafe retry", reason)
		}
	}
	if providerFailure("answer_truncated") || providerFailure("unsupported_numeric_claim") {
		t.Fatal("business failures must not open supplier circuit")
	}
}
