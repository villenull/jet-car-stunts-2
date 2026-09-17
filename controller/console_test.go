package controller

import (
	"bufio"
	"io"
	"net"
	"strings"
	"testing"
)

func mockConsole(t *testing.T, fn func(string) string) *Console {
	t.Helper()
	client, server := net.Pipe()
	go func() {
		defer server.Close()
		r := bufio.NewReader(server)
		io.WriteString(server, "Android Console: Authentication required\n")
		for {
			line, err := r.ReadString('\n')
			if err != nil {
				return
			}
			io.WriteString(server, fn(strings.TrimSpace(line))+"\n")
		}
	}()
	t.Cleanup(func() { client.Close() })
	return NewConsole(client)
}

func TestConsoleAuthAndEventFraming(t *testing.T) {
	seen := []string{}
	c := mockConsole(t, func(s string) string { seen = append(seen, s); return "OK" })
	if err := c.Authenticate("abc123"); err != nil {
		t.Fatal(err)
	}
	if err := c.Send(Event{Type: "button", Key: "KEYCODE_BUTTON_A", Action: "down"}); err != nil {
		t.Fatal(err)
	}
	if err := c.Send(Event{Type: "axis", Axis: "AXIS_X", Value: -1}); err != nil {
		t.Fatal(err)
	}
	if strings.Join(seen, "|") != "auth abc123|event send EV_KEY:BTN_A:1|event send EV_SYN:0:0|event send EV_ABS:ABS_X:-32767|event send EV_SYN:0:0" {
		t.Fatalf("framing: %v", seen)
	}
}

func TestConsoleRejectsBadTokenAndBoundsAxes(t *testing.T) {
	c := mockConsole(t, func(string) string { return "OK" })
	if err := c.Authenticate("bad\ntoken"); err == nil {
		t.Fatal("newline token accepted")
	}
	if got, _ := consoleValue("AXIS_LTRIGGER", 2); got != "255" {
		t.Fatalf("trigger bound %s", got)
	}
	if got, _ := consoleValue("AXIS_X", -2); got != "-32767" {
		t.Fatalf("axis bound %s", got)
	}
}
