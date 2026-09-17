#!/usr/bin/env python3
"""Isolated visible-presentation test for the Quit hide (needs root grant).

Proves the runner's ``hide_owned_emulator_window`` unmaps a real mapped X11
window and leaves foreign windows alone — without touching the live user
session (:0), Steam, ports 5038/5594/5595, or any device.

Requirements for --mode xvfb (NOT present on the 2026-09-14 host):
  Xvfb, libX11. Requirements for --mode desktop (prepared, NOT run here):
  libX11 only, plus a root-granted slot with NO user game running
  (preconditions enforced in-code: ports free, no emulator windows).
  Root grants a slot after the tilt live release.

Refuses to run against a live game session: desktop mode requires
--allow-desktop AND free ports AND zero existing emulator windows, creates
one small uniquely-titled owned window (never fullscreen, never focused,
never _NET_ACTIVE_WINDOW), unmaps it via the real hide, verifies UNMAPPED
before terminating the owned creator, then kills only its captured pgids.
Even when green, this covers hide mechanics only — final Steam GamingMode
compositing proof still needs a user session (see
analysis/release-workers/quit-gaming-followup.md).
"""
from __future__ import annotations

import ctypes as C
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "linux-launcher"))

DISPLAY = ":99"
FORBIDDEN = (":0", os.environ.get("DISPLAY", ""))


class XCreate:
    """Minimal Xlib creation bindings (the guard module only wraps guard ops)."""

    def __init__(self, display: bytes):
        lib = C.CDLL("libX11.so.6")
        ptr, ul, i = C.c_void_p, C.c_ulong, C.c_int
        self.lib = lib

        def bind(name, result, *args):
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = result, list(args)
            return fn

        self.open = bind("XOpenDisplay", ptr, C.c_char_p)
        self.close = bind("XCloseDisplay", i, ptr)
        self.root = bind("XDefaultRootWindow", ul, ptr)
        self.screen = bind("XDefaultScreen", i, ptr)
        self.create = bind("XCreateSimpleWindow", ul, ptr, ul, i, i, C.c_uint,
                           C.c_uint, C.c_uint, ul, ul)
        self.map = bind("XMapWindow", i, ptr, ul)
        self.flush = bind("XFlush", i, ptr)
        self.change = bind("XChangeProperty", i, ptr, ul, ul, ul, i, i,
                           C.c_void_p, C.c_int)
        self.intern = bind("XInternAtom", ul, ptr, C.c_char_p, i)
        self.set_name = bind("XStoreName", i, ptr, ul, C.c_char_p)
        self.attrs = bind("XGetWindowAttributes", i, ptr, ul, C.c_void_p)
        self.display = self.open(display)
        if not self.display:
            raise RuntimeError(f"cannot open {display!r}")

    def atom(self, name: str):
        return self.intern(self.display, name.encode(), 0)

    def set_string(self, xid, name, value: bytes):
        self.change(self.display, xid, self.atom(name), self.atom("STRING"),
                    8, 0, value, len(value))

    def set_pid(self, xid, pid: int):
        atom = self.atom("_NET_WM_PID")
        cardinal = self.atom("CARDINAL")
        value = (C.c_ulong * 1)(pid)
        self.change(self.display, xid, atom, cardinal, 32, 0, value, 1)

    def store_name(self, xid, name: bytes):
        self.set_name(self.display, xid, name)

    def set_class(self, xid, res_name: bytes, res_class: bytes):
        blob = res_name + b"\0" + res_class + b"\0"
        self.change(self.display, xid, self.atom("WM_CLASS"), self.atom("STRING"),
                    8, 0, blob, len(blob))


def free_tcp_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def tcp_closed(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def parse_args(argv):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("xvfb", "desktop"), default="xvfb",
                        help="xvfb: owned nested server (needs Xvfb); "
                             "desktop: current display, owned probe window only")
    parser.add_argument("--display", default=":99",
                        help="display for the owned Xvfb server (xvfb mode)")
    parser.add_argument("--allow-desktop", action="store_true",
                        help="required for desktop mode: confirms a root-granted "
                             "no-game slot; preconditions are still enforced")
    return parser.parse_args(argv)


def desktop_preconditions(display: str) -> str | None:
    """Return None when a desktop probe run is safe, else the refusal reason."""
    for port in (5038, 5594, 5595):
        if not tcp_closed(port):
            return f"port {port} busy (a session owns it)"
    from gamescope_window import X11
    try:
        guard = X11()
    except Exception as error:
        return f"cannot open {display}: {error}"
    try:
        clashes = [w for w in guard.inventory()
                   if w.title.startswith("Android Emulator - ")]
    finally:
        guard.close()
    if clashes:
        return f"{len(clashes)} emulator window(s) present (user game at slot)"
    return None


