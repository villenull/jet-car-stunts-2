package controller

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"strconv"
	"strings"
	"time"
)

// Console is the small interface used by the event adapter. Tests can provide
// a net.Pipe or bytes-backed mock; production uses one TCP connection to the
// emulator console for the entire controller session.
type Console struct {
	rw      io.ReadWriter
	rd      *bufio.Reader
	timeout time.Duration
}

func NewConsole(rw io.ReadWriter) *Console {
	return &Console{rw: rw, rd: bufio.NewReader(rw), timeout: 2 * time.Second}
}

func (c *Console) deadline() error {
	if d, ok := c.rw.(interface{ SetDeadline(time.Time) error }); ok {
		return d.SetDeadline(time.Now().Add(c.timeout))
	}
	return nil
}

func (c *Console) readLine() (string, error) {
	if err := c.deadline(); err != nil {
		return "", err
	}
	line, err := c.rd.ReadString('\n')
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(line), nil
}

func (c *Console) command(s string) error {
	if err := c.deadline(); err != nil {
		return err
	}
	if _, err := io.WriteString(c.rw, s+"\n"); err != nil {
		return err
	}
	for i := 0; i < 8; i++ {
		line, err := c.readLine()
		if err != nil {
			return err
		}
		if line == "OK" {
			return nil
		}
		if strings.HasPrefix(line, "KO") {
			return fmt.Errorf("emulator console: %s", line)
		}
		// Authenticated consoles can emit a help/status banner before the
		// command response; it is informational, not a command failure.
	}
	return errors.New("emulator console: response not received")
}

func (c *Console) Authenticate(token string) error {
	if strings.ContainsAny(token, "\r\n") {
		return errors.New("console token contains newline")
	}
	// The console sends a banner immediately after connect. It is not a command
	// response and must be consumed before auth is written.
	if _, err := c.readLine(); err != nil {
		return fmt.Errorf("read console greeting: %w", err)
	}
	if err := c.deadline(); err != nil {
		return err
	}
	if _, err := io.WriteString(c.rw, "auth "+token+"\n"); err != nil {
		return err
	}
	// The emulator may send one or more instructional lines after auth before
	// its terminal OK response.  Ignore those banner lines, but preserve KO
	// as a hard authentication failure and bound the read so a broken console
	// cannot hang the controller.
	for i := 0; i < 8; i++ {
		line, err := c.readLine()
		if err != nil {
			return err
		}
		if line == "OK" {
			return nil
		}
		if strings.HasPrefix(line, "KO") {
			return fmt.Errorf("emulator console: %s", line)
		}
	}
	return errors.New("emulator console: authentication response not received")
}

func ConnectConsole(addr, tokenPath string) (*Console, net.Conn, error) {
	b, err := os.ReadFile(tokenPath)
	if err != nil {
		return nil, nil, err
	}
	token := strings.TrimSpace(string(b))
	if token == "" {
		return nil, nil, errors.New("empty console token")
	}
	conn, err := (&net.Dialer{Timeout: 2 * time.Second}).Dial("tcp", addr)
	if err != nil {
		return nil, nil, err
	}
	c := NewConsole(conn)
	if err := c.Authenticate(token); err != nil {
		_ = conn.Close()
		return nil, nil, err
	}
	return c, conn, nil
}

var keyCodes = map[string]string{
	"KEYCODE_BUTTON_A": "BTN_A", "KEYCODE_BUTTON_B": "BTN_B", "KEYCODE_BUTTON_X": "BTN_X", "KEYCODE_BUTTON_Y": "BTN_Y",
	"KEYCODE_BUTTON_L1": "BTN_TL", "KEYCODE_BUTTON_R1": "BTN_TR", "KEYCODE_BUTTON_START": "BTN_START", "KEYCODE_BACK": "BTN_SELECT",
	"KEYCODE_BUTTON_THUMBL": "BTN_THUMBL", "KEYCODE_BUTTON_THUMBR": "BTN_THUMBR",
	"KEYCODE_DPAD_UP": "BTN_DPAD_UP", "KEYCODE_DPAD_DOWN": "BTN_DPAD_DOWN", "KEYCODE_DPAD_LEFT": "BTN_DPAD_LEFT", "KEYCODE_DPAD_RIGHT": "BTN_DPAD_RIGHT",
}
var axisCodes = map[string]string{"AXIS_X": "ABS_X", "AXIS_Y": "ABS_Y", "AXIS_RX": "ABS_RX", "AXIS_RY": "ABS_RY", "AXIS_LTRIGGER": "ABS_Z", "AXIS_RTRIGGER": "ABS_RZ"}

func consoleValue(axis string, v float64) (string, error) {
	if strings.Contains(axis, "TRIGGER") {
		if v < 0 {
			v = 0
		}
		if v > 1 {
			v = 1
		}
		return strconv.Itoa(int(v * 255)), nil
	}
	if v < -1 {
		v = -1
	}
	if v > 1 {
		v = 1
	}
	return strconv.Itoa(int(v * 32767)), nil
}

// Send converts the bridge protocol to documented emulator-console EV events.
// One authenticated connection is reused; no adb shell command is started.
func (c *Console) Send(e Event) error {
	if err := c.sendEvent(e); err != nil {
		return err
	}
	// This emulator build exposes no symbolic EV_SYN code aliases; SYN_REPORT
	// is Linux code 0 and must be sent numerically.
	return c.command("event send EV_SYN:0:0")
}

func (c *Console) sendEvent(e Event) error {
	switch e.Type {
	case "button":
		code, ok := keyCodes[e.Key]
		if !ok {
			return fmt.Errorf("unsupported key %q", e.Key)
		}
		v := "0"
		if e.Action == "down" {
			v = "1"
		}
		return c.command("event send EV_KEY:" + code + ":" + v)
	case "axis":
		code, ok := axisCodes[e.Axis]
		if !ok {
			return fmt.Errorf("unsupported axis %q", e.Axis)
		}
		v, err := consoleValue(e.Axis, e.Value)
		if err != nil {
			return err
		}
		return c.command("event send EV_ABS:" + code + ":" + v)
	case "device":
		return nil
	default:
		return fmt.Errorf("unsupported event type %q", e.Type)
	}
}
