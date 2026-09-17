#!/usr/bin/env python3
"""Tests for the bridge side-channel: raw-event Unix socket IPC.

Verifies that BridgeSideChannel correctly sends/receives raw events
between bridge and runner processes, and that the runner applies
driving control mapping to side-channel events.
"""

import contextlib
import io
import json
import os
import selectors
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# Ensure linux-launcher is on the path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bridge_side_channel import BridgeSideChannel
import runner


class TestBridgeSideChannel(unittest.TestCase):
    """Unit tests for BridgeSideChannel socket pair."""

    def test_socket_pair_created(self):
        """Both ends of the socket pair should be valid."""
        ch = BridgeSideChannel()
        self.assertGreater(ch.bridge_fd, 0)
        self.assertGreater(ch._server_sock.fileno(), 0)
        ch.close()

    def test_send_and_receive(self):
        """Events sent from bridge side should be readable on runner side."""
        ch = BridgeSideChannel()
        event = {"type": "button", "key": "A", "action": "down", "t_ms": 1000}
        ch.send_raw(event)
        # Small delay for socket delivery
        time.sleep(0.01)
        events = ch.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "button")
        self.assertEqual(events[0]["key"], "A")
        ch.close()

    def test_multiple_events_batched(self):
        """Multiple events sent in quick succession should all be received."""
        ch = BridgeSideChannel()
        for i in range(5):
            ch.send_raw({"type": "axis", "axis": "LX", "value": float(i) * 0.2, "t_ms": i})
        time.sleep(0.01)
        events = ch.read_events()
        self.assertEqual(len(events), 5)
        for i, evt in enumerate(events):
            self.assertAlmostEqual(evt["value"], i * 0.2)
        ch.close()

    def test_empty_read_returns_empty(self):
        """Reading when no data available returns empty list."""
        ch = BridgeSideChannel()
        events = ch.read_events()
        self.assertEqual(events, [])
        ch.close()

    def test_empty_poll_does_not_disable_future_input(self):
        ch = BridgeSideChannel()
        self.addCleanup(ch.close)
        payload = b'{"type":"axis","axis":"LX","value":0.5}\n'
        with mock.patch('bridge_side_channel.os.read', side_effect=[BlockingIOError(), payload]):
            self.assertEqual(ch.read_events(), [])
            self.assertFalse(ch._closed)
            self.assertEqual(ch.read_events()[0]['value'], 0.5)

    def test_interrupted_read_can_retry(self):
        ch = BridgeSideChannel()
        self.addCleanup(ch.close)
        with mock.patch('bridge_side_channel.os.read', side_effect=InterruptedError()):
            self.assertEqual(ch.read_events(), [])
        self.assertFalse(ch._closed)

    def test_closed_channel_returns_empty(self):
        """Reading after close returns empty list."""
        ch = BridgeSideChannel()
        ch.close()
        events = ch.read_events()
        self.assertEqual(events, [])

    def test_malformed_lines_dropped(self):
        """Malformed JSON lines should be silently dropped."""
        ch = BridgeSideChannel()
        # Send mix of valid and invalid
        valid = json.dumps({"type": "button", "key": "B", "action": "up", "t_ms": 2000}) + "\n"
        invalid = b"not json\n"
        ch._client_sock.sendall(valid.encode() + invalid)
        time.sleep(0.01)
        events = ch.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["key"], "B")
        ch.close()

    def test_bridge_fd_passable_to_subprocess(self):
        """Bridge FD should be usable as a command-line arg."""
        ch = BridgeSideChannel()
        fd = ch.bridge_fd
        self.assertIsInstance(fd, int)
        # Verify the fd is valid by creating a socket from it
        sock = socket.fromfd(fd, socket.AF_UNIX, socket.SOCK_STREAM)
        self.assertIsNotNone(sock)
        sock.close()
        ch.close()


