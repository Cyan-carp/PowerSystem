package service

import "testing"

func TestMatchesAtThreshold(t *testing.T) {
	for _, tc := range []struct {
		op   string
		want bool
	}{
		{">", false}, {">=", true}, {"<", false}, {"<=", true}, {"invalid", false},
	} {
		if got := Matches(tc.op, 80, 80); got != tc.want {
			t.Errorf("Matches(%q,80,80)=%v; want %v", tc.op, got, tc.want)
		}
	}
}
