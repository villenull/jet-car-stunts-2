package controller

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

type ReadyMarker struct {
	Serial    string `json:"serial"`
	Transport string `json:"transport"`
	DeviceID  int    `json:"device_id"`
}

func WriteReadyFile(path, serial string, h *Helper) error {
	if path == "" {
		return nil
	}
	if !filepath.IsAbs(path) {
		return fmt.Errorf("ready-file must be absolute")
	}
	if h == nil {
		return fmt.Errorf("ready-file requires helper")
	}
	b, err := json.Marshal(ReadyMarker{Serial: serial, Transport: "uhid", DeviceID: h.deviceID})
	if err != nil {
		return err
	}
	b = append(b, '\n')
	tmp := path + ".tmp"
	if err = os.WriteFile(tmp, b, 0600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
