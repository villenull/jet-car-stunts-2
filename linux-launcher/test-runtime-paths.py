#!/usr/bin/env python3
"""Offline resolver tests: temporary files only, no runtime/device calls."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from runtime_paths import resolve_paths


class RuntimePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='jcs2 paths ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_legacy_preserves_guest_without_windows_dependency(self):
        p = resolve_paths(self.root, {})
        self.assertEqual(p.sdk, self.root / 'analysis/arm-runtime-20260910T010130Z/runtime/sdk')
        self.assertEqual(p.avd_home, self.root / 'analysis/hardened-runtime-20260910T150000Z/avdhome')
        self.assertEqual(p.dist, self.root)
        (self.root / 'runtime/sdk').mkdir(parents=True)
        self.assertEqual(resolve_paths(self.root, {}), p)

    def test_portable_is_explicit(self):
        p = resolve_paths(self.root, {'JCS2_LAYOUT': 'portable'})
        self.assertEqual(p.sdk, self.root / 'runtime/sdk')
        self.assertEqual(p.avd_home, self.root / 'state/avd')
        self.assertEqual(p.dist / 'controller/mapping.json', self.root / 'assets/controller/mapping.json')
        self.assertEqual(p.logdir, self.root / 'state/logs')

    def test_overrides_are_root_relative_even_from_other_cwd(self):
        env = {'JCS2_LAYOUT': 'portable', 'JCS2_SDK': 'other sdk',
               'JCS2_AVD_HOME': 'other avd', 'JCS2_DIST': 'other assets',
               'JCS2_CONTROLLER': '/tmp/explicit controller', 'JCS2_AVD': 'saved',
               'JCS2_LOGDIR': 'other logs'}
        previous = Path.cwd()
        try:
            os.chdir('/tmp')
            p = resolve_paths(self.root, env)
        finally:
            os.chdir(previous)
        self.assertEqual(p.sdk, self.root / 'other sdk')
        self.assertEqual(p.avd_home, self.root / 'other avd')
        self.assertEqual(p.dist, self.root / 'other assets')
        self.assertEqual(p.controller, Path('/tmp/explicit controller'))
        self.assertEqual(p.avd, 'saved')
        self.assertEqual(p.logdir, self.root / 'other logs')
        self.assertEqual(p.environment()['ANDROID_HOME'], str(p.sdk))

    def test_installed_marker_selects_portable_without_environment(self):
        (self.root / 'jcs2-layout.json').write_text('{"schema": 1, "layout": "portable", "avd": "jcs2"}')
        p = resolve_paths(self.root, {})
        self.assertEqual((p.layout, p.avd, p.sdk), ('portable', 'jcs2', self.root / 'runtime/sdk'))
        self.assertEqual(p.avd_home / 'jcs2.avd', self.root / 'state/avd/jcs2.avd')
        self.assertEqual(resolve_paths(self.root, {'JCS2_AVD': 'other'}).avd, 'other')

    def test_malformed_installed_marker_fails_closed(self):
        for body in ('{', '[]', '{"schema": 1, "layout": "legacy"}', '{"schema": 2, "layout": "portable"}',
                     '{"schema": 1, "layout": "portable", "avd": "../x"}',
                     '{"schema": 1, "layout": "portable", "sdk": "/tmp"}'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                (self.root / 'jcs2-layout.json').write_text(body)
                resolve_paths(self.root, {})

    def test_reject_invalid_configuration(self):
        for env in ({'JCS2_LAYOUT': 'auto'}, {'JCS2_SDK': ''}, {'JCS2_AVD': '../guest'}):
            with self.subTest(env=env), self.assertRaises(ValueError):
                resolve_paths(self.root, env)

    def test_check_is_read_only_and_never_launches(self):
        p = resolve_paths(self.root, {'JCS2_LAYOUT': 'portable'})
        with patch('subprocess.run', side_effect=AssertionError('must not launch')), patch('subprocess.Popen', side_effect=AssertionError('must not launch')):
            self.assertEqual(len(p.missing()), 7)
            self.assertEqual(list(self.root.iterdir()), [])
            for path in (p.sdk / 'platform-tools/adb', p.sdk / 'emulator/emulator', p.controller,
                         p.helper_jar, p.dist / 'controller/mapping.json',
                         p.avd_home / 'hardened_api28.ini', p.avd_home / 'hardened_api28.avd/config.ini'):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                path.chmod(0o755)
            self.assertEqual(p.missing(), [])


if __name__ == '__main__':
    unittest.main()
