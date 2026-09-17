#!/usr/bin/env python3
"""Bridge side-channel: raw-event Unix socket pair for cross-process menu context.

Provides a pair of connected Unix domain sockets for communicating raw
joystick events from the bridge subprocess to the runner.  The runner applies
menu context gating and DeckControls BEFORE forwarding to the controller.

Architecture:
  Bridge ──(raw NDJSON)──▶ SideChannel reader ──▶ MenuContext ──▶ DeckControls ──▶ Controller
  Bridge ──(stdout)──────▶ DeckControls ──▶ log file (backward compat)

The bridge NEVER applies DeckControls when JCS2_BRIDGE_SIDECHANNEL is set;
the runner owns all gameplay remapping in live mode.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path


class BridgeSideChannel:
    """Unix socket pair for raw event communication between bridge and runner.

    Usage in runner:
        channel = BridgeSideChannel()
        bridge_fd = channel.bridge_fd  # pass to bridge as --side-channel
        # In pump loop: events = channel.read_events()

    Usage in bridge:
        fd = int(os.environ["JCS2_BRIDGE_SIDECHANNEL"])
        sock = socket.fromfd(fd, socket.AF_UNIX, socket.SOCK_STREAM)
        # In main loop: BridgeSideChannel.send_raw(sock, event_dict)
    """

    def __init__(self) -> None:
        self._server_sock, self._client_sock = socket.socketpair(
            socket.AF_UNIX, socket.SOCK_STREAM
        )
        self._server_sock.setblocking(False)
        self._client_sock.setblocking(False)
        self._closed = False

    @property
    def bridge_fd(self) -> int:
        """File descriptor to pass to the bridge subprocess."""
        return self._client_sock.fileno()

    def read_events(self) -> list[dict]:
        """Read all available raw events from the bridge (non-blocking).

        Returns a list of parsed event dicts.  Incomplete lines are buffered
        internally until the next call.  Returns empty list on no data or
        socket closure.
        """
        if self._closed:
            return []
        try:
            data = os.read(self._server_sock.fileno(), 65536)
        except (BlockingIOError, InterruptedError):
            # A nonblocking stream can be quiet between input frames.
            # This is not EOF and must not disable all subsequent controls.
            return []
        except OSError:
            # EBADF / closed fd: mark closed to prevent busy-loop.
            # Without this, the selector keeps reporting the fd as readable,
            # and each pump cycle retries the same failed read.
            self._closed = True
            return []
        if not data:
            self._closed = True
            return []
        events = []
        for line in data.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                import json
                event = json.loads(line)
                if isinstance(event, dict) and "type" in event:
                    events.append(event)
            except (ValueError, TypeError):
                pass  # drop malformed lines
        return events

    def send_raw(self, event: dict) -> None:
        """Send a raw event to the runner (called from bridge side).

        This is a thin helper for the bridge process.  In practice the
        bridge writes directly to the socket, but this provides a clean API.
        """
        import json
        line = json.dumps(event, separators=(",", ":")) + "\n"
        try:
            self._client_sock.sendall(line.encode())
        except (BrokenPipeError, OSError):
            # EBADF / closed fd: mark closed to prevent busy-loop.
            self._closed = True

    def close(self) -> None:
        """Close both ends of the socket pair."""
        self._closed = True
        for sock in (self._server_sock, self._client_sock):
            try:
                sock.close()
            except OSError:
                pass

    def close_client(self) -> None:
        """Close only the client end (after bridge process exits)."""
        try:
            self._client_sock.close()
        except OSError:
            pass

    def close_server(self) -> None:
        """Close only the server end."""
        try:
            self._server_sock.close()
        except OSError:
            pass
        self._closed = True
