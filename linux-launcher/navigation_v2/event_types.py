"""Raw physical event definitions for menu navigation.

These represent PHYSICAL Deck button presses BEFORE any gameplay remapping
(DeckControls). The navigation layer intercepts these raw events and either
consumes them (menu mode) or passes them downstream for gameplay remapping.
"""

from __future__ import annotations

from enum import Enum
from typing import NamedTuple


class PhysicalButton(Enum):
    DPAD_UP = "DPAD_UP"
    DPAD_DOWN = "DPAD_DOWN"
    DPAD_LEFT = "DPAD_LEFT"
    DPAD_RIGHT = "DPAD_RIGHT"
    A = "A"       # south face button — menu select
    B = "B"       # east face button — menu back
    START = "START"
    Y = "Y"


class ButtonAction(Enum):
    DOWN = "down"
    UP = "up"


class RawButtonEvent(NamedTuple):
    button: PhysicalButton
    action: ButtonAction
    timestamp_ms: int


class RawAxisEvent(NamedTuple):
    axis: str   # e.g. "LX", "LY"
    value: float  # -1.0 to 1.0
    timestamp_ms: int


DPAD_BUTTONS = frozenset({
    PhysicalButton.DPAD_UP, PhysicalButton.DPAD_DOWN,
    PhysicalButton.DPAD_LEFT, PhysicalButton.DPAD_RIGHT,
})

MENU_NAV_BUTTONS = DPAD_BUTTONS | {PhysicalButton.A, PhysicalButton.B}
