package controller

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"io"
	"math"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"
)

const helperRemoteJar = "/data/local/tmp/jcs2-input-helper.jar"

type Helper struct {
	cmd      *exec.Cmd
	in       io.WriteCloser
	lines    <-chan string
	exit     <-chan error
	held     map[string]bool
	ax       [6]float64
	deviceID int
	mu       sync.Mutex
	closed   bool
}

func StartHelper(adbPath, adbPort, serial, hostJar string) (*Helper, error) {
	if adbPath == "" {
		adbPath = "adb"
	}
	if adbPort == "" || serial == "" || hostJar == "" {
		return nil, fmt.Errorf("helper requires adb port, serial, and host jar")
	}
	if st, err := os.Stat(hostJar); err != nil || st.IsDir() {
		return nil, fmt.Errorf("helper host jar: %v", err)
	}
	if out, err := runADB(adbPath, adbPort, serial, 10*time.Second, "push", hostJar, helperRemoteJar); err != nil {
		return nil, fmt.Errorf("adb push helper: %w (%s)", err, out)
	}
	if out, err := runADB(adbPath, adbPort, serial, 5*time.Second, "shell", "id"); err != nil {
		return nil, fmt.Errorf("adb shell id: %w (%s)", err, out)
	} else if !strings.Contains(out, "uid=2000") {
		return nil, fmt.Errorf("helper shell uid is not 2000: %q", compact(out))
	}
	cmd := exec.Command(adbPath, "-P", adbPort, "-s", serial, "shell", "CLASSPATH="+helperRemoteJar, "app_process", "/system/bin", "Jcs2InputHelper")
	in, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	outPipe, err := cmd.StdoutPipe()
	if err != nil {
		_ = in.Close()
		return nil, err
	}
	errPipe, err := cmd.StderrPipe()
	if err != nil {
		_ = in.Close()
		return nil, err
	}
	if err = cmd.Start(); err != nil {
		_ = in.Close()
		return nil, err
	}
	lines := make(chan string, 32)
	exits := make(chan error, 1)
	go func() {
		s := bufio.NewScanner(outPipe)
		s.Buffer(make([]byte, 256), 4096)
		for s.Scan() {
			select {
			case lines <- s.Text():
			case <-time.After(2 * time.Second):
			}
		}
		close(lines)
	}()
	go func() {
		var b bytes.Buffer
		_, _ = io.CopyN(&b, errPipe, 8192)
		if b.Len() > 0 {
			select {
			case lines <- "STDERR " + compact(b.String()):
			default:
			}
		}
	}()
	go func() { exits <- cmd.Wait(); close(exits) }()
	h := &Helper{cmd: cmd, in: in, lines: lines, exit: exits, held: map[string]bool{}}
	line, err := h.waitLine(5 * time.Second)
	if err != nil {
		_ = h.abort()
		return nil, fmt.Errorf("helper readiness: %w", err)
	}
	p := strings.Fields(line)
	if len(p) != 3 || p[0] != "READY" || p[1] != "JCS2_UHID" {
		_ = h.abort()
		return nil, fmt.Errorf("helper readiness malformed: %q", line)
	}
	id, err := strconv.Atoi(p[2])
	if err != nil || id < 0 {
		_ = h.abort()
		return nil, fmt.Errorf("helper device id malformed: %q", line)
	}
	h.deviceID = id
	return h, nil
}
func runADB(adb, port, serial string, timeout time.Duration, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	all := append([]string{"-P", port, "-s", serial}, args...)
	c := exec.CommandContext(ctx, adb, all...)
	var b bytes.Buffer
	c.Stdout = &b
	c.Stderr = &b
	err := c.Run()
	if ctx.Err() != nil {
		return compact(b.String()), fmt.Errorf("timeout")
	}
	return compact(b.String()), err
}
func compact(s string) string {
	s = strings.TrimSpace(strings.ReplaceAll(s, "\n", " "))
	if len(s) > 1024 {
		s = s[:1024]
	}
	return s
}
func (h *Helper) waitLine(timeout time.Duration) (string, error) {
	t := time.NewTimer(timeout)
	defer t.Stop()
	select {
	case l, ok := <-h.lines:
		if !ok {
			return "", fmt.Errorf("helper exited before READY")
		}
		if strings.HasPrefix(l, "STDERR ") {
			return "", fmt.Errorf("%s", l)
		}
		return l, nil
	case err := <-h.exit:
		return "", fmt.Errorf("helper exited: %v", err)
	case <-t.C:
		return "", fmt.Errorf("timeout")
	}
}
func (h *Helper) ack(timeout time.Duration) error {
	l, err := h.waitLine(timeout)
	if err != nil {
		return err
	}
	if l != "ACK" {
		return fmt.Errorf("helper protocol: %q", l)
	}
	return nil
}
func (h *Helper) write(s string) error {
	if _, err := io.WriteString(h.in, s+"\n"); err != nil {
		return err
	}
	return h.ack(2 * time.Second)
}
func (h *Helper) Send(e Event) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.closed {
		return fmt.Errorf("helper closed")
	}
	switch e.Type {
	case "button":
		code, ok := helperKeys[e.Key]
		if !ok {
			return fmt.Errorf("helper unsupported key %q", e.Key)
		}
		if e.Action != "down" && e.Action != "up" {
			return fmt.Errorf("helper invalid action %q", e.Action)
		}
		if e.Action == "down" {
			h.held[e.Key] = true
		} else {
			delete(h.held, e.Key)
		}
		return h.write(fmt.Sprintf("key %d %s", code, e.Action))
	case "axis":
		idx, ok := helperAxes[e.Axis]
		if !ok {
			return fmt.Errorf("helper unsupported axis %q", e.Axis)
		}
		if math.IsNaN(e.Value) || math.IsInf(e.Value, 0) {
			return fmt.Errorf("helper non-finite axis %q", e.Axis)
		}
		if idx < 4 && (e.Value < -1 || e.Value > 1) {
			return fmt.Errorf("helper axis %q out of range", e.Axis)
		}
		if idx >= 4 && (e.Value < 0 || e.Value > 1) {
			return fmt.Errorf("helper trigger %q out of range", e.Axis)
		}
		h.ax[idx] = e.Value
		return h.writeAxis()
	case "device":
		if e.Connected != nil && !(*e.Connected) {
			return h.Release()
		}
		return h.writeAxis()
	default:
		return fmt.Errorf("helper unsupported event %q", e.Type)
	}
}
func (h *Helper) writeAxis() error {
	return h.write(fmt.Sprintf("axis %s %s %s %s %s %s", f(h.ax[0]), f(h.ax[1]), f(h.ax[2]), f(h.ax[3]), f(h.ax[4]), f(h.ax[5])))
}
func f(v float64) string { return strconv.FormatFloat(v, 'f', 5, 64) }
func (h *Helper) Release() error {
	for k := range h.held {
		if code, ok := helperKeys[k]; ok {
			if err := h.write(fmt.Sprintf("key %d up", code)); err != nil {
				return err
			}
		}
	}
	h.held = map[string]bool{}
	h.ax = [6]float64{}
	return h.write("neutral")
}
func (h *Helper) abort() error {
	h.mu.Lock()
	if h.closed {
		h.mu.Unlock()
		return nil
	}
	h.closed = true
	h.mu.Unlock()
	_ = h.in.Close()
	select {
	case <-h.exit:
	case <-time.After(2 * time.Second):
		_ = h.cmd.Process.Kill()
		<-h.exit
	}
	return nil
}
func (h *Helper) Close() error {
	if h == nil {
		return nil
	}
	h.mu.Lock()
	if h.closed {
		h.mu.Unlock()
		return nil
	}
	h.mu.Unlock()
	_ = h.Release()
	h.mu.Lock()
	h.closed = true
	h.mu.Unlock()
	_, _ = io.WriteString(h.in, "quit\n")
	_ = h.in.Close()
	select {
	case err := <-h.exit:
		return err
	case <-time.After(2 * time.Second):
		_ = h.cmd.Process.Kill()
		return <-h.exit
	}
}

var helperKeys = map[string]int{"KEYCODE_BUTTON_A": 96, "KEYCODE_BUTTON_B": 97, "KEYCODE_BUTTON_X": 99, "KEYCODE_BUTTON_Y": 100, "KEYCODE_BUTTON_L1": 102, "KEYCODE_BUTTON_R1": 103, "KEYCODE_BUTTON_START": 108, "KEYCODE_BACK": 4, "KEYCODE_BUTTON_THUMBL": 106, "KEYCODE_BUTTON_THUMBR": 107, "KEYCODE_DPAD_UP": 19, "KEYCODE_DPAD_DOWN": 20, "KEYCODE_DPAD_LEFT": 21, "KEYCODE_DPAD_RIGHT": 22}
var helperAxes = map[string]int{"AXIS_X": 0, "AXIS_Y": 1, "AXIS_RX": 2, "AXIS_RY": 3, "AXIS_LTRIGGER": 4, "AXIS_RTRIGGER": 5}
