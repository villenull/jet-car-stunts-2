package controller

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"sync"
)

// liveBatch is either one ordered button/device edge or the newest pending
// values for each axis. Button edges are barriers: never coalesce across them.
type liveBatch struct{ events []Event }
type liveQueue struct {
	mu      sync.Mutex
	ready   *sync.Cond
	batches []liveBatch
	done    bool
	err     error
}

func newLiveQueue() *liveQueue {
	q := &liveQueue{}
	q.ready = sync.NewCond(&q.mu)
	return q
}
func (q *liveQueue) add(e Event) bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.done {
		return false
	}
	n := len(q.batches)
	if e.Type == "axis" && n > 0 {
		tail := &q.batches[n-1]
		if tail.events[0].Type == "axis" {
			for i := range tail.events {
				if tail.events[i].Axis == e.Axis {
					tail.events[i] = e
					return true
				}
			}
			tail.events = append(tail.events, e)
			q.ready.Signal()
			return true
		}
	}
	// A jammed button stream must fail safe, not build an unbounded stale queue.
	if n >= 64 {
		q.done = true
		q.err = fmt.Errorf("live input queue overflow")
		q.ready.Broadcast()
		return false
	}
	q.batches = append(q.batches, liveBatch{events: []Event{e}})
	q.ready.Signal()
	return true
}
func (q *liveQueue) finish(err error) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if !q.done {
		q.done = true
		q.err = err
	}
	q.ready.Broadcast()
}
func (q *liveQueue) next() (Event, bool, error) {
	q.mu.Lock()
	defer q.mu.Unlock()
	for len(q.batches) == 0 && !q.done {
		q.ready.Wait()
	}
	if q.err != nil {
		return Event{}, false, q.err
	}
	if len(q.batches) == 0 {
		return Event{}, false, nil
	}
	e := q.batches[0].events[0]
	q.batches[0].events = q.batches[0].events[1:]
	if len(q.batches[0].events) == 0 {
		q.batches = q.batches[1:]
	}
	return e, true, nil
}

// ReplayLive owns/closes r. Unlike deterministic Replay, it coalesces pending
// axis samples while the sink waits for its ACK. The newest neutral wins over
// older steering; button/device transitions retain ordering. EOF drains pending
// valid events, while malformed input/overflow aborts so caller can neutralize.
func ReplayLive(r io.ReadCloser, sink EventSink, m Mapping, trace io.Writer) error {
	q := newLiveQueue()
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		s := bufio.NewScanner(r)
		s.Buffer(make([]byte, 1024), 1024*1024)
		for s.Scan() {
			var e Event
			if err := json.Unmarshal(s.Bytes(), &e); err != nil {
				q.finish(err)
				return
			}
			mapped, err := m.ApplyMapping(e)
			if err != nil {
				q.finish(err)
				return
			}
			switch mapped.Type {
			case "axis":
				index, ok := helperAxes[mapped.Axis]
				low := -1.0
				if index >= 4 {
					low = 0
				}
				if !ok || mapped.Value < low || mapped.Value > 1 {
					q.finish(fmt.Errorf("invalid live axis %q", mapped.Axis))
					return
				}
			case "button":
				_, ok := helperKeys[mapped.Key]
				if !ok || (mapped.Action != "down" && mapped.Action != "up") {
					q.finish(fmt.Errorf("invalid live button %q", mapped.Key))
					return
				}
			case "device":
			default:
				q.finish(fmt.Errorf("invalid live event type %q", mapped.Type))
				return
			}
			if !q.add(mapped) {
				return
			}
		}
		q.finish(s.Err())
	}()
	defer func() { q.finish(nil); _ = r.Close(); <-finished }()
	for {
		e, ok, err := q.next()
		if err != nil {
			return err
		}
		if !ok {
			return nil
		}
		if trace != nil {
			_ = json.NewEncoder(trace).Encode(e)
		}
		if err := sink.Send(e); err != nil {
			return fmt.Errorf("live t=%d: %w", e.TMS, err)
		}
	}
}
