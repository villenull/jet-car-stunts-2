#!/usr/bin/env python3
"""Behavioral unit tests for the Linux runner's state and input boundaries."""

import io
import json
import os
import selectors
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import runner


class TiltIntegrationTests(unittest.TestCase):
    """Tests for tilt_control wiring in InputRouter."""

    def setUp(self):
        # These are offline integration tests, never open Deck motion devices.
        patcher = mock.patch("tilt_control.adapter.TiltAdapter.open", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make_router(self) -> tuple[runner.InputRouter, Path]:
        temp = tempfile.mkdtemp()
        launcher = mock.Mock()
        launcher.run_dir = Path(temp)
        destination = io.BytesIO()
        router = runner.InputRouter(launcher, destination)
        return router, Path(temp)

    def test_start_tilt_returns_false_when_import_unavailable(self):
        """start_tilt returns False if tilt_control is not importable."""
        router, tmpdir = self._make_router()
        try:
            # Temporarily make TiltAdapter None
            original = runner.TiltAdapter
            runner.TiltAdapter = None
            try:
                result = router.start_tilt()
                self.assertFalse(result)
                self.assertIsNone(router._tilt_adapter)
            finally:
                runner.TiltAdapter = original
        finally:
            router.close()
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_start_tilt_initializes_adapter(self):
        """start_tilt creates TiltAdapter and calls the mocked device boundary."""
        router, tmpdir = self._make_router()
        try:
            settings_path = str(Path(tmpdir) / "tilt-settings.json")
            result = router.start_tilt(settings_path)
            self.assertTrue(result)
            if result:
                self.assertIsNotNone(router._tilt_adapter)
                self.assertTrue(router.tilt_mode in (True, False))
        finally:
            router.close()
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_tilt_mode_property_when_no_adapter(self):
        """tilt_mode returns False when no adapter is initialized."""
        router, tmpdir = self._make_router()
        try:
            self.assertFalse(router.tilt_mode)
        finally:
            router.close()
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_tilt_mode_reflects_settings(self):
        """tilt_mode reflects the adapter's current mode."""
        router, tmpdir = self._make_router()
        try:
            settings_path = str(Path(tmpdir) / "tilt-settings.json")
            router.start_tilt(settings_path)
            if router._tilt_adapter is not None:
                from tilt_control.settings import ControlMode
                # Default is GAMEPAD
                self.assertEqual(router._tilt_adapter.mode, ControlMode.GAMEPAD)
                self.assertFalse(router.tilt_mode)
                # Switch to TILT
                router._tilt_adapter.select_mode(ControlMode.TILT)
                self.assertTrue(router.tilt_mode)
                # Switch back
                router._tilt_adapter.select_mode(ControlMode.GAMEPAD)
                self.assertFalse(router.tilt_mode)
        finally:
            router.close()
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_mirror_native_sensor_when_no_adapter(self):
        """_mirror_native_sensor is a no-op when no adapter exists."""
        router, tmpdir = self._make_router()
        try:
            initial_buf = bytes(router.forward_buffer)
            router._mirror_native_sensor()
            self.assertEqual(bytes(router.forward_buffer), initial_buf)
            self.assertIsNone(router._sensor_transport)
        finally:
            router.close()
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_mirror_without_imu_leaves_guest_sensor_alone(self):
        """Without an owned IMU the guest sensor is never touched."""
        router, tmpdir = self._make_router()
        try:
            settings_path = str(Path(tmpdir) / "tilt-settings.json")
            router.start_tilt(settings_path)
            if router._tilt_adapter is not None:
                self.assertFalse(router._tilt_adapter._open)
                initial_buf = bytes(router.forward_buffer)
                router._mirror_native_sensor()
                # No virtual events, no console session, no status churn.
                self.assertEqual(bytes(router.forward_buffer), initial_buf)
                self.assertIsNone(router._sensor_transport)
        finally:
            router.close()
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_mirror_pushes_native_when_imu_owned(self):
        """Owned IMU + fake transport yields one native push, zero virtual."""
        router, tmpdir = self._make_router()
        try:
            settings_path = str(Path(tmpdir) / "tilt-settings.json")
            router.start_tilt(settings_path)
            adapter = router._tilt_adapter
            if adapter is not None:
                adapter._open = True
                adapter._estimator._last_ts = __import__('time').monotonic()
                transport = _FakeSensorTransport()
                router._sensor_transport = transport
                with mock.patch.object(adapter, 'consume_motion') as consume:
                    router._mirror_native_sensor()
                consume.assert_not_called()
                self.assertEqual(len(transport.pushes), 1)
                self.assertEqual(router._native_state, "live")
        finally:
            router.close()
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_close_cleans_up_tilt_adapter(self):
        """close() releases the tilt adapter."""
        router, tmpdir = self._make_router()
        try:
            settings_path = str(Path(tmpdir) / "tilt-settings.json")
            router.start_tilt(settings_path)
            adapter = router._tilt_adapter
            router.close()
            self.assertIsNone(router._tilt_adapter)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_pump_calls_mirror_native_sensor(self):
        """pump() calls _mirror_native_sensor before processing selector."""
        router, tmpdir = self._make_router()
        try:
            with mock.patch.object(router, '_mirror_native_sensor') as mock_mirror:
                router.pump(0.01)
                mock_mirror.assert_called_once()
        finally:
            router.close()
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


class _FakeSensorTransport:
    """Minimal fake console transport for router tests (no sockets)."""

    def __init__(self):
        self.pushes = []
        self.is_open = True
        self.last_error = None

    def set_acceleration(self, vector, *, force=False):
        self.pushes.append((tuple(vector), force))
        return True

    def close(self):
        self.is_open = False


class RunnerBehaviorTests(unittest.TestCase):
    def test_window_guard_only_starts_in_visible_gaming_mode(self):
        for desktop, headless, expected in (("Hyprland", False, False),
                                             ("gamescope", True, False),
                                             ("gamescope", False, True)):
            with self.subTest(desktop=desktop, headless=headless):
                launcher = object.__new__(runner.Launcher)
                launcher.args = mock.Mock(headless=headless)
                launcher.emulator = mock.Mock()
                launcher.emulator.process.pid = 123
                launcher.run_dir = Path("/tmp/unused-jcs2-unit-test")
                launcher.spawn = mock.Mock()
                launcher.window_guard = None
                with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": desktop}, clear=True):
                    launcher.start_gamescope_window_guard()
                self.assertEqual(launcher.spawn.called, expected)
                if expected:
                    argv = launcher.spawn.call_args.args[1]
                    self.assertEqual(argv[argv.index("--pid") + 1], "123")
                    self.assertIn(str(launcher.run_dir / "gamescope-window-ready.json"), argv)

    def test_failed_window_guard_prevents_readiness(self):
        launcher = object.__new__(runner.Launcher)
        launcher.window_guard = mock.Mock()
        launcher.window_guard.process.poll.return_value = 1
        launcher.run_dir = Path("/tmp/unused-jcs2-unit-test")
        launcher.stop_requested = False
        with self.assertRaisesRegex(runner.LauncherError, "window setup failed"):
            launcher.verify_gamescope_window()

    def test_window_guard_timeout_does_not_claim_ready(self):
        launcher = object.__new__(runner.Launcher)
        launcher.window_guard = mock.Mock()
        launcher.window_guard.process.poll.return_value = None
        launcher.run_dir = Path("/tmp/unused-jcs2-unit-test")
        launcher.stop_requested = False
        with mock.patch.object(runner.time, 'monotonic', side_effect=[0, 31]):
            with self.assertRaisesRegex(runner.LauncherError, "startup deadline"):
                launcher.verify_gamescope_window()

    def test_reveal_marker_only_after_game_resumed(self):
        with tempfile.TemporaryDirectory() as temp:
            launcher = object.__new__(runner.Launcher)
            launcher.run_dir = Path(temp)
            launcher.window_guard = mock.Mock()
            launcher.log = mock.Mock()
            launcher.adb = mock.Mock(return_value=mock.Mock(stdout='activity'))
            marker = launcher.run_dir / 'game-window-reveal.json'
            with mock.patch.object(runner, 'wait_until', side_effect=runner.LauncherError('not resumed')):
                with self.assertRaisesRegex(runner.LauncherError, 'not resumed'):
                    launcher.launch_game()
            self.assertFalse(marker.exists())
            with mock.patch.object(runner, 'wait_until'):
                launcher.launch_game()
            self.assertEqual(json.loads(marker.read_text())['resumed_package'], runner.GAME)

    def test_foreground_requires_resumed_activity(self):
        self.assertEqual(
            runner.resumed_package(
                "mCurrentFocus=Window{u0 com.trueaxis.jetcarstunts2/.Jetcarstunts2Activity}\n"
                "mResumedActivity: ActivityRecord{u0 com.other/.MainActivity}"
            ),
            "com.other",
        )
        self.assertIsNone(runner.resumed_package("mCurrentFocus=Window{u0 com.trueaxis.jetcarstunts2/.MainActivity}"))

    def test_network_accepts_unknown_loopback_state_but_not_other_up_flags(self):
        ok, _ = runner.isolated_network("1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 state UNKNOWN", "", "")
        self.assertTrue(ok)
        ok, _ = runner.isolated_network("1: lo: <LOOPBACK> mtu 65536 state UNKNOWN", "", "")
        self.assertTrue(ok)
        ok, detail = runner.isolated_network(
            "1: lo: <LOOPBACK,UP> mtu 65536 state UNKNOWN\n2: eth0: <BROADCAST,UP> mtu 1500 state UNKNOWN", "", ""
        )
        self.assertFalse(ok)
        self.assertIn("eth0", detail)
        ok, _ = runner.isolated_network("1: lo: <LOOPBACK,UP> mtu 65536 state UNKNOWN", "default dev lo", "")
        self.assertFalse(ok)

    def test_isolation_script_is_one_remote_shell_argument(self):
        launcher = object.__new__(runner.Launcher)
        argv = launcher.isolation_argv()
        self.assertEqual(argv[-2], "shell")
        self.assertEqual(argv[-1], runner.ISOLATION_SCRIPT)
        self.assertIn("svc wifi disable", argv[-1])
        self.assertNotIn("sh", argv[-2:])

    def test_wifi_disabled_requires_actual_disabled_evidence(self):
        self.assertTrue(runner.wifi_disabled("0\n", ""))
        self.assertTrue(runner.wifi_disabled("unknown\n", "mWifiEnabled: false"))
        self.assertFalse(runner.wifi_disabled("1\n", "Wi-Fi enabled: true"))

    def test_root_unroot_reconnect_retries_until_device(self):
        launcher = object.__new__(runner.Launcher)
        launcher.adb = mock.Mock(side_effect=[
            mock.Mock(returncode=1, stdout="offline\n"),
            mock.Mock(returncode=0, stdout="device\n"),
        ])
        launcher.wait_for_adb_device("fake", timeout=1)
        self.assertEqual(launcher.adb.call_count, 2)

    def test_ready_matches_exact_device_name_and_id(self):
        dump = "  3: JCS2 Virtual Xbox Controller\n  Device 3: JCS2 Virtual Xbox Controller\n"
        self.assertTrue(runner.input_device_matches(dump, 3))
        self.assertFalse(runner.input_device_matches(dump, 4))
        self.assertFalse(runner.input_device_matches("  3: Other Controller\n", 3))

    def test_ui_python_prefers_explicit_then_project_then_installed_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(runner.ui_python(root, {}), runner.sys.executable)
            installed = root / "runtime/python/bin/python3"
            project = root / "runtime/ui-python/bin/python3"
            for path in (installed, project):
                path.parent.mkdir(parents=True)
                path.write_text("#!/bin/sh\n")
                path.chmod(0o755)
            self.assertEqual(runner.ui_python(root, {}), str(project))
            project.chmod(0o644)
            self.assertEqual(runner.ui_python(root, {}), str(installed))
            self.assertEqual(runner.ui_python(root, {"JCS2_UI_PYTHON": str(installed)}), str(installed))
            for bad in ("python3", str(root / "missing"), str(project)):
                with self.subTest(bad=bad), self.assertRaises(runner.LauncherError):
                    runner.ui_python(root, {"JCS2_UI_PYTHON": bad})

    def test_prelaunch_chooser_uses_ui_python_and_maps_exit_codes(self):
        for returncode, play in ((0, True), (1, False), (3, True), (-15, False)):
            with self.subTest(returncode=returncode), tempfile.TemporaryDirectory() as temp:
                launcher = object.__new__(runner.Launcher)
                launcher.run_dir = Path(temp) / "run"
                launcher.stop_requested = False
                launcher.log = mock.Mock()
                process = mock.Mock(returncode=returncode)
                process.poll.side_effect = [None, returncode]
                launcher.spawn = mock.Mock(return_value=mock.Mock(process=process))
                with mock.patch.object(runner, "ui_python", return_value="/ui/python3"), \
                     mock.patch.object(runner.time, "sleep"):
                    self.assertEqual(launcher.choose_controls_before_launch(), play)
                argv = launcher.spawn.call_args.args[1]
                self.assertEqual(argv[0], "/ui/python3")
                self.assertTrue(argv[1].endswith("linux-launcher/controls_settings.py"))
                self.assertEqual(argv[2:], ["--settings", str(Path(temp) / "tilt-settings.json")])

    def test_live_panel_spawns_with_ui_python(self):
        with tempfile.TemporaryDirectory() as temp:
            launcher = mock.Mock()
            launcher.run_dir = Path(temp)
            router = runner.InputRouter(launcher, io.BytesIO())
            try:
                with mock.patch.object(runner, "ui_python", return_value="/ui/python3"):
                    router.open_controls_panel()
                argv = launcher.spawn.call_args.args[1]
                self.assertEqual(argv[0], "/ui/python3")
                self.assertEqual(argv[2:], ["--run-dir", temp])
            finally:
                router.close()

    def test_packages_require_only_game_unless_explicitly_extended(self):
        launcher = object.__new__(runner.Launcher)
        launcher.log = mock.Mock()
        listing = "package:com.android.settings\npackage:com.trueaxis.jetcarstunts2\n"
        launcher.adb = mock.Mock(return_value=mock.Mock(stdout=listing))
        with mock.patch.dict(os.environ, {}, clear=True):
            launcher.verify_packages()
        with mock.patch.dict(os.environ, {"JCS2_REQUIRE_PACKAGES": "ru.example.helper"}, clear=True):
            with self.assertRaisesRegex(runner.LauncherError, "ru.example.helper"):
                launcher.verify_packages()
        # Prefix matches are not installation evidence.
        launcher.adb.return_value = mock.Mock(stdout="package:com.trueaxis.jetcarstunts2.extra\n")
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(runner.LauncherError):
            launcher.verify_packages()
        for bad in ("../x", "nodots", "a.b;rm"):
            with self.subTest(bad=bad), self.assertRaises(runner.LauncherError):
                runner.required_packages({"JCS2_REQUIRE_PACKAGES": bad})
        self.assertEqual(runner.required_packages({"JCS2_REQUIRE_PACKAGES": "a.b, a.b c.d"}),
                         ["com.trueaxis.jetcarstunts2", "a.b", "c.d"])

    def test_input_router_normalizes_ndjson_and_keeps_partial_socket_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            launcher = mock.Mock()
            launcher.run_dir = Path(temp)
            destination = io.BytesIO()
            router = runner.InputRouter(launcher, destination)
            router.forward(b'{"type":"button", "key":"RB"')
            self.assertEqual(destination.getvalue(), b"")
            router.forward(b', "action":"down"}\n')
            self.assertEqual(destination.getvalue(), b'{"type":"button","key":"RB","action":"down"}\n')
            self.assertTrue((Path(temp) / "input-events.ndjson").read_text().endswith('"action":"down"}\n'))

    def test_input_router_reads_every_line_in_one_bridge_burst(self):
        """A pipe burst must not lose lines prefetched behind the first one."""
        with tempfile.TemporaryDirectory() as temp:
            launcher = mock.Mock()
            launcher.run_dir = Path(temp)
            destination = io.BytesIO()
            router = runner.InputRouter(launcher, destination)
            read_fd, write_fd = os.pipe()
            reader = os.fdopen(read_fd, "rb", buffering=0)
            os.set_blocking(read_fd, False)
            router.producer_buffers[reader] = bytearray()
            router.selector.register(reader, selectors.EVENT_READ, "bridge")
            burst = (
                b'{"type":"button","key":"RB","action":"down"}\n'
                b'{"type":"axis","axis":"LX","value":0.75}\n'
                b'{"type":"button","key":"RB","action":"up"}\n'
            )
            os.write(write_fd, burst)
            router.pump(0.5)
            self.assertEqual(destination.getvalue().decode().splitlines(), [
                '{"type":"button","key":"RB","action":"down"}',
                '{"type":"axis","axis":"LX","value":0.75}',
                '{"type":"button","key":"RB","action":"up"}',
            ])
            router.close()
            reader.close()
            os.close(write_fd)

    def test_dead_owned_adb_server_is_rejected_before_client_spawn(self):
        launcher = object.__new__(runner.Launcher)
        launcher.server = mock.Mock()
        launcher.server.process.poll.return_value = 1
        with self.assertRaises(runner.LauncherError), mock.patch.object(runner, "tcp_open", return_value=False):
            launcher.run(["/tmp/adb", "-P", "5038", "-s", runner.SERIAL, "shell", "id"])

    def test_cleanup_escalates_to_owned_process_group_only(self):
        launcher = object.__new__(runner.Launcher)
        launcher.log = mock.Mock()
        process = mock.Mock(pid=__import__("os").getpid())
        process.poll.return_value = None
        process.wait.side_effect = [runner.subprocess.TimeoutExpired("fake", 1), None]
        owned = runner.OwnedProcess("fake", process)
        with mock.patch.object(runner.os, "killpg", side_effect=[None, ProcessLookupError]) as killpg:
            launcher.stop_owned(owned)
        self.assertEqual(killpg.call_args_list, [
            mock.call(process.pid, runner.signal.SIGTERM), mock.call(process.pid, 0)])

class SettingsUiTiltBridgeTests(unittest.TestCase):
    """Tests for settings_ui to tilt_control bridging."""

    def test_steering_mode_to_control_mode_mapping(self):
        """SteeringMode from settings_ui maps to ControlMode in tilt_control."""
        from navigation_v2.settings_ui import SteeringMode
        from tilt_control.settings import ControlMode
        self.assertEqual(SteeringMode.GAMEPAD.value, ControlMode.GAMEPAD.value)
        self.assertEqual(SteeringMode.TILT.value, ControlMode.TILT.value)

    def test_pitch_mode_to_control_mode_mapping(self):
        """PitchMode from settings_ui maps to ControlMode in tilt_control."""
        from navigation_v2.settings_ui import PitchMode
        from tilt_control.settings import ControlMode
        self.assertEqual(PitchMode.GAMEPAD.value, ControlMode.GAMEPAD.value)
        self.assertEqual(PitchMode.TILT.value, ControlMode.TILT.value)

    def test_settings_ui_sensitivity_maps_to_tilt_gain(self):
        """LauncherSettings.tilt_sensitivity should be usable as tilt_gain."""
        from navigation_v2.settings_ui import LauncherSettings, SteeringMode
        settings = LauncherSettings(
            steering_mode=SteeringMode.TILT,
            tilt_sensitivity=1.5,
        )
        # The sensitivity value can be passed directly as tilt_gain_lx/ly
        self.assertAlmostEqual(settings.tilt_sensitivity, 1.5)

    def test_settings_controller_calibrate_callback(self):
        """SettingsController calibrate action fires callback."""
        from navigation_v2.settings_ui import (
            LauncherSettings, SettingsController, SteeringMode
        )
        calibrated = []
        settings = LauncherSettings()
        ctrl = SettingsController(
            settings,
            on_calibrate=lambda: calibrated.append(True),
        )
        ctrl.activate()
        ctrl._cursor = 5  # calibrate item (index 5 after deadzone+smoothing)
        ctrl.select()
        self.assertEqual(len(calibrated), 1)

    def test_settings_roundtrip_with_tilt_mode(self):
        """LauncherSettings persists and restores TILT mode."""
        import json
        import tempfile
        from pathlib import Path
        from navigation_v2.settings_ui import LauncherSettings, SteeringMode
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            original = LauncherSettings(
                steering_mode=SteeringMode.TILT,
                tilt_sensitivity=1.25,
            )
            original.save(path)
            loaded = LauncherSettings.load(path)
            self.assertEqual(loaded.steering_mode, SteeringMode.TILT)
            self.assertAlmostEqual(loaded.tilt_sensitivity, 1.25)


class TiltSettingsTests(unittest.TestCase):
    """Direct tests for tilt_control.settings persistence."""

    def test_tilt_settings_mode_roundtrip(self):
        """TiltSettings persists and restores mode."""
        import tempfile
        from tilt_control.settings import TiltSettings, ControlMode
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tilt.json"
            settings = TiltSettings(path)
            self.assertEqual(settings.mode, ControlMode.GAMEPAD)
            settings.mode = ControlMode.TILT
            loaded = TiltSettings(path)
            self.assertEqual(loaded.mode, ControlMode.TILT)

    def test_tilt_settings_calibration_persists(self):
        """Calibration data persists across TiltSettings instances."""
        import tempfile
        from tilt_control.settings import TiltSettings
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tilt.json"
            settings = TiltSettings(path)
            cal = {"roll_offset": 0.1, "pitch_offset": 0.2}
            settings.calibration = cal
            loaded = TiltSettings(path)
            self.assertAlmostEqual(loaded.calibration["roll_offset"], 0.1)
            self.assertAlmostEqual(loaded.calibration["pitch_offset"], 0.2)
            self.assertIn("timestamp", loaded.calibration)

    def test_tilt_settings_gain_persists(self):
        """Tilt gain values persist."""
        import tempfile
        from tilt_control.settings import TiltSettings
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tilt.json"
            settings = TiltSettings(path)
            settings.update(tilt_gain_lx=1.5, tilt_gain_ly=0.8)
            loaded = TiltSettings(path)
            self.assertAlmostEqual(loaded.tilt_gain_lx, 1.5)
            self.assertAlmostEqual(loaded.tilt_gain_ly, 0.8)

    def test_tilt_settings_deadzone_persists(self):
        """Deadzone value persists and is clamped."""
        import tempfile
        from tilt_control.settings import TiltSettings
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tilt.json"
            settings = TiltSettings(path)
            settings.update(deadzone=0.15)
            loaded = TiltSettings(path)
            self.assertAlmostEqual(loaded.deadzone, 0.15)

    def test_tilt_settings_smoothing_alpha_persists(self):
        """Smoothing alpha persists and is clamped."""
        import tempfile
        from tilt_control.settings import TiltSettings
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tilt.json"
            settings = TiltSettings(path)
            settings.update(smoothing_alpha=0.85)
            loaded = TiltSettings(path)
            self.assertAlmostEqual(loaded.smoothing_alpha, 0.85)

    def test_tilt_settings_as_dict(self):
        """as_dict returns merged defaults and persisted values."""
        import tempfile
        from tilt_control.settings import TiltSettings, ControlMode
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tilt.json"
            settings = TiltSettings(path)
            d = settings.as_dict()
            self.assertEqual(d["mode"], ControlMode.GAMEPAD.value)
            self.assertIn("deadzone", d)
            self.assertIn("smoothing_alpha", d)
            self.assertIn("tilt_gain_lx", d)
            self.assertIn("tilt_gain_ly", d)


class TiltAdapterTests(unittest.TestCase):
    """Direct tests for tilt_control.adapter (mocked device)."""

    def test_adapter_initializes_with_settings(self):
        """TiltAdapter accepts and uses TiltSettings."""
        import tempfile
        from tilt_control.adapter import TiltAdapter
        from tilt_control.settings import TiltSettings, ControlMode
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tilt.json"
            settings = TiltSettings(path)
            adapter = TiltAdapter(settings=settings, device_path="/dev/null")
            self.assertEqual(adapter.mode, ControlMode.GAMEPAD)
            # Default mode is GAMEPAD, so not in TILT mode
            self.assertNotEqual(adapter.mode, ControlMode.TILT)

    def test_adapter_health_report(self):
        """TiltAdapter.health() returns status dict."""
        import tempfile
        from tilt_control.adapter import TiltAdapter
        from tilt_control.settings import TiltSettings
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tilt.json"
            settings = TiltSettings(path)
            adapter = TiltAdapter(settings=settings, device_path="/dev/null")
            h = adapter.health()
            self.assertIn("mode", h)
            self.assertIn("device_open", h)
            self.assertIn("calibrated", h)
            self.assertIn("lx", h)
            self.assertIn("ly", h)

    def test_adapter_axes_initial_zero(self):
        """TiltAdapter.axes starts at zero."""
        import tempfile
        from tilt_control.adapter import TiltAdapter
        from tilt_control.settings import TiltSettings
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tilt.json"
            settings = TiltSettings(path)
            adapter = TiltAdapter(settings=settings, device_path="/dev/null")
            axes = adapter.axes
            self.assertEqual(axes["LX"], 0.0)
            self.assertEqual(axes["LY"], 0.0)

    def test_adapter_select_mode_persists(self):
        """select_mode persists to settings."""
        import tempfile
        from tilt_control.adapter import TiltAdapter
        from tilt_control.settings import TiltSettings, ControlMode
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tilt.json"
            settings = TiltSettings(path)
            adapter = TiltAdapter(settings=settings, device_path="/dev/null")
            adapter.select_mode(ControlMode.TILT)
            self.assertEqual(adapter.mode, ControlMode.TILT)
            # Verify persistence
            loaded = TiltSettings(path)
            self.assertEqual(loaded.mode, ControlMode.TILT)

    def test_adapter_close_resets_state(self):
        """close() resets axes and marks device closed."""
        import tempfile
        from tilt_control.adapter import TiltAdapter
        from tilt_control.settings import TiltSettings
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tilt.json"
            settings = TiltSettings(path)
            adapter = TiltAdapter(settings=settings, device_path="/dev/null")
            adapter._lx = 0.5
            adapter._ly = 0.3
            adapter._open = True
            adapter.close()
            self.assertFalse(adapter._open)
            self.assertEqual(adapter._lx, 0.0)
            self.assertEqual(adapter._ly, 0.0)


class BridgeStickPassthroughTests(unittest.TestCase):
    """Analog-only gate: sticks flow unless Tilt Drive is selected.

    Armed means persisted TILT mode, independent of IMU ownership
    (fail-closed): an unowned sensor never re-enables the sticks; only an
    explicit Gamepad selection restores them. An armed gate drops LX/LY
    only — triggers/bumpers/buttons always flow so Tilt Drive disables
    analog turning/pitch, never buttons.
    """

    def _router(self, temp, owned=False):
        launcher = mock.Mock()
        launcher.run_dir = Path(temp)
        destination = io.BytesIO()
        router = runner.InputRouter(launcher, destination)
        router._tilt_adapter = mock.Mock()
        router._tilt_adapter.mode = runner.ControlMode.TILT
        router._tilt_adapter.controlled_axes = frozenset(("LX", "LY"))
        router._tilt_adapter._open = owned
        return router, destination

    def test_forward_line_drops_lx_when_sensor_unowned(self):
        """Persisted Tilt Drive gates LX even with no owned sensor (fail-closed)."""
        with tempfile.TemporaryDirectory() as temp:
            router, destination = self._router(temp)
            router._forward_line(b'{"type":"axis","axis":"LX","value":0.5}\n')
            self.assertNotIn(b'"axis":"LX"', destination.getvalue())
            router.close()

    def test_forward_line_drops_ly_when_sensor_unowned(self):
        """_forward_line drops LY in Tilt Drive without an owned sensor."""
        with tempfile.TemporaryDirectory() as temp:
            router, destination = self._router(temp)
            router._forward_line(b'{"type":"axis","axis":"LY","value":0.3}\n')
            self.assertNotIn(b'"axis":"LY"', destination.getvalue())
            router.close()

    def test_forward_line_passes_lx_in_gamepad_mode(self):
        """_forward_line passes LX axis when tilt_mode is False."""
        with tempfile.TemporaryDirectory() as temp:
            launcher = mock.Mock()
            launcher.run_dir = Path(temp)
            destination = io.BytesIO()
            router = runner.InputRouter(launcher, destination)
            router._tilt_adapter = mock.Mock()
            router._tilt_adapter.mode = runner.ControlMode.GAMEPAD
            router._tilt_adapter.controlled_axes = frozenset()
            router._forward_line(b'{"type":"axis","axis":"LX","value":0.5}\n')
            self.assertIn(b'"axis":"LX"', destination.getvalue())
            router.close()

    def test_side_channel_drops_lx_when_sensor_unowned(self):
        """Side-channel LX is dropped in Tilt Drive without an owned sensor."""
        with tempfile.TemporaryDirectory() as temp:
            router, destination = self._router(temp)
            event = dict(type='axis', axis='LX', value=.5, t_ms=1)
            self.assertFalse(router._handle_side_channel_event(event))
            self.assertNotIn(b'"axis":"LX"', destination.getvalue())
            router.close()

    def test_owned_tilt_drops_lx_ly_but_spares_buttons(self):
        """Armed gate (persisted Tilt Drive): LX/LY drop, buttons flow."""
        with tempfile.TemporaryDirectory() as temp:
            router, destination = self._router(temp, owned=True)
            router._forward_line(b'{"type":"axis","axis":"LX","value":0.5}\n')
            router._forward_line(b'{"type":"axis","axis":"LY","value":0.3}\n')
            self.assertNotIn(b'"axis":"LX"', destination.getvalue())
            self.assertNotIn(b'"axis":"LY"', destination.getvalue())
            router._forward_line(b'{"type":"button","key":"RB","action":"down"}\n')
            router._forward_line(b'{"type":"axis","axis":"RT","value":0.9}\n')
            router._forward_line(b'{"type":"button","key":"START","action":"down"}\n')
            out = destination.getvalue()
            self.assertIn(b'"key":"RB"', out)
            self.assertIn(b'"axis":"RT"', out)
            self.assertIn(b'"key":"START"', out)
            router.close()


class SettingsUiSyncTests(unittest.TestCase):
    """Tests for settings_ui tilt_control sync bridge."""

    def test_legacy_mixed_settings_fall_back_to_both_joystick_axes(self):
        """Legacy mixed input choices preserve tuning but normalize to joystick."""
        import tempfile
        from navigation_v2.settings_ui import LauncherSettings, SteeringMode
        from tilt_control.settings import TiltSettings, ControlMode
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tilt.json"
            settings = LauncherSettings(
                steering_mode=SteeringMode.TILT,
                tilt_sensitivity=1.5,
                tilt_deadzone=0.15,
                tilt_smoothing=0.85,
            )
            result = settings.sync_to_tilt_settings(path)
            self.assertTrue(result)
            ts = TiltSettings(path)
            self.assertEqual(ts.mode, ControlMode.GAMEPAD)
            self.assertAlmostEqual(ts.tilt_gain_lx, 1.5)
            self.assertAlmostEqual(ts.tilt_gain_ly, 1.5)
            self.assertAlmostEqual(ts.deadzone, 0.15)
            self.assertAlmostEqual(ts.smoothing_alpha, 0.85)

    def test_sync_to_tilt_settings_gamepad_mode(self):
        """sync_to_tilt_settings with GAMEPAD mode sets tilt to GAMEPAD."""
        import tempfile
        from navigation_v2.settings_ui import LauncherSettings, SteeringMode
        from tilt_control.settings import TiltSettings, ControlMode
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tilt.json"
            settings = LauncherSettings(steering_mode=SteeringMode.GAMEPAD)
            result = settings.sync_to_tilt_settings(path)
            self.assertTrue(result)
            ts = TiltSettings(path)
            self.assertEqual(ts.mode, ControlMode.GAMEPAD)

    def test_from_tilt_settings(self):
        """LauncherSettings.from_tilt_settings loads from TiltSettings."""
        import tempfile
        from navigation_v2.settings_ui import LauncherSettings, SteeringMode
        from tilt_control.settings import TiltSettings, ControlMode
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tilt.json"
            ts = TiltSettings(path)
            ts.update(
                mode=ControlMode.TILT,
                tilt_gain_lx=1.25,
                deadzone=0.20,
                smoothing_alpha=0.90,
            )
            loaded = LauncherSettings.from_tilt_settings(path)
            self.assertEqual(loaded.steering_mode, SteeringMode.TILT)
            self.assertAlmostEqual(loaded.tilt_sensitivity, 1.25)
            self.assertAlmostEqual(loaded.tilt_deadzone, 0.20)
            self.assertAlmostEqual(loaded.tilt_smoothing, 0.90)

    def test_settings_controller_deadzone_adjust(self):
        """SettingsController adjusts deadzone via adjust_right/left."""
        from navigation_v2.settings_ui import (
            LauncherSettings, SettingsController
        )
        settings = LauncherSettings()
        ctrl = SettingsController(settings)
        ctrl.activate()
        ctrl._cursor = 3  # deadzone item
        ctrl.adjust_right()  # 0.10 -> 0.15
        self.assertAlmostEqual(settings.tilt_deadzone, 0.15)
        ctrl.adjust_left()   # 0.15 -> 0.10
        self.assertAlmostEqual(settings.tilt_deadzone, 0.10)

    def test_settings_controller_smoothing_adjust(self):
        """SettingsController adjusts smoothing via adjust_right/left."""
        from navigation_v2.settings_ui import (
            LauncherSettings, SettingsController
        )
        settings = LauncherSettings()
        ctrl = SettingsController(settings)
        ctrl.activate()
        ctrl._cursor = 4  # smoothing item
        ctrl.adjust_right()  # 0.92 -> 0.95
        self.assertAlmostEqual(settings.tilt_smoothing, 0.95)
        ctrl.adjust_left()   # 0.95 -> 0.92
        self.assertAlmostEqual(settings.tilt_smoothing, 0.92)

    def test_settings_controller_render_state_includes_new_items(self):
        """render_state includes deadzone and smoothing items."""
        from navigation_v2.settings_ui import LauncherSettings, SettingsController
        settings = LauncherSettings()
        ctrl = SettingsController(settings)
        ctrl.activate()
        state = ctrl.render_state()
        keys = [item["key"] for item in state]
        self.assertIn("deadzone", keys)
        self.assertIn("smoothing", keys)
        self.assertEqual(len(state), 7)  # 7 items total


if __name__ == "__main__":
    unittest.main()
