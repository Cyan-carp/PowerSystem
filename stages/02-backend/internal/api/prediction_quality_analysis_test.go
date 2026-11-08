package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"powersystem/backend/internal/config"
)

func qualityAnalysisTestServer(t *testing.T, handler http.HandlerFunc) *Server {
	t.Helper()
	keyFile := filepath.Join(t.TempDir(), "quality-key")
	if err := os.WriteFile(keyFile, []byte("fixture-key\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	upstream := httptest.NewServer(handler)
	t.Cleanup(upstream.Close)
	return &Server{cfg: config.Config{
		QualityLLMKeyFile:  keyFile,
		QualityLLMBaseURL:  upstream.URL,
		QualityLLMModel:    "deepseek-flash",
		QualityLLMThinking: "disabled",
	}}
}

func TestQualityAnalysisRetriesBlankJSONWithoutThinking(t *testing.T) {
	var modes []string
	var jsonModes []bool
	s := qualityAnalysisTestServer(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/chat/completions" || r.Header.Get("Authorization") != "Bearer fixture-key" {
			t.Errorf("unexpected request path or authorization")
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		var body struct {
			Thinking struct {
				Type string `json:"type"`
			} `json:"thinking"`
			ResponseFormat *struct {
				Type string `json:"type"`
			} `json:"response_format"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		modes = append(modes, body.Thinking.Type)
		jsonModes = append(jsonModes, body.ResponseFormat != nil)
		content := "   " // DeepSeek JSON mode can return spaces even with finish_reason=stop.
		completion := 1200
		if len(modes) == 2 {
			content = `{"conclusion":"数据不足","reasons":["样本太少"],"suggestions":["继续观察"]}`
			completion = 200
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"choices": []any{map[string]any{
				"finish_reason": "stop",
				"message":       map[string]any{"content": content},
			}},
			"usage": map[string]any{"prompt_tokens": 11, "completion_tokens": completion},
		})
	})
	content, prompt, completion, err := s.callQualityLLM(context.Background(), "JSON", "facts")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(content, "数据不足") || prompt != 22 || completion != 1400 {
		t.Fatalf("content=%q prompt=%d completion=%d", content, prompt, completion)
	}
	if len(modes) != 2 || modes[0] != "disabled" || modes[1] != "disabled" ||
		!jsonModes[0] || jsonModes[1] {
		t.Fatalf("thinking modes=%v json modes=%v", modes, jsonModes)
	}
}

func TestQualityAnalysisStopsAfterTwoBlankResponses(t *testing.T) {
	requests := 0
	s := qualityAnalysisTestServer(t, func(w http.ResponseWriter, _ *http.Request) {
		requests++
		_ = json.NewEncoder(w).Encode(map[string]any{
			"choices": []any{map[string]any{"message": map[string]any{"content": " \n "}}},
			"usage":   map[string]any{"prompt_tokens": 7, "completion_tokens": 9},
		})
	})
	_, prompt, completion, err := s.callQualityLLM(context.Background(), "JSON", "facts")
	if err == nil || !strings.Contains(err.Error(), "模型未返回可用正文") {
		t.Fatalf("expected clear empty-output error, got %v", err)
	}
	if requests != 2 || prompt != 14 || completion != 18 {
		t.Fatalf("requests=%d prompt=%d completion=%d", requests, prompt, completion)
	}
}
