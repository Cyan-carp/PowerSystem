package main

import (
	"testing"
	"time"
)

func TestOfflineMigrationBudgetCannotRelaxServerStartup(t *testing.T) {
	if _, err := migrationStartupBudget(false, 15*time.Minute); err == nil {
		t.Fatal("normal API accepted an extended schema budget")
	}
	if got, err := migrationStartupBudget(false, 30*time.Second); err != nil || got != 30*time.Second {
		t.Fatal("normal startup budget changed", got, err)
	}
	if got, err := migrationStartupBudget(true, 15*time.Minute); err != nil || got != 15*time.Minute {
		t.Fatal("bounded offline operation refused", got, err)
	}
	for _, budget := range []time.Duration{0, -time.Second, 29 * time.Second, 15*time.Minute + time.Second} {
		if _, err := migrationStartupBudget(true, budget); err == nil {
			t.Fatal("unbounded/invalid offline budget accepted", budget)
		}
	}
}
