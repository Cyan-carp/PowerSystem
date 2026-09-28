package api

import "testing"

func TestOriginAllowed(t *testing.T) {
	tests := []struct {
		name, origin, host string
		extra              []string
		want               bool
	}{
		{"same HTTP origin", "http://127.0.0.1:8080", "127.0.0.1:8080", nil, true},
		{"same HTTPS origin", "https://ops.example.test", "ops.example.test", nil, true},
		{"configured Vite origin", "http://127.0.0.1:5173", "127.0.0.1:8080", []string{"http://127.0.0.1:5173"}, true},
		{"unconfigured origin", "http://evil.example.test", "127.0.0.1:8080", nil, false},
		{"similar hostname", "http://127.0.0.1:8080.evil.test", "127.0.0.1:8080", nil, false},
		{"no browser origin", "", "127.0.0.1:8080", nil, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := originAllowed(tt.origin, tt.host, tt.extra); got != tt.want {
				t.Fatalf("originAllowed(%q, %q) = %v, want %v", tt.origin, tt.host, got, tt.want)
			}
		})
	}
}
