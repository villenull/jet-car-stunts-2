//go:build windows

package main

import (
	"encoding/json"
	"flag"
	"fmt"
	c "jcs2/controller"
	"os"
	"os/signal"
	"syscall"
	"time"
	"unsafe"
)

type xinputGamepad struct {
	Buttons        uint16
	LT, RT         byte
	LX, LY, RX, RY int16
}
type xinputState struct {
	Packet  uint32
	Gamepad xinputGamepad
}

const (
	xDPadUp    = 0x0001
	xDPadDown  = 0x0002
	xDPadLeft  = 0x0004
	xDPadRight = 0x0008
	xStart     = 0x0010
	xBack      = 0x0020
	xLS        = 0x0040
	xRS        = 0x0080
	xLB        = 0x0100
	xRB        = 0x0200
	xA         = 0x1000
	xB         = 0x2000
	xX         = 0x4000
	xY         = 0x8000
)

// XInput is loaded from the Windows system DLL, so no driver or bundled DLL
// is needed. xinput1_4 is preferred on current Windows, with the legacy DLL
// fallback retained for older Windows versions.
func run() int {
	port := flag.Int("console-port", 5596, "emulator console TCP port")
	tokenPath := flag.String("token-file", "", "dedicated emulator console auth token file")
	deadzone := flag.Float64("deadzone", 0.12, "stick deadzone [0,1)")
	mappingPath := flag.String("mapping", "mapping.json", "mapping JSON")
	replayPath := flag.String("replay", "", "NDJSON replay file (use - for stdin)")
	trace := flag.Bool("trace", false, "also write NDJSON events to stdout")
	adbPath := flag.String("adb-path", "adb.exe", "explicit adb executable for helper transport")
	adbPort := flag.String("adb-port", "5038", "dedicated adb server port")
	serial := flag.String("adb-serial", "127.0.0.1:5595", "explicit emulator serial")
	helperJar := flag.String("helper-jar", "", "persistent app_process helper jar; enables UHID transport")
	readyFile := flag.String("ready-file", "", "absolute host JSON readiness marker")
	pollHz := flag.Int("poll-hz", 120, "XInput poll rate")
	flag.Parse()
	if *tokenPath == "" && *helperJar == "" {
		fmt.Fprintln(os.Stderr, "jcs2-controller: --token-file is required")
		return 2
	}
	if *deadzone < 0 || *deadzone >= 1 || *pollHz <= 0 {
		fmt.Fprintln(os.Stderr, "jcs2-controller: invalid deadzone or poll-hz")
		return 2
	}
	mapping, err := c.LoadMapping(*mappingPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "jcs2-controller: mapping:", err)
		return 1
	}
	var sink c.EventSink
	var closeSink func()
	if *helperJar != "" {
		h, err := c.StartHelper(*adbPath, *adbPort, *serial, *helperJar)
		if err != nil {
			fmt.Fprintln(os.Stderr, "jcs2-controller: helper:", err)
			return 1
		}
		if err := c.WriteReadyFile(*readyFile, *serial, h); err != nil {
			fmt.Fprintln(os.Stderr, "jcs2-controller: ready-file:", err)
			_ = h.Close()
			return 1
		}
		sink = h
		closeSink = func() { _ = h.Close() }
	} else {
		console, conn, err := c.ConnectConsole(fmt.Sprintf("127.0.0.1:%d", *port), *tokenPath)
		if err != nil {
			fmt.Fprintln(os.Stderr, "jcs2-controller: console:", err)
			return 1
		}
		sink = console
		closeSink = func() { _ = conn.Close() }
	}
	defer func() {
		closeSink()
		if *readyFile != "" {
			_ = os.Remove(*readyFile)
		}
	}()
	if *replayPath != "" {
		var in *os.File
		if *replayPath == "-" {
			in = os.Stdin
		} else {
			in, err = os.Open(*replayPath)
			if err != nil {
				fmt.Fprintln(os.Stderr, err)
				return 1
			}
			defer in.Close()
		}
		var out *os.File
		if *trace {
			out = os.Stdout
		}
		if err := c.Replay(in, sink, mapping, out); err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
		return 0
	}
	dll := syscall.NewLazyDLL("xinput1_4.dll")
	proc := dll.NewProc("XInputGetState")
	if err := dll.Load(); err != nil {
		dll = syscall.NewLazyDLL("xinput9_1_0.dll")
		proc = dll.NewProc("XInputGetState")
		if err := dll.Load(); err != nil {
			fmt.Fprintln(os.Stderr, "jcs2-controller: no XInput DLL:", err)
			return 1
		}
	}
	enc := json.NewEncoder(os.Stdout)
	held := c.ButtonState{}
	wasConnected := false
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt)
	ticker := time.NewTicker(time.Second / time.Duration(*pollHz))
	defer ticker.Stop()
	emit := func(e c.Event) error {
		mapped, err := mapping.ApplyMapping(e)
		if err != nil {
			return err
		}
		if *trace {
			_ = enc.Encode(mapped)
		}
		return sink.Send(mapped)
	}
	lastAxes := map[string]float64{}
	for {
		var now time.Time
		select {
		case now = <-ticker.C:
		case <-stop:
			for _, e := range held.Release(time.Now().UnixMilli()) {
				if err := emit(e); err != nil {
					fmt.Fprintln(os.Stderr, "jcs2-controller: shutdown:", err)
					return 1
				}
			}
			return 0
		}
		var st xinputState
		ret, _, _ := proc.Call(0, uintptr(unsafe.Pointer(&st)))
		t := now.UnixMilli()
		if ret != 0 {
			if wasConnected {
				for _, e := range held.Release(t) {
					if err := emit(e); err != nil {
						fmt.Fprintln(os.Stderr, "jcs2-controller: release:", err)
						return 1
					}
				}
			}
			if wasConnected {
				b := false
				if err := emit(c.Event{Type: "device", Connected: &b, TMS: t}); err != nil {
					fmt.Fprintln(os.Stderr, "jcs2-controller: disconnect:", err)
					return 1
				}
			}
			wasConnected = false
			continue
		}
		if !wasConnected {
			b := true
			if err := emit(c.Event{Type: "device", Connected: &b, TMS: t}); err != nil {
				fmt.Fprintln(os.Stderr, "jcs2-controller: connect:", err)
				return 1
			}
		}
		wasConnected = true
		g := st.Gamepad
		next := map[string]bool{}
		add := func(mask uint16, key string) { next[key] = g.Buttons&mask != 0 }
		add(xA, "KEYCODE_BUTTON_A")
		add(xB, "KEYCODE_BUTTON_B")
		add(xX, "KEYCODE_BUTTON_X")
		add(xY, "KEYCODE_BUTTON_Y")
		add(xLB, "KEYCODE_BUTTON_L1")
		add(xRB, "KEYCODE_BUTTON_R1")
		add(xBack, "KEYCODE_BACK")
		add(xStart, "KEYCODE_BUTTON_START")
		add(xLS, "KEYCODE_BUTTON_THUMBL")
		add(xRS, "KEYCODE_BUTTON_THUMBR")
		add(xDPadUp, "KEYCODE_DPAD_UP")
		add(xDPadDown, "KEYCODE_DPAD_DOWN")
		add(xDPadLeft, "KEYCODE_DPAD_LEFT")
		add(xDPadRight, "KEYCODE_DPAD_RIGHT")
		for _, e := range held.Changes(next, t) {
			if err := emit(e); err != nil {
				fmt.Fprintln(os.Stderr, "jcs2-controller: button:", err)
				return 1
			}
		}
		axes := []struct {
			name string
			v    float64
		}{{"AXIS_X", float64(g.LX) / 32767}, {"AXIS_Y", -float64(g.LY) / 32767}, {"AXIS_RX", float64(g.RX) / 32767}, {"AXIS_RY", -float64(g.RY) / 32767}, {"AXIS_LTRIGGER", float64(g.LT) / 255}, {"AXIS_RTRIGGER", float64(g.RT) / 255}}
		for _, a := range axes {
			if a.name != "AXIS_LTRIGGER" && a.name != "AXIS_RTRIGGER" {
				a.v = c.Deadzone(a.v, *deadzone)
			}
			if old, ok := lastAxes[a.name]; ok && old == a.v {
				continue
			}
			lastAxes[a.name] = a.v
			if err := emit(c.Event{Type: "axis", Axis: a.name, Value: a.v, TMS: t}); err != nil {
				fmt.Fprintln(os.Stderr, "jcs2-controller: axis:", err)
				return 1
			}
		}
	}
}
