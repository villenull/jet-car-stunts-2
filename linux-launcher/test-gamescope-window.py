#!/usr/bin/env python3
"""Pure policy tests: no X server, emulator, or desktop changes."""
from dataclasses import replace
import unittest
import tempfile
from pathlib import Path
from unittest import mock
import gamescope_window as guard
from gamescope_window import Window, WindowPolicy


class FakeX11:
    def __init__(self):
        self.actions = []
        self.cursor_windows = []

    def hide_cursor(self, xid):
        self.cursor_windows.append(xid)

    def unmap(self, xid):
        self.actions.append(('unmap', xid))

    def present(self, xid):
        self.actions.append(('present', xid))


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.x = FakeX11()
        self.logs = []
        self.policy = WindowPolicy(123, self.x, lambda event, **fields: self.logs.append((event, fields)))
        self.main = Window(10, 123, ('Emulator', 'Emulator'),
                           'Android Emulator - hardened_api28:5594', 1280, 720, True)
        self.toolbar = Window(11, 123, ('Emulator', 'Emulator'), 'Emulator', 54, 506, True)

    def test_suppresses_only_owned_toolbar_and_presents_main(self):
        unrelated = [replace(self.toolbar, xid=20, pid=456),
                     replace(self.toolbar, xid=21, classes=('Steam',)),
                     replace(self.toolbar, xid=22, title='Steam'),
                     replace(self.toolbar, xid=23, width=700),
                     replace(self.toolbar, xid=24, height=100)]
        result = self.policy.update([self.main, self.toolbar, *unrelated])
        self.assertEqual(self.x.actions, [('unmap', 11), ('present', 10)])
        self.assertIsNone(result)
        confirmed = self.policy.update([self.main, replace(self.toolbar, mapped=False), *unrelated])
        self.assertEqual(confirmed, {'pid': 123, 'main_xid': 10, 'toolbar_xids': [11]})

    def test_boot_is_hidden_until_explicit_reveal(self):
        self.assertIsNone(self.policy.update([self.main, self.toolbar], reveal=False))
        self.assertEqual(self.x.actions, [('unmap', 11), ('unmap', 10)])
        self.x.actions.clear()
        hidden = [replace(self.main, mapped=False), replace(self.toolbar, mapped=False)]
        self.assertIsNone(self.policy.update(hidden, reveal=False))
        self.assertEqual(self.x.actions, [])
        self.assertIsNone(self.policy.update(hidden, reveal=True))
        self.assertEqual(self.x.actions, [('present', 10)])
        self.assertIsNotNone(self.policy.update([self.main, hidden[1]], reveal=True))

    def test_cursor_scope_is_owned_main_only_even_when_steam_present(self):
        steam = replace(self.main, xid=99, pid=999, classes=('Steam',))
        self.policy.update([steam, self.main, self.toolbar])
        self.assertEqual(self.x.cursor_windows, [10])
        self.x.actions.clear()
        self.policy.update([steam, self.main, replace(self.toolbar, mapped=False)])
        self.assertEqual(self.x.cursor_windows, [10, 10])
        self.assertEqual(self.x.actions, [])

    def test_controls_panel_suspends_game_focus_then_returns_once(self):
        self.policy.update([self.main])
        self.x.actions.clear()
        self.policy.update([self.main, self.toolbar], panel_active=True)
        self.assertEqual(self.x.actions, [('unmap', 11)])
        self.x.actions.clear()
        self.policy.update([self.main], panel_active=True)
        self.assertEqual(self.x.actions, [])
        self.policy.update([self.main], panel_active=False)
        self.assertEqual(self.x.actions, [('present', 10)])
        self.x.actions.clear()
        self.policy.update([self.main], panel_active=False)
        self.assertEqual(self.x.actions, [])

    def test_no_main_does_not_touch_toolbar(self):
        self.assertIsNone(self.policy.update([self.toolbar]))
        self.assertEqual(self.x.actions, [])

    def test_other_pid_main_cannot_authorize_toolbar_change(self):
        self.assertIsNone(self.policy.update([replace(self.main, pid=456), self.toolbar]))
        self.assertEqual(self.x.actions, [])

    def test_ambiguous_main_does_not_change_anything(self):
        self.assertIsNone(self.policy.update([self.main, replace(self.main, xid=12), self.toolbar]))
        self.assertEqual(self.x.actions, [])

    def test_stable_poll_never_repeatedly_steals_focus(self):
        self.policy.update([self.main, self.toolbar])
        hidden = replace(self.toolbar, mapped=False)
        self.x.actions.clear()
        self.policy.update([self.main, hidden])
        self.policy.update([self.main, hidden])
        self.assertEqual(self.x.actions, [])
        self.assertEqual(sum(event == 'window-inventory' for event, _ in self.logs), 2)

    def test_toolbar_reappearance_suppressed_and_focus_repaired_once(self):
        hidden = replace(self.toolbar, mapped=False)
        self.policy.update([self.main, hidden])
        self.x.actions.clear()
        self.policy.update([self.main, self.toolbar])
        self.policy.update([self.main, hidden])
        self.assertEqual(self.x.actions, [('unmap', 11), ('present', 10)])

    def test_main_recreated_can_be_presented(self):
        self.policy.update([self.main])
        self.x.actions.clear()
        self.policy.update([replace(self.main, xid=15)])
        self.assertEqual(self.x.actions, [('present', 15)])

    def test_main_must_be_mapped_before_ready(self):
        hidden_main = replace(self.main, mapped=False)
        self.assertIsNone(self.policy.update([hidden_main]))
        self.assertIsNone(self.policy.update([hidden_main]))
        self.assertEqual(self.x.actions, [('present', 10)])
        self.assertIsNotNone(self.policy.update([self.main]))

    def test_visible_toolbar_never_claims_ready(self):
        self.assertIsNone(self.policy.update([self.main, self.toolbar]))
        self.assertIsNone(self.policy.update([self.main, self.toolbar]))
        self.assertIsNotNone(self.policy.update([self.main, replace(self.toolbar, mapped=False)]))

    def test_inventory_order_does_not_generate_changes(self):
        hidden = replace(self.toolbar, mapped=False)
        self.policy.update([self.main, hidden])
        self.policy.update([hidden, self.main])
        self.assertEqual(sum(event == 'window-inventory' for event, _ in self.logs), 1)

    def test_external_quit_hide_is_not_remapped_by_helper(self):
        """The runner's confirmed-Quit hide must stick.

        Drive the real policy to ready (as the helper does pre-quit), then
        simulate the runner's owned unmap of the presented main (e.g. the
        Quit hide): the next helper pass must NOT map/present it again —
        no remap race. A genuinely recreated window (new xid) MAY still be
        presented, so the helper does not go blind.
        """
        hidden_toolbar = replace(self.toolbar, mapped=False)
        self.policy.update([self.main, self.toolbar])
        self.assertIsNotNone(self.policy.update([self.main, hidden_toolbar]))
        self.x.actions.clear()
        # Runner hid the owned main out-of-band; helper re-inventories.
        self.assertIsNone(self.policy.update([replace(self.main, mapped=False), hidden_toolbar]))
        self.assertNotIn(('present', 10), self.x.actions)
        self.assertEqual([a for a in self.x.actions if a[0] == 'present'], [])
        # Recreated main (new xid) is still picked up exactly once.
        self.x.actions.clear()
        self.policy.update([replace(self.main, xid=15), hidden_toolbar])
        self.assertIn(('present', 15), self.x.actions)


