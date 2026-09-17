package controller

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"
)

func fakeADB(t *testing.T, ready bool) string {
	t.Helper()
	d := t.TempDir()
	p := filepath.Join(d, "adb")
	mode := "NOPE"
	if ready {
		mode = "READY JCS2_UHID 7"
	}
	script := `#!/bin/sh
case "$*" in
  *push*) exit 0 ;;
  *id*) echo 'uid=2000(shell) gid=2000(shell)'; exit 0 ;;
esac
echo '` + mode + `'
while IFS= read -r line; do
  [ "$line" = quit ] && exit 0
  echo ACK
done
`
	if err := os.WriteFile(p, []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestHelperRejectsUnknownAndBadAxes(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("fake shell")
	}
	jar := filepath.Join(t.TempDir(), "h.jar")
	if err := os.WriteFile(jar, []byte("jar"), 0600); err != nil {
		t.Fatal(err)
	}
	h, err := StartHelper(fakeADB(t, true), "5038", "serial", jar)
	if err != nil {
		t.Fatal(err)
	}
	defer h.Close()
	if err := h.Send(Event{Type: "axis", Axis: "UNKNOWN", Value: 0}); err == nil {
		t.Fatal("unknown axis accepted")
	}
	if err := h.Send(Event{Type: "axis", Axis: "AXIS_X", Value: 2}); err == nil {
		t.Fatal("out of range accepted")
	}
	if err := h.Send(Event{Type: "axis", Axis: "AXIS_X", Value: 0.25}); err != nil {
		t.Fatal(err)
	}
}

func TestStartHelperMissingJarAndNoReady(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("fake shell")
	}
	if _, err := StartHelper(fakeADB(t, true), "5038", "serial", filepath.Join(t.TempDir(), "missing.jar")); err == nil {
		t.Fatal("missing jar accepted")
	}
	jar := filepath.Join(t.TempDir(), "h.jar")
	if err := os.WriteFile(jar, []byte("jar"), 0600); err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	if _, err := StartHelper(fakeADB(t, false), "5038", "serial", jar); err == nil {
		t.Fatal("no READY accepted")
	} else if time.Since(start) > 7*time.Second {
		t.Fatal("readiness unbounded")
	}
}
