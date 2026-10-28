package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"time"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"powersystem/backend/internal/service"
)

func main() {
	id := flag.Int64("inbox-id", 0, "one explicit inbox ID; no ranges")
	request := flag.String("request-id", "", "stable audited idempotency key")
	actor := flag.String("actor", "", "operator identity")
	reason := flag.String("reason", "", "reviewed compensation reason")
	replay := flag.Bool("isolated-replay", false, "explicit replay ONLY in S03 isolated database; default inspect")
	flag.Parse()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	db, err := gorm.Open(postgres.Open(os.Getenv("TR_COMPENSATION_DSN")), &gorm.Config{DisableAutomaticPing: true, Logger: logger.Default.LogMode(logger.Silent)})
	if err == nil {
		pool, e := db.DB()
		if e != nil {
			err = e
		} else {
			defer pool.Close()
			err = pool.PingContext(ctx)
		}
	}
	if err == nil {
		var outcome string
		outcome, err = service.CompensateTelemetry(db.WithContext(ctx), *id, *request, *actor, *reason, *replay)
		if err == nil {
			fmt.Println(outcome)
			return
		}
	}
	fmt.Fprintln(os.Stderr, "compensation failed:", err)
	os.Exit(1)
}