def wait_for_display(display: str, timeout: float = 10.0) -> bool:
    """Readiness probe via libX11 only (no xdpyinfo dependency)."""
    lib = C.CDLL("libX11.so.6")
    lib.XOpenDisplay.restype = C.c_void_p
    lib.XOpenDisplay.argtypes = [C.c_char_p]
    lib.XCloseDisplay.restype = C.c_int
    lib.XCloseDisplay.argtypes = [C.c_void_p]
    deadline = time.monotonic() + timeout
    while True:
        handle = lib.XOpenDisplay(display.encode())
        if handle:
            lib.XCloseDisplay(handle)
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def run_probe(display: str, children: list, tmp: Path, owned: bool,
              main_title: bytes, main_size: tuple[int, int]) -> int:
    """Create windows, hide the owned one, verify, then own-TERM. Shared."""
    import argparse
    import runner
    from gamescope_window import X11

    def spawn_owned(argv, env=None):
        proc = subprocess.Popen(argv, start_new_session=True,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=env)
        children.append(proc)
        return proc

    env = dict(os.environ, DISPLAY=display)
    owner = spawn_owned(["sleep", "120"], env=env)  # fake emulator pid
    time.sleep(0.2)

    creator = XCreate(display.encode())
    black = 0
    white = 0xFFFFFF
    root = creator.root(creator.display)
    width, height = main_size
    main_xid = creator.create(creator.display, root, 0, 0, width, height, 0, black, white)
    if owned:
        creator.set_class(main_xid, b"qemu-system-x86_64", b"Emulator")
    else:
        # Distinct instance name on a shared desktop; the class element
        # 'Emulator' is still required by the hide's owned validation, and
        # the real window-guard helper filters by pid so it ignores us.
        creator.set_class(main_xid, b"jcs2-quitprobe", b"Emulator")
    creator.set_string(main_xid, "_NET_WM_NAME", main_title)
    creator.store_name(main_xid, main_title)
    creator.set_pid(main_xid, owner.pid)
    foreign_xid = creator.create(creator.display, root, 0, 0, 640, 480, 0, black, white)
    creator.set_class(foreign_xid, b"steam", b"Steam")
    creator.set_string(foreign_xid, "_NET_WM_NAME", b"Steam")
    creator.set_pid(foreign_xid, os.getpid())
    creator.map(creator.display, main_xid)
    creator.map(creator.display, foreign_xid)
    creator.flush(creator.display)
    time.sleep(0.5)

    ready = tmp / "gamescope-window-ready.json"
    ready.write_text(json.dumps({"pid": owner.pid, "main_xid": main_xid,
                                 "toolbar_xids": []}) + "\n")

    launcher = runner.Launcher.__new__(runner.Launcher)
    launcher.args = argparse.Namespace()
    launcher.run_dir = tmp
    launcher.emulator = type("Owned", (), {})()
    launcher.emulator.process = owner
    events: list = []
    launcher.log = lambda message, **fields: events.append((message, fields))

    old_display = os.environ.get("DISPLAY")
    os.environ["DISPLAY"] = display
    try:
        launcher.hide_owned_emulator_window(opener=X11)
    finally:
        if old_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = old_display
    time.sleep(0.5)

    # Validate UNMAPPED before terminating the owned creator (mirrors
    # begin_quit_teardown order: hide, verify, then owned TERM).
    guard = X11()
    try:
        states = {w.xid: w.mapped for w in guard.inventory()
                  if w.xid in (main_xid, foreign_xid)}
    finally:
        guard.close()
    creator.close(creator.display)

    hide_rows = [f for m, f in events if m == "stage=game-hide"]
    if states.get(main_xid) is not False:
        print(f"FAIL: owned main still mapped: {states}")
        return 1
    if states.get(foreign_xid) is not True:
        print(f"FAIL: foreign window disturbed: {states}")
        return 1
    if not hide_rows or hide_rows[0].get("via") != "recorded-identity":
        print(f"FAIL: expected recorded-identity hide, events={events}")
        return 1
    print(f"PASS: owned main {main_xid} unmapped via recorded-identity; "
          f"foreign {foreign_xid} untouched")
    return 0


def stop_children(children: list) -> None:
    for proc in reversed(children):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + 5
    for proc in reversed(children):
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def main(argv=None) -> int:
    opts = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        C.CDLL("libX11.so.6")
    except OSError:
        print("SKIP: libX11.so.6 not installed")
        return 2

    children: list[subprocess.Popen] = []
    tmp = Path(tempfile.mkdtemp(prefix="jcs2-quit-present-"))
    try:
        if opts.mode == "xvfb":
            display = opts.display
            if shutil.which("Xvfb") is None:
                print("SKIP: Xvfb not installed (no nested display available)")
                return 2
            if display in FORBIDDEN:
                print(f"REFUSE: target display {display} collides with the live session")
                return 1
            server = subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1280x800x24"],
                start_new_session=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            children.append(server)
            if not wait_for_display(display):
                print("FAIL: owned Xvfb never became ready")
                return 1
            return run_probe(display, children, tmp, owned=True,
                             main_title=b"Android Emulator - quitpresent:5594",
                             main_size=(1280, 800))
        # Desktop mode: current display, strictly owned probe window.
        if not opts.allow_desktop:
            print("REFUSE: desktop mode needs --allow-desktop (root-granted no-game slot)")
            return 1
        display = os.environ.get("DISPLAY", "")
        if not display:
            print("REFUSE: no DISPLAY for desktop mode")
            return 1
        reason = desktop_preconditions(display)
        if reason is not None:
            print(f"REFUSE: {reason}")
            return 1
        probe_title = f"JCS2 Quit Probe - {os.getpid()}".encode()
        return run_probe(display, children, tmp, owned=False,
                         main_title=probe_title, main_size=(320, 200))
    finally:
        stop_children(children)
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
