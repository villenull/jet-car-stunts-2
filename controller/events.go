package controller

import "math"

type Event struct {
	Type      string  `json:"type"`
	Axis      string  `json:"axis,omitempty"`
	Key       string  `json:"key,omitempty"`
	Action    string  `json:"action,omitempty"`
	Value     float64 `json:"value,omitempty"`
	Connected *bool   `json:"connected,omitempty"`
	TMS       int64   `json:"t_ms"`
}

func Deadzone(v, dz float64) float64 {
	if math.Abs(v) <= dz {
		return 0
	}
	s := 1.0
	if v < 0 {
		s = -1
	}
	return s * (math.Abs(v) - dz) / (1 - dz)
}

type ButtonState struct{ Held map[string]bool }

func (s *ButtonState) Changes(next map[string]bool, now int64) []Event {
	if s.Held == nil {
		s.Held = map[string]bool{}
	}
	out := make([]Event, 0)
	for key, old := range s.Held {
		if old && !next[key] {
			out = append(out, Event{Type: "button", Key: key, Action: "up", TMS: now})
		}
	}
	for key, on := range next {
		if on && !s.Held[key] {
			out = append(out, Event{Type: "button", Key: key, Action: "down", TMS: now})
		}
	}
	s.Held = next
	return out
}

func (s *ButtonState) Release(now int64) []Event {
	return s.Changes(map[string]bool{}, now)
}
