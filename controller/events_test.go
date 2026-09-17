package controller

import "testing"

func TestDeadzoneRescalesOutsideRange(t *testing.T) {
	if got := Deadzone(0.1, 0.12); got != 0 {
		t.Fatalf("inside deadzone: %v", got)
	}
	got := Deadzone(0.56, 0.12)
	if got < 0.49 || got > 0.51 {
		t.Fatalf("rescaled value: %v", got)
	}
	if Deadzone(-1, 0.12) != -1 {
		t.Fatal("negative endpoint")
	}
}

func TestButtonTransitionsAndDisconnectRelease(t *testing.T) {
	s := ButtonState{}
	if got := s.Changes(map[string]bool{"KEYCODE_BUTTON_A": true}, 1); len(got) != 1 || got[0].Action != "down" {
		t.Fatalf("down: %#v", got)
	}
	if got := s.Changes(map[string]bool{"KEYCODE_BUTTON_A": true}, 2); len(got) != 0 {
		t.Fatalf("repeat: %#v", got)
	}
	if got := s.Release(3); len(got) != 1 || got[0].Action != "up" {
		t.Fatalf("release: %#v", got)
	}
}

func TestDisconnectReconnectDoesNotStickKeys(t *testing.T) {
	s := ButtonState{}
	_ = s.Changes(map[string]bool{"KEYCODE_BUTTON_A": true}, 1)
	released := s.Release(2)
	if len(released) != 1 || released[0].Action != "up" {
		t.Fatalf("disconnect release: %#v", released)
	}
	reconnected := s.Changes(map[string]bool{"KEYCODE_BUTTON_A": true}, 3)
	if len(reconnected) != 1 || reconnected[0].Action != "down" {
		t.Fatalf("reconnect down: %#v", reconnected)
	}
}
