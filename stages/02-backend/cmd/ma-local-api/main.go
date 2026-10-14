// Local MA acceptance proxy: no MQTT ingestion, prediction or notification workers.
package main

import (
	"go.uber.org/zap"
	"log"
	"net/http"
	"powersystem/backend/internal/api"
	"powersystem/backend/internal/config"
	"powersystem/backend/internal/store"
	"powersystem/backend/internal/tdengine"
	"strings"
)

func main() {
	cfg, err := config.Load("api")
	if err != nil {
		log.Fatal("invalid local configuration")
	}
	if !strings.HasPrefix(cfg.HTTPAddr, "127.0.0.1:") {
		log.Fatal("local bind required")
	}
	db, err := store.Postgres(cfg)
	if err != nil {
		log.Fatal("database unavailable")
	}
	rd, err := store.Redis(cfg)
	if err != nil {
		log.Fatal("redis unavailable")
	}
	defer rd.Close()
	logger := zap.NewNop()
	srv := api.New(cfg, db, rd, tdengine.New(cfg), logger)
	log.Fatal(http.ListenAndServe(cfg.HTTPAddr, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p := r.URL.Path
		allowed := r.Method == "GET" || p == "/api/v1/auth/login" || p == "/api/v1/agent/chat" || strings.HasSuffix(p, "/verify") || strings.HasPrefix(p, "/api/v1/agent/tasks/") && (strings.HasSuffix(p, "/cancel") || r.Method == "POST" && strings.Contains(p, "/approvals/") && strings.HasSuffix(p, "/decision")) || r.Method == "DELETE" && strings.HasPrefix(p, "/api/v1/agent/sessions/")
		if !allowed {
			w.WriteHeader(403)
			return
		}
		srv.Handler().ServeHTTP(w, r)
	})))
}
