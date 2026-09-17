package controller

import (
	"strings"
	"testing"
)

func TestMappingAffectsReplayTransport(t *testing.T) {
	seen := []string{}
	c := mockConsole(t, func(s string) string { seen = append(seen, s); return "OK" })
	m := Mapping{Buttons: map[string]string{"FIRE": "KEYCODE_BUTTON_A"}, Axes: map[string]string{"STEER": "AXIS_X"}}
	if err := c.Authenticate("abc123"); err != nil {
		t.Fatal(err)
	}
	if err := Replay(strings.NewReader(`{"type":"button","key":"FIRE","action":"down","t_ms":1}`+"\n"), c, m, nil); err != nil {
		t.Fatal(err)
	}
	if strings.Join(seen, "|") != "auth abc123|event send EV_KEY:BTN_A:1|event send EV_SYN:0:0" {
		t.Fatalf("mapped framing: %v", seen)
	}
}
