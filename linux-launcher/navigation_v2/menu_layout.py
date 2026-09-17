"""Menu layout definitions with resolution-adaptive bounding boxes.

Button positions are defined as fractional coordinates (0..1) relative to
the actual display resolution. Bounding boxes are detected from screenshot
pixel analysis, NOT hardcoded pixel coordinates.

The game renders at whatever the Android framebuffer resolution is (currently
1280x800). All coordinates here are fractional so they adapt to any resolution.

Directional tap navigation
--------------------------
JCS2 menus are touch UI: they respond to taps, not gamepad focus.  Each
supported menu screen carries an explicit NEIGHBOR TABLE (`MenuDef.neighbors`)
mapping, for every entry, the immediately-adjacent entry per D-pad direction:

  * main_menu    — vertical column, wraps top↔bottom
  * pause        — vertical column, wraps top↔bottom
  * level_select — horizontal tab row (EASY/MEDIUM/HARD/ALL), no wrap
  * results      — horizontal bottom row (RETRY/VIEW/REPLAY/CONTINUE), no wrap

Movement semantics: a D-pad press taps the adjacent entry (the game flashes
the tapped button = select flash) and that entry becomes the cursor.  At an
edge with no adjacent entry the cursor stays and nothing is tapped.  A-press
re-taps the cursor to confirm/activate; B-press issues KEYCODE_BACK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from .adb_adapter import Color, ScreenCapture
from .event_types import PhysicalButton


@dataclass(frozen=True)
class ButtonRegion:
    """A menu button with fractional position and bounding box."""
    label: str
    # center fractional coordinates
    cx: float
    cy: float
    # bounding box fractional half-widths
    hw: float  # half-width
    hh: float  # half-height
    verified: bool = False

    def pixel_center(self, w: int, h: int) -> tuple[int, int]:
        return int(self.cx * w), int(self.cy * h)

    def pixel_bbox(self, w: int, h: int) -> tuple[int, int, int, int]:
        """Returns (x1, y1, x2, y2) in pixel coordinates."""
        cx, cy = self.cx * w, self.cy * h
        return (
            int(cx - self.hw * w), int(cy - self.hh * h),
            int(cx + self.hw * w), int(cy + self.hh * h),
        )


class NavDirection(Enum):
    VERTICAL = "vertical"
    HORIZONTAL = "horizontal"


class MenuState(Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class MenuDef:
    """Definition for one menu screen.

    `neighbors` is the directional neighbor table: for each D-pad direction
    it maps every cursor index to the immediately-adjacent index in that
    direction.  A direction key that is absent, or an index missing from the
    table, means "no adjacent entry" (edge lock — cursor stays).
    """
    name: str
    buttons: tuple[ButtonRegion, ...]
    default_index: int = 0
    direction: NavDirection = NavDirection.VERTICAL
    wraps: bool = True
    state: MenuState = MenuState.SUPPORTED
    neighbors: Mapping[PhysicalButton, Mapping[int, int]] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.buttons)

    def neighbor(self, direction: PhysicalButton, index: int) -> int:
        """Return the immediately-adjacent index for `direction`.

        Falls back to `index` (edge lock, no movement) when the direction
        is not navigable from that entry.
        """
        table = self.neighbors.get(direction)
        if table is None:
            return index
        return table.get(index, index)


def _vertical_wrap_up(count: int) -> dict[int, int]:
    """Vertical wrap neighbor table for DPAD_UP (index i → i-1 mod count)."""
    return {i: (i - 1) % count for i in range(count)}


def _vertical_wrap_down(count: int) -> dict[int, int]:
    """Vertical wrap neighbor table for DPAD_DOWN (index i → i+1 mod count)."""
    return {i: (i + 1) % count for i in range(count)}


# Fractional coordinates re-measured from analysis/linux-launcher/menu-fixtures/
# main-menu-1280x800.png + main-menu-after-back-1280x800.png (PIL pixel
# analysis, 2026-09-11).  Buttons are red trapezoids; each center below is a
# SOLID-INTERIOR point of the trapezoid (widest contiguous red runs at >=85%
# of the band max => away from text and the slanted edges).  Every center is
# solid red in BOTH main-menu fixtures.  Method + numbers:
#   analysis/linux-launcher/tap-hardening/MEASUREMENT.md and measure.py.
# Note: the previous centers (~0.430,0.280...) were ~30-50px too high and
# ~50px right on 1280x800, which is why taps landed on bar edges/gaps.

MAIN_MENU = MenuDef(
    name="main_menu",
    buttons=(
        ButtonRegion("PLAY",             0.4031, 0.2663, 0.070, 0.050, verified=True),
        ButtonRegion("CREATE",           0.4039, 0.4500, 0.088, 0.050),
        ButtonRegion("USER CHALLENGES",  0.4367, 0.5550, 0.113, 0.050),
        ButtonRegion("HELP AND OPTIONS", 0.4250, 0.6575, 0.108, 0.048),
        ButtonRegion("USER LEVELS",      0.4109, 0.7625, 0.091, 0.048),
        ButtonRegion("STORE",            0.3883, 0.8650, 0.075, 0.048),
    ),
    default_index=0,
    direction=NavDirection.VERTICAL,
    wraps=True,
    neighbors={
        PhysicalButton.DPAD_UP: _vertical_wrap_up(6),
        PhysicalButton.DPAD_DOWN: _vertical_wrap_down(6),
    },
)

PAUSE_MENU = MenuDef(
    name="pause",
    buttons=(
        ButtonRegion("RESTART",          0.780, 0.315, 0.120, 0.035),
        ButtonRegion("RESUME",           0.730, 0.435, 0.120, 0.035),
        ButtonRegion("HELP AND OPTIONS", 0.700, 0.500, 0.140, 0.035),
    ),
    default_index=1,
    direction=NavDirection.VERTICAL,
    wraps=True,
    neighbors={
        PhysicalButton.DPAD_UP: _vertical_wrap_up(3),
        PhysicalButton.DPAD_DOWN: _vertical_wrap_down(3),
    },
)

# Difficulty tabs re-measured from analysis/linux-launcher/menu-fixtures/
# level-select-1280x800.png (PIL + OCR of the EASY/MEDIUM/HARD/ALL labels,
# 2026-09-11).  The tabs are white labels on the red chevron/trapezoid bar;
# each center below is a SOLID RED point on the bar under/near its label
# (EASY word ~x384-547, MEDIUM ~x736-895/y80-120, HARD ~x962-1066/y88-126,
# ALL ~x1161-1225/y96-132).  All four centers verified solid red in the
# fixture.  Method: analysis/linux-launcher/tap-hardening/MEASUREMENT.md.

LEVEL_SELECT = MenuDef(
    name="level_select",
    buttons=(
        ButtonRegion("EASY",   0.4844, 0.1650, 0.060, 0.035),
        ButtonRegion("MEDIUM", 0.6375, 0.1062, 0.060, 0.030),
        ButtonRegion("HARD",   0.7969, 0.1200, 0.055, 0.030),
        ButtonRegion("ALL",    0.9336, 0.1275, 0.050, 0.030),
    ),
    default_index=0,
    direction=NavDirection.HORIZONTAL,
    wraps=False,
    # Tab row: horizontal, edge-locked at EASY (left) and ALL (right).
    neighbors={
        PhysicalButton.DPAD_LEFT: {0: 0, 1: 0, 2: 1, 3: 2},
        PhysicalButton.DPAD_RIGHT: {0: 1, 1: 2, 2: 3, 3: 3},
    },
)

# RESULTS screen — bottom row (RETRY / VIEW / REPLAY / CONTINUE), horizontal.
# Centres re-measured from results-continue-red-1280x800.png (1271x800, PIL,
# 2026-09-11): RETRY/VIEW/REPLAY are white labels on dark trapezoid buttons,
# CONTINUE is the red trapezoid in the bottom-right corner.  Each center is
# verified ON its button (dark or red) in the fixture.  Fractional coordinates
# adapt to any landscape resolution.
RESULTS_MENU = MenuDef(
    name="results",
    buttons=(
        ButtonRegion("RETRY",    0.1102, 0.9188, 0.060, 0.040),
        ButtonRegion("VIEW",     0.3855, 0.9313, 0.050, 0.040),
        ButtonRegion("REPLAY",   0.4800, 0.9375, 0.045, 0.040),
        ButtonRegion("CONTINUE", 0.8513, 0.9575, 0.100, 0.050),
    ),
    default_index=0,
    direction=NavDirection.HORIZONTAL,
    wraps=False,
    # Bottom row: horizontal, edge-locked at RETRY (left) and CONTINUE (right).
    neighbors={
        PhysicalButton.DPAD_LEFT: {0: 0, 1: 0, 2: 1, 3: 2},
        PhysicalButton.DPAD_RIGHT: {0: 1, 1: 2, 2: 3, 3: 3},
    },
)

SETTINGS_MENU = MenuDef(
    name="settings",
    buttons=(),
    state=MenuState.UNSUPPORTED,
)

OTHER_MENU = MenuDef(
    name="other_menu",
    buttons=(),
    state=MenuState.UNSUPPORTED,
)


MENU_DEFS: dict[str, MenuDef] = {
    "main_menu": MAIN_MENU,
    "pause": PAUSE_MENU,
    "level_select": LEVEL_SELECT,
    "results": RESULTS_MENU,
    "settings": SETTINGS_MENU,
    "other_menu": OTHER_MENU,
}


def get_menu_for_screen(screen_name: str) -> MenuDef | None:
    return MENU_DEFS.get(screen_name)


def detect_buttons_from_capture(
    cap: ScreenCapture, menu: MenuDef
) -> list[tuple[ButtonRegion, tuple[int, int, int, int]]]:
    """Validate button positions against actual capture pixels.

    Returns list of (button, pixel_bbox) for buttons whose expected
    region matches expected visual characteristics (non-black, colored
    background). Buttons that fail validation are excluded.
    """
    w, h, data = cap.width, cap.height, cap.pixel_data
    results = []
    for btn in menu.buttons:
        bbox = btn.pixel_bbox(w, h)
        cx, cy = btn.pixel_center(w, h)
        # Sample center pixel — menu buttons should have visible content
        offset = (cy * w + cx) * 4
        if offset + 3 >= len(data):
            continue
        c = Color(data[offset], data[offset + 1], data[offset + 2])
        # Main menu buttons have red/colored backgrounds (R > 100)
        # Reject if pixel is pure black (probably wrong location)
        if c.r < 15 and c.g < 15 and c.b < 15:
            continue
        results.append((btn, bbox))
    return results
