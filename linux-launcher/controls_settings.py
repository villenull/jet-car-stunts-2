#!/usr/bin/env python3
"""Touch-first driving mode selector before the emulator starts / while paused.

Launcher modes (persisted via ``save_selection`` prelaunch or ``request_switch``
live; the router is the sole writer at runtime):
- Gamepad (default): every physical control is forwarded; drive with the
  physical sticks. Keep the in-game Gamepad toggle ON.
- Tilt Drive: physical LX/LY sticks are disabled host-side and steering comes
  from Deck tilt. Triggers, bumpers, and buttons are unaffected. Keep the
  in-game Gamepad toggle ON — the game's own OFF setting is NOT used (proven
  live: the game ignores sensor input while its Gamepad is OFF, so Tilt Drive
  keeps the game ON; nothing about the game's OFF was changed or fixed).

Honesty marks (live-proven Sep-15): sensor-to-calibration response is proven;
actual in-race tilt steering and analog brake effect are UNVERIFIED in this
build. ``read_status`` remains the router protocol surface.
"""
from pathlib import Path
import argparse
import json
import socket
import traceback
import uuid
from tilt_control.settings import TiltSettings, ControlMode


def save_selection(settings: TiltSettings, mode: str) -> None:
    """Prelaunch persistence for the launcher driving mode.

    Called by the prelaunch panel buttons. Reversible at any time by
    choosing the other mode (or deleting the settings file, which
    defaults to Gamepad). The in-game Gamepad toggle is never touched.
    """
    if mode not in ('gamepad', 'tilt'):
        raise ValueError('Choose Gamepad or Tilt Drive')
    settings.mode = ControlMode(mode)


def request_switch(run_dir: Path, mode: str) -> str:
    """Send an explicit user selection to the already-running input router."""
    if mode not in ('gamepad', 'tilt'):
        raise ValueError('Choose Gamepad or Tilt Drive')
    request_id = uuid.uuid4().hex
    payload = {'type': 'control_mode', 'mode': mode, 'request_id': request_id}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(0.5)
        client.connect(str(run_dir / 'input.sock'))
        client.sendall((json.dumps(payload) + '\n').encode())
    return request_id


