#!/usr/bin/env python3
"""Keep Gamescope on one owned emulator's game window, not its Qt toolbar."""
from __future__ import annotations

import argparse
import ctypes as C
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import select
import signal
import time


SUBSTRUCTURE_NOTIFY_MASK = 1 << 19
ACTIVE_POLL_S = 0.01
IDLE_POLL_S = 0.25


@dataclass(frozen=True)
class Window:
    xid: int
    pid: int
    classes: tuple[str, ...]
    title: str
    width: int
    height: int
    mapped: bool


class Color(C.Structure):
    _fields_ = [("pixel", C.c_ulong), ("red", C.c_ushort), ("green", C.c_ushort),
                ("blue", C.c_ushort), ("flags", C.c_char), ("pad", C.c_char)]


class Attributes(C.Structure):
    _fields_ = [("x", C.c_int), ("y", C.c_int), ("width", C.c_int),
                ("height", C.c_int), ("border_width", C.c_int), ("depth", C.c_int),
                ("visual", C.c_void_p), ("root", C.c_ulong), ("class_", C.c_int),
                ("bit_gravity", C.c_int), ("win_gravity", C.c_int),
                ("backing_store", C.c_int), ("backing_planes", C.c_ulong),
                ("backing_pixel", C.c_ulong), ("save_under", C.c_int),
                ("colormap", C.c_ulong), ("map_installed", C.c_int),
                ("map_state", C.c_int), ("all_event_masks", C.c_long),
                ("your_event_mask", C.c_long), ("do_not_propagate_mask", C.c_long),
                ("override_redirect", C.c_int), ("screen", C.c_void_p)]


class ClientData(C.Union):
    _fields_ = [("b", C.c_char * 20), ("s", C.c_short * 10), ("l", C.c_long * 5)]


class ClientMessage(C.Structure):
    _fields_ = [("type", C.c_int), ("serial", C.c_ulong), ("send_event", C.c_int),
                ("display", C.c_void_p), ("window", C.c_ulong),
                ("message_type", C.c_ulong), ("format", C.c_int), ("data", ClientData)]


class Event(C.Union):
    _fields_ = [("client", ClientMessage), ("padding", C.c_long * 24)]


