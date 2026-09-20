#!/usr/bin/env python3
"""Hand the Deck's built-in pad to the lane, and give it back afterwards.

In Desktop Mode the Omarchy ``deck-input-mapper`` service owns the built-in
controller: it takes an exclusive ``EVIOCGRAB`` on the pad's evdev node and
re-emits the presses as desktop keyboard/mouse through uinput.  EVIOCGRAB is
device-wide -- the kernel delivers that device's events to the grabbing handle
only -- so while the mapper runs, the lane's bridge reads ``/dev/input/js0``
and receives nothing except the ``JS_EVENT_INIT`` snapshot every reader gets at
``open()``.  Physical input is then lost completely and silently; only the
lane's own diagnostic (a state snapshot in the trace, no live events) shows it.

The lane therefore claims the pad before the bridge opens the node:

    systemctl --user stop deck-input-mapper.service   # releases the EVIOCGRAB
    sudo -n /usr/local/sbin/deck-lizard-mode off      # firmware back to pad reports

Order matters: the unit's ``ExecStopPost`` re-asserts lizard mode ``on``, so
``off`` must come second.  The second command is also the only one the Deck's
sudoers grants passwordlessly (``NOPASSWD: /usr/local/sbin/deck-lizard-mode
on|off``).

Everything here is lane-scoped and reversible: the mapper is restarted only if
this lane stopped it, and lizard mode is only touched when the firmware was
actually in its mouse/keyboard mode.  ``release_pad`` undoes exactly what
``claim_pad`` did, so a lane that never changed a thing changes nothing back.

The mapper's own ownership is a cycle, not a fact: it releases the pad and
re-enables lizard mode before every rescan and takes it again when it matches a
native pad.  Stopping the service is what makes ownership stable for the lane.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

MAPPER_UNIT = "deck-input-mapper.service"
LIZARD_MODE_TOOL = "/usr/local/sbin/deck-lizard-mode"
LIZARD_MODE_NODE = Path("/sys/module/hid_steam/parameters/lizard_mode")

# The lizard_mode parameter value that means "the firmware is providing a
# keyboard/mouse and the pad reports no buttons" -- the state a lane must undo.
LIZARD_ON = "Y"


class DeckPadError(Exception):
    """The pad's current owner could not be determined, so nothing was changed."""


def _run(argv, **kwargs):
    """Run one ownership command (module-local seam for tests)."""
    return subprocess.run(argv, **kwargs)


def lizard_mode(node: Path | None = None) -> str:
    """Read the firmware's input mode: 'Y', 'N', or '' when it cannot be read."""
    try:
        return (node or LIZARD_MODE_NODE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def mapper_active(run=None) -> bool | None:
    """True/False for the desktop mapper's state, None when it cannot be read.

    Only a positive answer from systemd is trusted: an unreadable state must
    never be treated as "inactive", because then a lane would leave the pad
    hijacked while believing it had claimed it.
    """
    run = run or _run
    result = run(["systemctl", "--user", "is-active", MAPPER_UNIT],
                 capture_output=True, text=True, timeout=15)
    state = (result.stdout or "").strip()
    if state in ("active", "activating", "reloading"):
        return True
    if state in ("inactive", "failed", "deactivating"):
        return False
    return None


def _systemctl(action: str, run=None) -> None:
    (run or _run)(["systemctl", "--user", action, MAPPER_UNIT],
                  capture_output=True, text=True, timeout=60, check=True)


def _lizard(want: str, run=None) -> None:
    (run or _run)(["sudo", "-n", LIZARD_MODE_TOOL, want],
                  capture_output=True, text=True, timeout=30, check=True)


def claim_pad(run=None, node: Path | None = None) -> dict:
    """Stop the desktop mapper and put the firmware back in pad mode.

    Returns the state ``release_pad`` needs.  Raises DeckPadError when the
    mapper's own state cannot be read -- then nothing has been touched.
    """
    run = run or _run
    node = node or LIZARD_MODE_NODE
    active = mapper_active(run=run)
    if active is None:
        raise DeckPadError(f"cannot read {MAPPER_UNIT} state from systemd")
    state = {"mapper_active_before": active, "lizard_before": lizard_mode(node),
             "mapper_stopped": False, "lizard_forced": ""}
    if active:
        _systemctl("stop", run=run)
        state["mapper_stopped"] = True
    # Read the firmware again instead of assuming: stopping the unit runs its
    # ExecStopPost, which switches lizard mode back 'on'.
    current = lizard_mode(node)
    if current == LIZARD_ON:
        _lizard("off", run=run)
        state["lizard_forced"] = LIZARD_ON
    state["lizard_after"] = lizard_mode(node)
    return state


def release_pad(state: dict | None, run=None) -> dict:
    """Undo exactly what claim_pad() changed.

    A restarted mapper re-claims the pad and asserts its own lizard mode, so
    recovering from the mapper case needs no lizard write of ours; the direct
    restore is only for a machine whose firmware was in mouse mode with no
    mapper holding it.
    """
    run = run or _run
    restored = {"mapper_restarted": False, "lizard_restored": ""}
    if not state:
        return restored
    if state.get("mapper_stopped"):
        _systemctl("start", run=run)
        restored["mapper_restarted"] = True
    elif state.get("lizard_forced") == LIZARD_ON and state.get("lizard_before") == LIZARD_ON:
        _lizard("on", run=run)
        restored["lizard_restored"] = LIZARD_ON
    return restored
