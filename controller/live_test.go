package controller

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"testing"
	"time"
)

func TestLiveQueueNeutralAndButtons(t *testing.T) {
	q := newLiveQueue()
	for i := 0; i < 1000; i++ {
		q.add(Event{Type: "axis", Axis: "AXIS_X", Value: .7})
	}
	q.add(Event{Type: "axis", Axis: "AXIS_Y", Value: .2})
	q.add(Event{Type: "axis", Axis: "AXIS_X", Value: 0})
	q.add(Event{Type: "button", Key: "KEYCODE_BUTTON_R1", Action: "down"})
	q.add(Event{Type: "axis", Axis: "AXIS_X", Value: -.5})
	q.add(Event{Type: "button", Key: "KEYCODE_BUTTON_R1", Action: "up"})
	q.finish(nil)
	var got []Event
	for {
		e, ok, err := q.next()
		if err != nil {
			t.Fatal(err)
		}
		if !ok {
			break
		}
		got = append(got, e)
	}
	if len(got) != 5 || got[0].Value != 0 || got[1].Axis != "AXIS_Y" || got[2].Action != "down" || got[3].Value != -.5 || got[4].Action != "up" {
		t.Fatalf("order/neutral: %+v", got)
	}
}
func TestLiveQueueOverflow(t *testing.T) {
	q := newLiveQueue()
	for i := 0; i < 65; i++ {
		q.add(Event{Type: "button", Key: "KEYCODE_BUTTON_A", Action: "down"})
	}
	if _, _, err := q.next(); err == nil {
		t.Fatal("expected bounded-queue failure")
	}
}

type eofReader struct {
	io.Reader
	ended  chan struct{}
	closed bool
}

func (r *eofReader) Read(b []byte) (int, error) {
	n, e := r.Reader.Read(b)
	if e == io.EOF && !r.closed {
		r.closed = true
		close(r.ended)
	}
	return n, e
}
func (r *eofReader) Close() error { return nil }

type gatedSink struct {
	ended  chan struct{}
	events []Event
}

func (s *gatedSink) Send(e Event) error { <-s.ended; s.events = append(s.events, e); return nil }
func TestLiveSlowSinkDropsOldAxisSamples(t *testing.T) {
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	for i := 0; i < 1000; i++ {
		enc.Encode(Event{Type: "axis", Axis: "AXIS_X", Value: .5})
	}
	enc.Encode(Event{Type: "axis", Axis: "AXIS_X", Value: 0})
	r := &eofReader{Reader: &b, ended: make(chan struct{})}
	s := &gatedSink{ended: r.ended}
	if err := ReplayLive(r, s, Mapping{}, nil); err != nil {
		t.Fatal(err)
	}
	if len(s.events) > 2 || len(s.events) == 0 || s.events[len(s.events)-1].Value != 0 {
		t.Fatalf("stale queue: %+v", s.events)
	}
}

type failingSink struct{}

func (failingSink) Send(Event) error { return errors.New("sink stopped") }
func TestLiveSinkFailureClosesBlockedReader(t *testing.T) {
	r, w := io.Pipe()
	defer w.Close()
	done := make(chan error, 1)
	go func() { done <- ReplayLive(r, failingSink{}, Mapping{}, nil) }()
	if _, err := w.Write([]byte("{\"type\":\"axis\",\"axis\":\"AXIS_X\",\"value\":0.4}\n")); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("expected sink error")
		}
	case <-time.After(time.Second):
		t.Fatal("blocked reader leaked")
	}
}
func TestLiveRejectsInvalidInput(t *testing.T) {
	for _, text := range []string{"garbage\n", "{\"type\":\"axis\",\"axis\":\"unknown\"}\n", "{\"type\":\"axis\",\"axis\":\"AXIS_X\",\"value\":2}\n", "{\"type\":\"button\",\"key\":\"KEYCODE_BUTTON_A\",\"action\":\"bad\"}\n"} {
		if err := ReplayLive(io.NopCloser(bytes.NewBufferString(text)), failingSink{}, Mapping{}, nil); err == nil {
			t.Fatalf("accepted %s", text)
		}
	}
}
