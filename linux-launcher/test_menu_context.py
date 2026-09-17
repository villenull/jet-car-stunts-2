#!/usr/bin/env python3
"""Offline regressions for touch-only menus (historical test entry point)."""
import builtins
import io
import json
import runpy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import runner


class TouchOnlyMenuTests(unittest.TestCase):
    def test_runtime_import_and_routing_need_no_navigation_or_processes(self):
        original_import = builtins.__import__
        def guarded_import(name, *args, **kwargs):
            if name == 'navigation_v2' or name.startswith('navigation_v2.'):
                raise AssertionError('runtime requested retired menu navigation')
            return original_import(name, *args, **kwargs)
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch('builtins.__import__', side_effect=guarded_import), \
             mock.patch.object(runner.subprocess, 'run', side_effect=AssertionError('unexpected subprocess')), \
             mock.patch.object(runner.subprocess, 'Popen', side_effect=AssertionError('unexpected subprocess')), \
             mock.patch.object(threading.Thread, 'start', side_effect=AssertionError('unexpected detector thread')):
            namespace = runpy.run_path(str(Path(runner.__file__)))
            launcher = mock.Mock(run_dir=Path(temp))
            destination = io.BytesIO()
            router = namespace['InputRouter'](launcher, destination)
            try:
                for key in runner.UNASSIGNED_BUTTONS:
                    for action in ('down', 'up'):
                        self.assertFalse(router._handle_side_channel_event(
                            dict(type='button', key=key, action=action, t_ms=0)))
                router.pump(0)
                self.assertEqual(destination.getvalue(), b'')
                launcher.adb.assert_not_called()
                self.assertFalse((Path(temp) / 'cursor.json').exists())
            finally:
                router.close()

    def test_fallback_and_legacy_routes_also_drop_unassigned_buttons(self):
        with tempfile.TemporaryDirectory() as temp:
            launcher = mock.Mock(run_dir=Path(temp))
            destination = io.BytesIO()
            router = runner.InputRouter(launcher, destination)
            router._deck_controls = None
            try:
                for key in runner.UNASSIGNED_BUTTONS:
                    event = dict(type='button', key=key, action='down', t_ms=0)
                    self.assertFalse(router._handle_side_channel_event(event))
                    router.forward((json.dumps(event) + '\n').encode())
                self.assertEqual(destination.getvalue(), b'')
                launcher.adb.assert_not_called()
            finally:
                router.close()


if __name__ == '__main__':
    unittest.main()