class X11:
    def __init__(self):
        self.lib = C.CDLL('libX11.so.6')
        def bind(name, result, *args):
            fn = getattr(self.lib, name)
            fn.restype, fn.argtypes = result, list(args)
            return fn
        ptr = C.c_void_p
        ul = C.c_ulong
        self.open = bind('XOpenDisplay', ptr, C.c_char_p)
        self.close_display = bind('XCloseDisplay', C.c_int, ptr)
        self.root_window = bind('XDefaultRootWindow', ul, ptr)
        self.intern = bind('XInternAtom', ul, ptr, C.c_char_p, C.c_int)
        self.get_property = bind('XGetWindowProperty', C.c_int, ptr, ul, ul,
                                 C.c_long, C.c_long, C.c_int, ul,
                                 C.POINTER(ul), C.POINTER(C.c_int), C.POINTER(ul),
                                 C.POINTER(ul), C.POINTER(C.POINTER(C.c_ubyte)))
        self.free = bind('XFree', C.c_int, ptr)
        self.query_tree = bind('XQueryTree', C.c_int, ptr, ul, C.POINTER(ul),
                               C.POINTER(ul), C.POINTER(C.POINTER(ul)),
                               C.POINTER(C.c_uint))
        self.attributes = bind('XGetWindowAttributes', C.c_int, ptr, ul, C.POINTER(Attributes))
        self.unmap_window = bind('XUnmapWindow', C.c_int, ptr, ul)
        self.map_raised = bind('XMapRaised', C.c_int, ptr, ul)
        self.send_event = bind('XSendEvent', C.c_int, ptr, ul, C.c_int, C.c_long, C.POINTER(Event))
        self.flush = bind('XFlush', C.c_int, ptr)
        self.bitmap = bind('XCreateBitmapFromData', ul, ptr, ul, C.c_char_p, C.c_uint, C.c_uint)
        self.pixmap_cursor = bind('XCreatePixmapCursor', ul, ptr, ul, ul,
                                  C.POINTER(Color), C.POINTER(Color), C.c_uint, C.c_uint)
        self.define_cursor = bind('XDefineCursor', C.c_int, ptr, ul, ul)
        self.free_pixmap = bind('XFreePixmap', C.c_int, ptr, ul)
        self.free_cursor = bind('XFreeCursor', C.c_int, ptr, ul)
        self.select_input = bind('XSelectInput', C.c_int, ptr, ul, C.c_long)
        self.connection_number = bind('XConnectionNumber', C.c_int, ptr)
        self.pending = bind('XPending', C.c_int, ptr)
        self.next_event = bind('XNextEvent', C.c_int, ptr, C.POINTER(Event))
        # Windows can vanish between inventory calls. Xlib's default error
        # handler exits the process; retain a callback for the connection life.
        self.error_callback = C.CFUNCTYPE(C.c_int, ptr, ptr)(lambda _d, _e: 0)
        bind('XSetErrorHandler', ptr, type(self.error_callback))(self.error_callback)
        self.display = self.open(None)
        if not self.display:
            raise RuntimeError('cannot open X11 DISPLAY')
        self.root = self.root_window(self.display)
        # Wake on top-level create/map instead of only a 250 ms poll, so the
        # boot window is hidden as soon as the emulator maps it.
        self.select_input(self.display, self.root, SUBSTRUCTURE_NOTIFY_MASK)
        self.atoms: dict[str, int] = {}
        pixel = self.bitmap(self.display, self.root, b'\0', 1, 1)
        color = Color()
        self.blank_cursor = self.pixmap_cursor(self.display, pixel, pixel,
                                               C.byref(color), C.byref(color), 0, 0)
        self.free_pixmap(self.display, pixel)
        if not self.blank_cursor:
            self.close_display(self.display)
            raise RuntimeError('cannot create emulator cursor')

    def atom(self, name):
        if name not in self.atoms:
            self.atoms[name] = self.intern(self.display, name.encode(), 0)
        return self.atoms[name]

    def property(self, xid, name):
        actual, fmt, count, remaining = C.c_ulong(), C.c_int(), C.c_ulong(), C.c_ulong()
        data = C.POINTER(C.c_ubyte)()
        rc = self.get_property(self.display, xid, self.atom(name), 0, 4096, 0, 0,
                               C.byref(actual), C.byref(fmt), C.byref(count),
                               C.byref(remaining), C.byref(data))
        try:
            if rc or not data:
                return None
            if fmt.value == 8:
                return C.string_at(data, count.value)
            if fmt.value == 32:
                return tuple(C.cast(data, C.POINTER(C.c_ulong))[i] for i in range(count.value))
            return None
        finally:
            if data:
                self.free(data)

    def inventory(self):
        pending, seen, windows = [self.root], set(), []
        while pending and len(seen) < 4096:
            xid = pending.pop()
            if xid in seen:
                continue
            seen.add(xid)
            root, parent, count = C.c_ulong(), C.c_ulong(), C.c_uint()
            children = C.POINTER(C.c_ulong)()
            try:
                if self.query_tree(self.display, xid, C.byref(root), C.byref(parent),
                                   C.byref(children), C.byref(count)):
                    pending.extend(children[i] for i in range(count.value))
            finally:
                if children:
                    self.free(children)
            pid = self.property(xid, '_NET_WM_PID')
            if not isinstance(pid, tuple) or len(pid) != 1:
                continue
            attrs = Attributes()
            if not self.attributes(self.display, xid, C.byref(attrs)):
                continue
            classes = self.property(xid, 'WM_CLASS')
            title = self.property(xid, '_NET_WM_NAME') or self.property(xid, 'WM_NAME')
            classes = classes if isinstance(classes, bytes) else b''
            title = title if isinstance(title, bytes) else b''
            windows.append(Window(xid, pid[0], tuple(classes.decode(errors='replace').strip('\0').split('\0')),
                                  title.decode(errors='replace').rstrip('\0'), attrs.width,
                                  attrs.height, attrs.map_state != 0))
        return windows

    def fileno(self):
        return self.connection_number(self.display)

    def drain(self):
        """Discard queued notifications; return how many were pending."""
        count, event = 0, Event()
        while count < 4096 and self.pending(self.display) > 0:
            self.next_event(self.display, C.byref(event))
            count += 1
        return count

    def unmap(self, xid):
        self.unmap_window(self.display, xid)
        self.flush(self.display)

    def message(self, xid, name, values):
        event = Event()
        event.client.type = 33  # ClientMessage
        event.client.display = self.display
        event.client.window = xid
        event.client.message_type = self.atom(name)
        event.client.format = 32
        for i, value in enumerate(values):
            event.client.data.l[i] = value
        self.send_event(self.display, self.root, 0, (1 << 20) | (1 << 19), C.byref(event))

    def present(self, xid):
        self.map_raised(self.display, xid)
        self.message(xid, '_NET_WM_STATE', [1, self.atom('_NET_WM_STATE_FULLSCREEN'), 0, 2, 0])
        self.message(xid, '_NET_ACTIVE_WINDOW', [2, 0, 0, 0, 0])
        self.flush(self.display)

    def hide_cursor(self, xid):
        # A window cursor changes appearance only within this owned render
        # tree. Unlike screen-wide cursor hiding, Steam keeps its own cursor.
        pending, seen = [xid], set()
        while pending and len(seen) < 4096:
            child = pending.pop()
            if child in seen:
                continue
            seen.add(child)
            self.define_cursor(self.display, child, self.blank_cursor)
            root, parent, count = C.c_ulong(), C.c_ulong(), C.c_uint()
            children = C.POINTER(C.c_ulong)()
            try:
                if self.query_tree(self.display, child, C.byref(root), C.byref(parent),
                                   C.byref(children), C.byref(count)):
                    pending.extend(children[i] for i in range(count.value))
            finally:
                if children:
                    self.free(children)
        self.flush(self.display)

    def close(self):
        self.free_cursor(self.display, self.blank_cursor)
        self.close_display(self.display)


