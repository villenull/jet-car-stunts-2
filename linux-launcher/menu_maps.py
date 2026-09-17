"""Menu layout definitions for JCS2 native OpenGL menus.

All coordinates are in the 1920x1080 landscape display space used by both
ADB screencap and ``input tap``.  Tap targets are placed at the center of
each button's visible text area.

The game does NOT support native D-pad/focus navigation in its OpenGL menus.
Menu selection is implemented externally via ``input tap`` at known button
coordinates, translating D-pad up/down to cursor movement.

Verified coordinates are marked; others are derived from pixel analysis of
screencap images.  Only verified coordinates should be trusted for automated
use without human spot-checking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class MenuButton:
    label: str
    x: int
    y: int
    verified: bool = False


@dataclass(frozen=True)
class MenuLayout:
    name: str
    buttons: tuple[MenuButton, ...]
    default_index: int = 0
    nav_vertical: bool = True
    nav_horizontal: bool = False

    @property
    def count(self) -> int:
        return len(self.buttons)


MAIN_MENU = MenuLayout(
    name="main_menu",
    buttons=(
        MenuButton("PLAY",              850, 300, verified=True),
        MenuButton("CREATE",            830, 370, verified=False),
        MenuButton("USER CHALLENGES",   830, 440, verified=False),
        MenuButton("HELP AND OPTIONS",  900, 540, verified=False),
        MenuButton("USER LEVELS",       900, 660, verified=False),
        MenuButton("STORE",             800, 750, verified=False),
    ),
    default_index=0,
)

PAUSE_MENU = MenuLayout(
    name="pause",
    buttons=(
        MenuButton("RESTART",           1500, 340, verified=False),
        MenuButton("RESUME",            1400, 470, verified=False),
        MenuButton("HELP AND OPTIONS",  1350, 540, verified=False),
    ),
    default_index=1,
)

LEVEL_SELECT = MenuLayout(
    name="level_select",
    buttons=(
        MenuButton("Easy",   1100, 180, verified=False),
        MenuButton("Medium", 1300, 180, verified=False),
        MenuButton("Hard",   1480, 180, verified=True),
    ),
    default_index=0,
    nav_vertical=False,
    nav_horizontal=True,
)

MENUS = {
    "main_menu": MAIN_MENU,
    "pause": PAUSE_MENU,
    "level_select": LEVEL_SELECT,
}

FOOTER_QUIT = MenuButton("QUIT", 300, 1010, verified=False)
FOOTER_QUIT_LEVEL = MenuButton("QUIT LEVEL", 350, 1010, verified=False)
FOOTER_PROFILE = MenuButton("PROFILE", 1600, 1010, verified=False)
