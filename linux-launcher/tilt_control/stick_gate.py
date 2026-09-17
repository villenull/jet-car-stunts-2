"""Analog-only stick gate for Tilt Drive (pure helper, no I/O).

User-facing modes (persisted launcher setting, TiltSettings "mode"):
``gamepad`` = Gamepad: every physical control is forwarded; the game's own
Gamepad toggle stays ON and arbitrates guest-side.
``tilt`` = Tilt Drive: physical LX/LY stick axes never reach the guest —
steering comes from the Deck motion sensor mirrored into the guest
accelerometer. Every other physical control (triggers LT/RT, bumpers LB/RB,
START/BACK, X/Y, D-pad) always passes in BOTH modes: Tilt Drive disables
ONLY analog turning/pitch, never buttons.

The gate is FAIL-CLOSED: ``tilt_steering_active`` must already be the
persisted Tilt Drive selection, and the gate does NOT consult live IMU
ownership. A dropped/unowned sensor therefore NEVER re-enables the sticks
(no silent fallback): in Tilt Drive with a dead sensor the car simply does
not steer, and the runner surfaces a loud status warning telling the user
to select Gamepad or recover the sensor. Reconnects are inherently safe —
there is no per-device gate state; each event is classified against the
persisted mode, so a re-attached pad or restarted bridge resumes gated
without a leak window. Startup is safe the same way: the persisted mode is
loaded before the first forward, and a missing/corrupt settings file
defaults to Gamepad (today's behavior).
"""

GATED_AXES = frozenset(("LX", "LY"))


def stick_gate_allows(event: dict, tilt_steering_active: bool) -> bool:
    """True when ``event`` may be forwarded to the guest.

    ``tilt_steering_active`` must already be the persisted Tilt Drive
    selection (the runner's ``_tilt_steering_active``). With the gate
    inactive every event passes; active, only LX/LY axis events are
    dropped. Never raises on malformed input (fail-open on malformed
    input only: forward — the fail-CLOSED direction is owned by the
    runner, which never arms the gate except from persisted Tilt Drive).
    """
    try:
        if not tilt_steering_active:
            return True
        if event.get("type") == "axis" and event.get("axis") in GATED_AXES:
            return False
        return True
    except AttributeError:
        return True
