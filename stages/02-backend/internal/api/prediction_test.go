package api

import (
	"testing"
	"time"
)

func TestParseTDTime(t *testing.T) {
	want := time.Date(2026, 9, 28, 1, 2, 3, 456000000, time.Local).UnixMilli()
	for _, input := range []any{want, float64(want), "2026-09-28 01:02:03.456"} {
		got, err := parseTDTime(input)
		if err != nil || got != want {
			t.Fatalf("input=%v got=%d err=%v want=%d", input, got, err, want)
		}
	}
	if _, err := parseTDTime("not-a-time"); err == nil {
		t.Fatal("malformed TDengine timestamp was accepted")
	}
}