def read_status(run_dir: Path, request_id: str | None = None) -> dict | None:
    try:
        data = json.loads((run_dir / 'control-mode.json').read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('mode') not in ('gamepad', 'tilt'):
        return None
    if request_id is not None and data.get('request_id') != request_id:
        return None
    return data


def show_settings(settings_path: Path | str | None = None, run_dir: Path | None = None) -> bool:
    """Mode selector + guidance + sensor status panel.

    Prelaunch (``run_dir`` None): Gamepad / Tilt Drive buttons persist the
    launcher mode, then Cancel / Play (Play starts the game without touching
    anything else). Live: mode buttons switch the running router, then Return
    resumes the game (the runner neutralizes inputs on open and restores them
    after close). Never touches the in-game Gamepad toggle or any save.
    """
    # Lazy imports keep offline preflight and unit tests display-independent.
    import tkinter as tk
    settings = TiltSettings(settings_path)
    current = settings.mode.value
    window = tk.Tk(className='JCS2Controls')
    window.title('JCS2 Controls')
    window.geometry('1280x800')
    window.configure(bg='#16202c')
    window.attributes('-fullscreen', True)
    proceed = False
    foreground, background = '#ffffff', '#16202c'
    tk.Label(window, text='JCS2 Controls', font=('sans', 30, 'bold'),
             fg=foreground, bg=background).pack(pady=(40, 12))
    tk.Label(window, text='Driving mode (launcher; reversible anytime):',
             font=('sans', 20), fg=foreground, bg=background).pack(pady=10)
    chosen = {'mode': current}
    mode_row = tk.Frame(window, bg=background)
    mode_row.pack(pady=8)

    def pick(mode_value: str):
        chosen['mode'] = mode_value
        try:
            if run_dir is not None:
                request_switch(run_dir, mode_value)
            else:
                save_selection(settings, mode_value)
        except Exception:
            pass
        refresh_marks()

    gamepad_btn = tk.Button(mode_row, text='Gamepad (sticks)', font=('sans', 22, 'bold'),
                            width=20, pady=14, command=lambda: pick('gamepad'))
    tilt_btn = tk.Button(mode_row, text='Tilt Drive', font=('sans', 22, 'bold'),
                         width=20, pady=14, command=lambda: pick('tilt'))
    gamepad_btn.pack(side='left', padx=18)
    tilt_btn.pack(side='left', padx=18)

    def refresh_marks():
        gamepad_btn.configure(bg='#7cd4a1' if chosen['mode'] == 'gamepad' else background)
        tilt_btn.configure(bg='#7cd4a1' if chosen['mode'] == 'tilt' else background)

    refresh_marks()
    tk.Label(window,
             text=('Keep the in-game Gamepad toggle ON in both modes.\n'
                   'Tilt Drive disables the physical sticks; triggers and '
                   'buttons are unaffected.\n'
                   "The game's OFF setting is not used (tilt is ignored while OFF).\n"
                   'In-race tilt steering + brake effect: UNVERIFIED in this build.'),
             font=('sans', 20), fg=foreground, bg=background,
             justify='center').pack(pady=(16, 8))
    tk.Label(window, text=sensor_summary(run_dir), font=('sans', 16),
             fg='#c6d0dd', bg=background, wraplength=1100,
             justify='center').pack(pady=22)

    def close(proceed_value: bool):
        nonlocal proceed
        proceed = proceed_value
        window.destroy()

    buttons = tk.Frame(window, bg=background)
    buttons.pack(pady=20)
    if run_dir is not None:
        tk.Button(buttons, text='Return to game', font=('sans', 23, 'bold'), width=18,
                  pady=18, bg='#7cd4a1', command=lambda: close(True)).pack(side='left', padx=18)
    else:
        tk.Button(buttons, text='Cancel', font=('sans', 23), width=15,
                  pady=18, command=lambda: close(False)).pack(side='left', padx=18)
        # No progression selector (user scope Sept 14: all levels unlocked
        # from the start). Play starts the game without touching anything.
        tk.Button(buttons, text='Play', font=('sans', 23, 'bold'), width=18,
                  pady=18, bg='#7cd4a1', command=lambda: close(True)).pack(side='left', padx=18)
    window.protocol('WM_DELETE_WINDOW', window.destroy)
    window.mainloop()
    return proceed


def sensor_summary(run_dir: Path | None) -> str:
    """Short host/guest tilt status for the touch panel (never raises)."""
    if run_dir is None:
        return ('Tilt needs the Deck motion sensor and the game feed; '
                'both are checked automatically at launch.')
    try:
        status = read_status(run_dir)
    except Exception:
        status = None
    if status is None:
        return ('Game status unavailable; in Tilt Drive sticks stay disabled '
                '— select Gamepad to restore them.')
    host = 'Deck sensor ready' if status.get('sensor_available') else 'Deck sensor unavailable'
    native = status.get('native_sensor') or 'unknown'
    if native == 'live':
        return host + '; game tilt feed live.'
    if native in ('unavailable', 'degraded'):
        return host + '; ' + str(status.get('native_error') or 'game tilt feed unavailable')
    return host + '; game tilt feed status unknown.'


PLAY, CANCEL, PANEL_ERROR = 0, 1, 3


def main(argv=None) -> int:
    """Exit PLAY/CANCEL; PANEL_ERROR means the UI itself failed (e.g. no Tk)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, help='owned live launcher run directory')
    parser.add_argument('--settings', type=Path, help='prelaunch settings file (default: launcher log directory)')
    args = parser.parse_args(argv)
    try:
        return PLAY if show_settings(args.settings, run_dir=args.run_dir) else CANCEL
    except Exception:
        traceback.print_exc()
        return PANEL_ERROR


if __name__ == '__main__':
    raise SystemExit(main())
