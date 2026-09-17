"""Menu navigation state machine.

Processes raw physical events BEFORE gameplay remapping.  JCS2 menus are
TOUCH UI — they respond to taps, not gamepad focus.  This layer implements
DIRECTIONAL TAP NAVIGATION:

  * D-pad in a supported menu taps the immediately-adjacent entry (per the
    screen's neighbor table) — the tap itself produces the game's select
    flash on the newly selected button.
  * A re-taps the cursor to confirm/activate the flashed button.
  * B issues KEYCODE_BACK.
  * D-pad passes through to the game ONLY in gameplay (car control) and is
    dropped in UNKNOWN / unsupported screens (fail-closed): it never reaches
    the game inside a menu.

Never taps during gameplay.  Never taps on unsupported/unknown screens.
Fail-closed throughout.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Callable

from .adb_adapter import AdbAdapter
from .event_types import (
    ButtonAction, DPAD_BUTTONS, MENU_NAV_BUTTONS,
    PhysicalButton, RawAxisEvent, RawButtonEvent,
)
from .menu_layout import (
    ButtonRegion, MenuDef, MenuState, get_menu_for_screen,
)
from .screen_detect import AsyncScreenDetector, Screen


class NavigationTrace:
    """Records navigation actions for dry-run inspection."""

    def __init__(self):
        self.entries: list[dict] = []

    def record(self, action: str, **kw):
        self.entries.append({"t": time.monotonic(), "action": action, **kw})


class MenuNavigator:
    """Stateful menu navigation controller.

    Processes raw physical button events and manages:
    - Screen detection (async, bounded, cached)
    - Cursor position within current menu (directional tap nav)
    - D-pad direction → tap immediately-adjacent entry (select flash)
    - A-press → confirm tap on cursor (via adapter, dry-run in tests)
    - B-press → back navigation
    """

    def __init__(
        self,
        detector: AsyncScreenDetector,
        adapter: AdbAdapter,
        on_highlight_change: Callable[[str, int, ButtonRegion | None, tuple[int, int]], None] | None = None,
        on_menu_leave: Callable[[], None] | None = None,
        trace: NavigationTrace | None = None,
    ):
        self._detector = detector
        self._adapter = adapter
        self._on_highlight = on_highlight_change
        self._on_leave = on_menu_leave
        self._trace = trace

        self._current_screen: Screen = Screen.UNKNOWN
        self._current_menu: MenuDef | None = None
        self._cursor: int = 0
        self._active = False

    def _log(self, action: str, **kw):
        if self._trace:
            self._trace.record(action, **kw)

    def _resolution(self) -> tuple[int, int]:
        r = self._detector.last_resolution
        return r if r else (0, 0)

    def _enter_menu(self, menu: MenuDef):
        self._current_menu = menu
        self._cursor = menu.default_index
        self._active = True
        self._log("enter_menu", menu=menu.name, cursor=self._cursor)
        self._emit_highlight()

    def _leave_menu(self):
        if self._active:
            self._log("leave_menu")
            self._active = False
            self._current_menu = None
            if self._on_leave:
                self._on_leave()

    def _emit_highlight(self):
        if not self._on_highlight or not self._current_menu:
            return
        if self._current_menu.state != MenuState.SUPPORTED:
            return
        if self._cursor >= self._current_menu.count:
            return
        btn = self._current_menu.buttons[self._cursor]
        res = self._resolution()
        self._on_highlight(
            self._current_menu.name, self._cursor, btn, res,
        )

    def _refresh_screen(self, force: bool = False) -> Screen:
        result = self._detector.detect(force=force)
        new_screen = result.screen

        if new_screen != self._current_screen:
            old = self._current_screen
            self._current_screen = new_screen
            self._log("screen_change", old=old.value, new=new_screen.value)

            menu = get_menu_for_screen(new_screen.value)
            if menu and menu.state == MenuState.SUPPORTED and menu.count > 0:
                self._enter_menu(menu)
            else:
                self._leave_menu()

        return new_screen

    def _move_directional(self, button: PhysicalButton):
        """Directional tap: tap the immediately-adjacent entry in `button`'s
        direction (per the menu's neighbor table).  The tapped entry becomes
        the cursor; the tap itself produces the game's select flash.  At an
        edge with no adjacent entry nothing is tapped (cursor stays).
        """
        menu = self._current_menu
        if menu is None or menu.count == 0:
            return
        old = self._cursor
        target = menu.neighbor(button, old)
        if target == old:
            self._log("dpad_edge", direction=button.value, old=old,
                      label=menu.buttons[old].label)
            return
        self._cursor = target
        btn = menu.buttons[target]
        self._log("dpad_select", direction=button.value, old=old, new=target,
                  label=btn.label)
        self._tap_button(btn)

    def _tap_button(self, btn: ButtonRegion):
        """Tap a button's pixel center via the adapter (select flash)."""
        res = self._resolution()
        if res == (0, 0):
            self._log("tap_no_resolution", label=btn.label)
            return
        cx, cy = btn.pixel_center(*res)
        self._log("tap", label=btn.label, x=cx, y=cy, res=res)
        self._adapter.input_tap(cx, cy)

    def _confirm_current(self):
        """A-press: re-tap the cursor to confirm the flashed selection.

        JCS2 menus are touch UI — the D-pad tap flashes the button; the A-press
        (a second tap on the same spot) activates it.  Force a re-detect after
        the confirm tap since it likely changes the screen.
        """
        menu = self._current_menu
        if not menu or menu.count == 0 or menu.state != MenuState.SUPPORTED:
            return
        btn = menu.buttons[self._cursor]
        self._log("confirm", label=btn.label, cursor=self._cursor)
        self._tap_button(btn)
        # Force re-detect after confirm tap since screen likely changed
        self._detector.detect(force=True)

    def _handle_back(self):
        """B button → KEYCODE_BACK (4) for menu back navigation."""
        self._log("back")
        self._adapter.input_key(4)  # KEYCODE_BACK

    def handle_button(self, event: RawButtonEvent) -> bool:
        """Process a raw physical button event.

        Returns True if the event was consumed (menu navigation).
        Returns False if it should pass through to gameplay.

        D-pad: in a supported menu it taps the immediately-adjacent entry
        (directional tap nav); in gameplay it passes through (car control);
        in UNKNOWN or unsupported screens it is consumed and dropped
        (fail-closed — it never reaches the game there).
        """
        if event.button not in MENU_NAV_BUTTONS:
            return False

        # D-pad: directional tap nav in supported menus; pass through in
        # gameplay; drop in UNKNOWN / unsupported screens (fail-closed).
        if event.button in DPAD_BUTTONS:
            screen = self._refresh_screen()
            if screen == Screen.UNKNOWN:
                # Consumed but not acted on — safe drop (fail-closed)
                self._log("dpad_dropped", screen=screen.value)
                return True
            if (self._active and self._current_menu is not None
                    and self._current_menu.state == MenuState.SUPPORTED):
                if event.action == ButtonAction.DOWN:
                    self._move_directional(event.button)
                return True  # consumed — no D-pad reaches the game in menus
            if screen == Screen.GAMEPLAY:
                # Pass through: D-pad controls the car during gameplay.
                self._log("dpad_forward", screen=screen.value)
                return False
            # Unsupported menu (settings/other) — no tap map.  Safe drop.
            self._log("dpad_dropped", screen=screen.value,
                      menu=self._current_menu.name if self._current_menu else None)
            return True

        # A: confirm tap on cursor in menu, pass through in gameplay
        if event.button == PhysicalButton.A:
            screen = self._refresh_screen()
            if self._active and event.action == ButtonAction.DOWN:
                self._confirm_current()
                return True
            return False  # pass through for gameplay boost

        # B: back in menu, pass through in gameplay
        if event.button == PhysicalButton.B:
            screen = self._refresh_screen()
            if self._active and event.action == ButtonAction.DOWN:
                self._handle_back()
                return True
            return False  # pass through for gameplay handbrake

        return False

    def handle_axis(self, event: RawAxisEvent) -> bool:
        """Axis events always pass through — never consumed by menu nav."""
        return False

    @property
    def state(self) -> dict:
        return {
            "screen": self._current_screen.value,
            "active": self._active,
            "menu": self._current_menu.name if self._current_menu else None,
            "cursor": self._cursor,
            "label": (
                self._current_menu.buttons[self._cursor].label
                if self._current_menu and self._cursor < self._current_menu.count
                else None
            ),
        }