def log(event, **fields):
    print(json.dumps(dict(time=time.time(), event=event, **fields), sort_keys=True), flush=True)


class WindowPolicy:
    def __init__(self, pid, adapter, logger=log):
        self.pid, self.adapter, self.log = pid, adapter, logger
        self.main_xid = None
        self.last_inventory = None
        self.panel_active = False

    def update(self, windows, reveal=True, panel_active=False):
        return_from_panel = self.panel_active and not panel_active
        self.panel_active = panel_active
        owned = sorted((w for w in windows if w.pid == self.pid and 'Emulator' in w.classes),
                       key=lambda w: w.xid)
        if owned != self.last_inventory:
            self.log('window-inventory', windows=[asdict(w) for w in owned])
            self.last_inventory = owned
        mains = [w for w in owned if w.title.startswith('Android Emulator - ')]
        # Ambiguous/no main: change no windows, even if a toolbar exists.
        if len(mains) != 1:
            return None
        main = mains[0]
        toolbars = [w for w in owned if w.title == 'Emulator' and
                    0 < w.width < 160 and w.height > 200 and w.height > w.width]
        remapped = [w for w in toolbars if w.mapped]
        for toolbar in remapped:
            self.adapter.unmap(toolbar.xid)
            self.log('toolbar-unmapped', xid=toolbar.xid, pid=self.pid)
        self.adapter.hide_cursor(main.xid)
        if not reveal:
            if main.mapped:
                self.adapter.unmap(main.xid)
                self.log('startup-main-hidden', xid=main.xid, pid=self.pid)
            return None
        if panel_active:
            return None
        if self.main_xid != main.xid or remapped or return_from_panel:
            self.adapter.present(main.xid)
            self.log('main-presented', xid=main.xid, pid=self.pid)
            self.main_xid = main.xid
            # X11 requests are asynchronous. A fresh inventory must confirm
            # the intended state before the runner can launch the game.
            return None
        if not main.mapped or remapped:
            return None
        return {'pid': self.pid, 'main_xid': main.xid,
                'toolbar_xids': [w.xid for w in toolbars]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', required=True, type=int)
    parser.add_argument('--ready-file', required=True, type=Path)
    parser.add_argument('--reveal-file', type=Path, help='keep boot UI hidden until runner marks game resumed')
    parser.add_argument('--panel-file', type=Path, help='suspend game focus while controls panel is open')
    args = parser.parse_args()
    stopped = False
    def stop(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    adapter = None
    pidfd = None
    try:
        pidfd = os.pidfd_open(args.pid)
        adapter = X11()
        policy = WindowPolicy(args.pid, adapter)
        # The runner bounds every pre-reveal startup stage and owns this
        # helper/emulator. Only start the window readiness deadline at reveal.
        deadline = None if args.reveal_file else time.monotonic() + 30
        ready = False
        display_fd = adapter.fileno()
        while not stopped and not select.select([pidfd], [], [], 0)[0]:
            adapter.drain()  # This pass observes whatever those events changed.
            reveal = args.reveal_file is None or args.reveal_file.is_file()
            if reveal and deadline is None:
                deadline = time.monotonic() + 30
            result = policy.update(adapter.inventory(), reveal=reveal,
                                   panel_active=args.panel_file is not None and args.panel_file.exists())
            if result and not ready:
                temporary = args.ready_file.with_name(args.ready_file.name + f'.{os.getpid()}.tmp')
                temporary.write_text(json.dumps(result) + '\n')
                temporary.replace(args.ready_file)
                ready = True
                log('ready', **result)
            if not ready and deadline is not None and time.monotonic() >= deadline:
                raise RuntimeError('owned emulator game window did not become ready before startup deadline')
            # Events that arrived during this pass need an immediate recheck.
            timeout = ACTIVE_POLL_S if adapter.drain() else IDLE_POLL_S
            select.select([pidfd, display_fd], [], [], timeout)
        return 0
    except (OSError, RuntimeError) as error:
        log('error', error=str(error))
        return 1
    finally:
        if adapter:
            adapter.close()
        if pidfd is not None:
            os.close(pidfd)


if __name__ == '__main__':
    raise SystemExit(main())
