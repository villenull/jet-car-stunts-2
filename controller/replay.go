package controller

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
)

// Replay reads one Event JSON object per line. It deliberately uses Console.Send,
// so replay and the XInput path share mapping, framing, deadlines, and release behavior.
type EventSink interface{ Send(Event) error }

func Replay(r io.Reader, c EventSink, m Mapping, trace io.Writer) error {
	s := bufio.NewScanner(r)
	s.Buffer(make([]byte, 1024), 1024*1024)
	for s.Scan() {
		var e Event
		if err := json.Unmarshal(s.Bytes(), &e); err != nil {
			return err
		}
		e, err := m.ApplyMapping(e)
		if err != nil {
			return err
		}
		if trace != nil {
			_ = json.NewEncoder(trace).Encode(e)
		}
		if err := c.Send(e); err != nil {
			return fmt.Errorf("replay t=%d: %w", e.TMS, err)
		}
	}
	return s.Err()
}
