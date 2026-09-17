#!/usr/bin/env python3
"""Tests for EBADF (Bad file descriptor) hardening.

Verifies that the side-channel reader and InputRouter handle closed file
descriptors without busy-looping or crashing.  The selector keeps reporting
a closed fd as readable; without these guards each pump cycle retries the
same failed read indefinitely.
"""

import json
import os
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bridge_side_channel import BridgeSideChannel


class TestBridgeSideChannelEBADF(unittest.TestCase):
    """BridgeSideChannel.read_events() must handle closed/bad fds gracefully."""

    def test_read_events_on_closed_socket_sets_closed(self):
        """read_events() on a closed server socket must set _closed = True."""
        ch = BridgeSideChannel()
        # Close the server socket to simulate bridge exit
        ch._server_sock.close()
        # read_events should not raise and must mark the channel as closed
        result = ch.read_events()
        self.assertEqual(result, [])
        self.assertTrue(ch._closed)

    def test_read_events_returns_empty_when_already_closed(self):
        """read_events() returns [] immediately when _closed is already True."""
        ch = BridgeSideChannel()
        ch._closed = True
        result = ch.read_events()
        self.assertEqual(result, [])

    def test_read_events_normal_operation(self):
        """read_events() parses valid JSON lines from the socket."""
        ch = BridgeSideChannel()
        event = {"type": "button", "key": "A", "action": "down", "t_ms": 0}
        line = json.dumps(event, separators=(",", ":")) + "\n"
        ch._client_sock.sendall(line.encode())
        events = ch.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "button")
        self.assertEqual(events[0]["key"], "A")
        ch.close()

    def test_send_raw_on_closed_socket_sets_closed(self):
        """send_raw() on a closed client socket must set _closed = True."""
        ch = BridgeSideChannel()
        ch._client_sock.close()
        # send_raw should not raise
        ch.send_raw({"type": "button", "key": "A", "action": "down", "t_ms": 0})
        self.assertTrue(ch._closed)

    def test_close_idempotent(self):
        """close() can be called multiple times without error."""
        ch = BridgeSideChannel()
        ch.close()
        ch.close()
        self.assertTrue(ch._closed)

    def test_read_events_after_client_close_returns_empty(self):
        """When the client closes its end, server read returns empty → closed."""
        ch = BridgeSideChannel()
        # Close the client (simulates bridge process exit)
        ch._client_sock.close()
        # Server read should return empty bytes → _closed = True
        result = ch.read_events()
        self.assertEqual(result, [])
        self.assertTrue(ch._closed)

    def test_read_events_malformed_lines_dropped(self):
        """Malformed lines are silently dropped; valid lines still parsed."""
        ch = BridgeSideChannel()
        valid = json.dumps({"type": "axis", "axis": "LX", "value": 0.5, "t_ms": 0}, separators=(",", ":"))
        payload = b"not-json\n" + valid.encode() + b"\n"
        ch._client_sock.sendall(payload)
        events = ch.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "axis")
        ch.close()

    def test_double_close_fd_no_crash(self):
        """Closing the fd externally then calling close() must not crash."""
        ch = BridgeSideChannel()
        fd = ch._server_sock.fileno()
        # Close the underlying fd directly (simulates external close)
        os.close(fd)
        # The socket object still exists but its fd is gone
        # close() must handle this gracefully
        ch.close()
        self.assertTrue(ch._closed)


class TestInputRouterProducerEBADF(unittest.TestCase):
    """InputRouter._read_producer() must handle closed producer fds."""

    def _make_router(self):
        """Create a minimal InputRouter for testing."""
        from runner import InputRouter
        import tempfile
        td = tempfile.mkdtemp(prefix="test-ebadf-")
        mock_launcher = MagicMock()
        mock_launcher.run_dir = Path(td)
        mock_launcher.log = MagicMock()
        router = InputRouter(mock_launcher, destination=MagicMock())
        return router

    def test_read_producer_on_closed_pipe_returns_false(self):
        """_read_producer() on a closed pipe returns False (drop producer)."""
        router = self._make_router()
        # Create a pipe and close the read end
        r, w = os.pipe()
        os.close(w)
        os.close(r)
        # Create a mock producer with the closed fd
        producer = MagicMock()
        producer.fileno.return_value = r
        # _read_producer should return False (not raise)
        result = router._read_producer(producer)
        self.assertFalse(result)

    def test_read_producer_normal_pipe(self):
        """_read_producer() reads data from a valid pipe and forwards lines."""
        router = self._make_router()
        r, w = os.pipe()
        os.write(w, b'{"type":"axis","axis":"LX","value":0.5,"t_ms":0}\n')
        os.close(w)
        os.set_blocking(r, False)
        producer = MagicMock()
        producer.fileno.return_value = r
        result = router._read_producer(producer)
        self.assertTrue(result)
        # Verify destination.write was called
        router.destination.write.assert_called()
        os.close(r)

    def test_read_producer_empty_pipe_returns_false(self):
        """_read_producer() returns False when pipe EOF (no data)."""
        router = self._make_router()
        r, w = os.pipe()
        os.close(w)  # Close write end → EOF
        os.set_blocking(r, False)
        producer = MagicMock()
        producer.fileno.return_value = r
        result = router._read_producer(producer)
        self.assertFalse(result)
        os.close(r)


