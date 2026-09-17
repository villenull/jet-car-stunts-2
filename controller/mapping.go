package controller

import (
	"encoding/json"
	"fmt"
	"os"
)

type Mapping struct {
	Deadzone float64           `json:"deadzone"`
	Buttons  map[string]string `json:"buttons"`
	Axes     map[string]string `json:"axes"`
}

func LoadMapping(path string) (Mapping, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return Mapping{}, err
	}
	var m Mapping
	if err := json.Unmarshal(b, &m); err != nil {
		return Mapping{}, err
	}
	if m.Buttons == nil || m.Axes == nil {
		return Mapping{}, fmt.Errorf("mapping must define buttons and axes")
	}
	return m, nil
}

// ApplyMapping accepts either human aliases (A/LX) or already mapped Android
// names. This keeps replay files portable while ensuring mapping.json changes
// the actual commands sent to the guest.
func (m Mapping) ApplyMapping(e Event) (Event, error) {
	switch e.Type {
	case "button":
		if v, ok := m.Buttons[e.Key]; ok {
			e.Key = v
		}
		if e.Key == "" {
			return e, fmt.Errorf("empty button key")
		}
	case "axis":
		if v, ok := m.Axes[e.Axis]; ok {
			e.Axis = v
		}
		if e.Axis == "" {
			return e, fmt.Errorf("empty axis")
		}
	}
	return e, nil
}