class HelperDeadlineTests(unittest.TestCase):
    def run_helper(self, reveal_exists):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / 'ready.json'
            reveal = Path(directory) / 'reveal.json'
            if reveal_exists:
                reveal.write_text('{}')
            adapter = mock.Mock()
            adapter.inventory.return_value = []
            adapter.fileno.return_value = 56
            adapter.drain.return_value = 0
            with mock.patch.object(__import__('sys'), 'argv',
                                   ['guard', '--pid', '123', '--ready-file', str(ready), '--reveal-file', str(reveal)]), \
                 mock.patch.object(guard.os, 'pidfd_open', return_value=55), \
                 mock.patch.object(guard.os, 'close') as close, \
                 mock.patch.object(guard.signal, 'signal'), \
                 mock.patch.object(guard, 'X11', return_value=adapter), \
                 mock.patch.object(guard, 'log'), \
                 mock.patch.object(guard.select, 'select', side_effect=[([], [], []), ([], [], []), ([55], [], [])]), \
                 mock.patch.object(guard.time, 'monotonic', side_effect=[1000, 1031]):
                status = guard.main()
            self.assertFalse(ready.exists())
            adapter.close.assert_called_once()
            close.assert_called_once_with(55)
            return status

    def test_pre_reveal_wait_uses_runner_deadlines_and_closes_on_emulator_exit(self):
        self.assertEqual(self.run_helper(False), 0)

    def test_wakes_on_display_events_and_rechecks_events_seen_during_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = mock.Mock()
            adapter.inventory.return_value = []
            adapter.fileno.return_value = 56
            adapter.drain.side_effect = [0, 2, 0, 0]
            argv = ['guard', '--pid', '123', '--ready-file', str(Path(directory) / 'ready.json'),
                    '--reveal-file', str(Path(directory) / 'reveal.json')]
            with mock.patch.object(__import__('sys'), 'argv', argv), \
                 mock.patch.object(guard.os, 'pidfd_open', return_value=55), \
                 mock.patch.object(guard.os, 'close'), \
                 mock.patch.object(guard.signal, 'signal'), \
                 mock.patch.object(guard, 'X11', return_value=adapter), \
                 mock.patch.object(guard, 'log'), \
                 mock.patch.object(guard.select, 'select', side_effect=[([], [], [])] * 4 + [([55], [], [])]) as waits:
                self.assertEqual(guard.main(), 0)
        self.assertEqual([c.args for c in waits.call_args_list[1:4:2]],
                         [([55, 56], [], [], guard.ACTIVE_POLL_S), ([55, 56], [], [], guard.IDLE_POLL_S)])

    def test_x11_event_symbols_exist_without_opening_display(self):
        import ctypes
        try:
            lib = ctypes.CDLL('libX11.so.6')
        except OSError:
            self.skipTest('libX11 not installed')
        for name in ('XSelectInput', 'XConnectionNumber', 'XPending', 'XNextEvent'):
            self.assertTrue(hasattr(lib, name), name)
        self.assertEqual(ctypes.sizeof(guard.Event), 192 if ctypes.sizeof(ctypes.c_long) == 8 else 96)

    def test_missing_main_after_reveal_times_out_and_closes_resources(self):
        self.assertEqual(self.run_helper(True), 1)


if __name__ == '__main__':
    unittest.main()