class TestInputRouterSideChannelEBADF(unittest.TestCase):
    """InputRouter pump loop handles side-channel EBADF without busy-loop."""

    def test_read_side_channel_on_closed_channel(self):
        """_read_side_channel() returns False when channel is dead."""
        from runner import InputRouter
        mock_launcher = MagicMock()
        mock_launcher.run_dir = Path("/tmp/test-ebadf-run2")
        mock_launcher.log = MagicMock()
        router = InputRouter(mock_launcher, destination=MagicMock())
        # Create a side-channel and close it
        ch = BridgeSideChannel()
        router._side_channel = ch
        ch._server_sock.close()
        # read_side_channel should return False (not crash)
        result = router._read_side_channel()
        self.assertFalse(result)


class TestStartBridgePassFds(unittest.TestCase):
    """start_bridge must mark the side-channel fd inheritable and pass_fds."""

    def test_start_bridge_passes_side_channel_fd(self):
        """Popen must receive pass_fds with the side-channel fd."""
        from runner import InputRouter
        import tempfile
        from unittest.mock import patch

        td = tempfile.mkdtemp(prefix="test-passfds-")
        mock_launcher = MagicMock()
        mock_launcher.run_dir = Path(td)
        mock_launcher.log = MagicMock()
        mock_launcher.environment.return_value = {}
        router = InputRouter(mock_launcher, destination=MagicMock())

        # Use a real channel to get a real fd for the mock to return
        real_ch = BridgeSideChannel()
        controlled_fd = real_ch.bridge_fd
        self.assertGreater(controlled_fd, 2, "fd must be > 2")

        # Build a mock channel that returns our controlled fd
        mock_ch = MagicMock()
        mock_ch.bridge_fd = controlled_fd
        # start_bridge accesses _server_sock.fileno() for selector registration
        mock_server_sock = MagicMock()
        mock_server_sock.fileno.return_value = real_ch._server_sock.fileno()
        mock_ch._server_sock = mock_server_sock

        # Mock Popen to capture pass_fds
        mock_proc = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.fileno.return_value = 50
        mock_proc.pid = 999
        mock_proc.pgid = 999

        with patch("runner.BridgeSideChannel", return_value=mock_ch):
            with patch("runner.subprocess.Popen", return_value=mock_proc) as mock_popen:
                with patch("runner.OwnedProcess") as MockOwned:
                    mock_owned = MagicMock()
                    mock_owned.process = mock_proc
                    mock_owned.pgid = 999
                    mock_owned.name = "joystick-bridge"
                    mock_owned.log_handles = ()
                    MockOwned.return_value = mock_owned

                    try:
                        router.start_bridge(Path("/nonexistent/bridge.py"))
                    except Exception:
                        pass  # ok — we only care about Popen args

                    # Verify Popen was called with pass_fds containing the fd
                    mock_popen.assert_called_once()
                    call_kwargs = mock_popen.call_args
                    self.assertIn("pass_fds", call_kwargs.kwargs,
                                  "Popen must use pass_fds keyword")
                    self.assertIn(controlled_fd, call_kwargs.kwargs["pass_fds"],
                                  f"pass_fds must contain the bridge fd {controlled_fd}")

                    # Verify fd is inheritable (set by start_bridge before Popen)
                    self.assertTrue(os.get_inheritable(controlled_fd),
                                    "bridge_fd must be marked inheritable")

        real_ch.close()

    def test_start_bridge_no_side_channel_no_pass_fds(self):
        """When BridgeSideChannel is unavailable, no pass_fds is used."""
        from runner import InputRouter
        import tempfile
        from unittest.mock import patch

        td = tempfile.mkdtemp(prefix="test-passfds-nosc-")
        mock_launcher = MagicMock()
        mock_launcher.run_dir = Path(td)
        mock_launcher.log = MagicMock()
        mock_launcher.environment.return_value = {}
        router = InputRouter(mock_launcher, destination=MagicMock())

        mock_proc = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.fileno.return_value = 51
        mock_proc.pid = 1000

        with patch("runner.BridgeSideChannel", None):
            with patch("runner.subprocess.Popen", return_value=mock_proc) as mock_popen:
                with patch("runner.OwnedProcess") as MockOwned:
                    mock_owned = MagicMock()
                    mock_owned.process = mock_proc
                    mock_owned.pgid = 1000
                    mock_owned.name = "joystick-bridge"
                    mock_owned.log_handles = ()
                    MockOwned.return_value = mock_owned

                    try:
                        router.start_bridge(Path("/nonexistent/bridge.py"))
                    except Exception:
                        pass

                    mock_popen.assert_called_once()
                    call_kwargs = mock_popen.call_args
                    self.assertIn("pass_fds", call_kwargs.kwargs)
                    self.assertEqual(call_kwargs.kwargs["pass_fds"], ())


class TestBridgeSideChannelInheritable(unittest.TestCase):
    """Bridge fd must be inheritable for subprocess inheritance."""

    def test_bridge_fd_inheritable(self):
        """After BridgeSideChannel creation, bridge_fd must be inheritable."""
        ch = BridgeSideChannel()
        fd = ch.bridge_fd
        self.assertGreater(fd, 2)
        # Default socket fd is non-inheritable on Python 3.4+
        # The runner must call os.set_inheritable(fd, True) before Popen
        # We test the pre-condition here: fd starts non-inheritable
        self.assertFalse(os.get_inheritable(fd),
                         "bridge_fd should start non-inheritable")
        ch.close()

    def test_set_inheritable_makes_fd_passable(self):
        """os.set_inheritable(fd, True) allows subprocess to inherit the fd."""
        ch = BridgeSideChannel()
        fd = ch.bridge_fd
        os.set_inheritable(fd, True)
        self.assertTrue(os.get_inheritable(fd),
                        "fd must be inheritable after set_inheritable")
        ch.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