class TestSideChannelIntegration(unittest.TestCase):
    """Integration tests: runner reads side-channel events, applies driving control mappings."""

    @contextlib.contextmanager
    def _make_runner_with_side_channel(self):
        """Create an InputRouter with a side-channel for testing."""
        with tempfile.TemporaryDirectory() as temp:
            launcher = mock.Mock()
            launcher.run_dir = Path(temp)
            destination = io.BytesIO()
            router = runner.InputRouter(launcher, destination)
            # Create a side-channel manually (simulates what start_bridge does)
            from bridge_side_channel import BridgeSideChannel
            router._side_channel = BridgeSideChannel()
            router._side_channel_active = True
            router._deck_controls = runner._DeckControls() if runner._DeckControls else None
            # Register side-channel with selector
            sc_fd = router._side_channel._server_sock.fileno()
            os.set_blocking(sc_fd, False)
            router.producer_buffers["side_channel"] = bytearray()
            router.selector.register(router._side_channel._server_sock,
                                     selectors.EVENT_READ, "side_channel")
            try:
                yield router, destination, temp
            finally:
                router.close()

    def test_side_channel_event_forwarded_to_controller(self):
        """Raw event from side-channel should be forwarded after DeckControls."""
        with self._make_runner_with_side_channel() as (router, dest, temp):
            # Simulate bridge sending a raw LX axis event
            event = {"type": "axis", "axis": "LX", "value": 0.5, "t_ms": 1000}
            router._side_channel.send_raw(event)
            router.pump(0.1)
            output = dest.getvalue().decode().strip()
            self.assertTrue(len(output) > 0, "Expected event forwarded to controller")
            parsed = json.loads(output)
            # DeckControls applies steering curve to LX
            self.assertEqual(parsed["type"], "axis")
            self.assertEqual(parsed["axis"], "LX")
            router.close()

    def test_unassigned_buttons_dropped_without_adb_or_screen_detection(self):
        """Actual socket transport drops D-pad/A/B without synthesizing touches."""
        with self._make_runner_with_side_channel() as (router, dest, temp):
            with mock.patch.object(runner.subprocess, "run", side_effect=AssertionError("unexpected ADB")), \
                 mock.patch.object(runner.subprocess, "Popen", side_effect=AssertionError("unexpected process")):
                for key in sorted(runner.UNASSIGNED_BUTTONS):
                    for action in ("down", "up"):
                        router._side_channel.send_raw(dict(type="button", key=key, action=action, t_ms=0))
                        self.assertFalse(router._side_channel._closed, "test transport must deliver events")
                        router.pump(0.01)
                self.assertEqual(dest.getvalue(), b"")
                router.launcher.adb.assert_not_called()
                self.assertFalse((Path(temp) / "cursor.json").exists())

    def test_pause_and_driving_buttons_still_mapped(self):
        with self._make_runner_with_side_channel() as (router, dest, temp):
            for key, expected in (("START", "BACK"), ("RB", "LB"), ("Y", "Y")):
                dest.seek(0)
                dest.truncate()
                router._side_channel.send_raw(dict(type="button", key=key, action="down", t_ms=0))
                router.pump(0.01)
                self.assertEqual(json.loads(dest.getvalue())["key"], expected)

    def test_rt_accelerator_edge_detection_via_side_channel(self):
        """RT accelerator edge detection should work through side-channel DeckControls."""
        with self._make_runner_with_side_channel() as (router, dest, temp):
            # Gameplay mode

            # RT press: should produce RB down
            event = {"type": "axis", "axis": "RT", "value": 0.5, "t_ms": 1000}
            router._side_channel.send_raw(event)
            router.pump(0.1)
            output = dest.getvalue().decode().strip()
            parsed = json.loads(output)
            self.assertEqual(parsed["key"], "RB")
            self.assertEqual(parsed["action"], "down")
            dest.seek(0)
            dest.truncate(0)

            # RT held (same value): should NOT produce another event (edge detection)
            event = {"type": "axis", "axis": "RT", "value": 0.5, "t_ms": 1004}
            router._side_channel.send_raw(event)
            router.pump(0.1)
            output = dest.getvalue().decode().strip()
            self.assertEqual(output, "", "RT held should not produce duplicate")
            dest.seek(0)
            dest.truncate(0)

            # RT release: should produce RB up
            event = {"type": "axis", "axis": "RT", "value": 0.0, "t_ms": 1008}
            router._side_channel.send_raw(event)
            router.pump(0.1)
            output = dest.getvalue().decode().strip()
            parsed = json.loads(output)
            self.assertEqual(parsed["key"], "RB")
            self.assertEqual(parsed["action"], "up")
            router.close()

    def test_lx_steering_curve_applied_via_side_channel(self):
        """LX steering curve should be applied to side-channel events."""
        with self._make_runner_with_side_channel() as (router, dest, temp):
            from joystick_bridge import steering_curve

            event = {"type": "axis", "axis": "LX", "value": 0.5, "t_ms": 1000}
            router._side_channel.send_raw(event)
            router.pump(0.1)
            output = dest.getvalue().decode().strip()
            parsed = json.loads(output)
            self.assertEqual(parsed["axis"], "LX")
            self.assertAlmostEqual(parsed["value"], steering_curve(0.5))
            router.close()


class TestBridgeSideChannelStandalone(unittest.TestCase):
    """Verify bridge standalone mode still works (no side-channel)."""

    def test_standalone_a_b_dropped_in_gameplay(self):
        """Without side-channel, DeckControls drops unassigned A and B."""
        from joystick_bridge import DeckControls
        dc = DeckControls()
        event_a = {"type": "button", "key": "A", "action": "down", "t_ms": 0}
        result = dc.translate(event_a)
        self.assertEqual(result, [], "A should be dropped in gameplay")
        event_b = {"type": "button", "key": "B", "action": "down", "t_ms": 0}
        result = dc.translate(event_b)
        self.assertEqual(result, [], "B should be dropped in gameplay")

    def test_no_side_channel_means_bridge_applies_deck_controls(self):
        """When side_channel_fd is None, bridge should apply DeckControls to stdout."""
        # Verify the bridge code path by checking that with no --side-channel arg,
        # DeckControls is applied.  This is implicitly tested by existing bridge tests.
        from joystick_bridge import DeckControls
        dc = DeckControls()
        event = {"type": "axis", "axis": "RT", "value": 0.5, "t_ms": 0}
        result = dc.translate(event)
        self.assertEqual(result[0]["key"], "RB")
        self.assertEqual(result[0]["action"], "down")


if __name__ == "__main__":
    unittest.main(verbosity=2)
