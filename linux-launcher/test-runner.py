#!/usr/bin/env python3
"""Behavioral unit tests for the Linux runner's state and input boundaries."""

import io
import json
import os
import selectors
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import deck_pad
import runner
from qt_settings import seed_compatibility_warning_suppression


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

    def test_a_persisted_tilt_mode_is_migrated_to_gamepad(self):
        """An older lane's tilt mode cannot arm a mode whose display flips."""
        from tilt_control.settings import ControlMode, TiltSettings
        router, tmpdir = self._make_router()
        try:
            settings_path = str(Path(tmpdir) / "tilt-settings.json")
            persisted = TiltSettings(settings_path)
            persisted.mode = ControlMode.TILT
            self.assertTrue(router.start_tilt(settings_path))
            self.assertEqual(router._tilt_adapter.mode, ControlMode.GAMEPAD)
            self.assertEqual(TiltSettings(settings_path).mode, ControlMode.GAMEPAD)
            rows = [call.args[0] for call in router.launcher.log.call_args_list if call.args]
            self.assertIn("stage=tilt-drive-disabled", rows)
        finally:
            router.close()

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

    def test_mirror_parks_guest_sensor_in_gamepad_mode(self):
        """Owned IMU in Gamepad mode parks the guest sensor, zero virtual axes."""
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
                with mock.patch.object(adapter, 'consume_motion') as consume, \
                     mock.patch.object(adapter, 'sensor_acceleration') as live_vector:
                    router._mirror_native_sensor()
                consume.assert_not_called()
                live_vector.assert_not_called()
                self.assertEqual(transport.pushes, [(runner.NEUTRAL_ACCELERATION, True)])
                self.assertEqual(router._native_state, "live")
        finally:
            router.close()
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_tilt_drive_still_streams_the_live_imu(self):
        """Tilt Drive mirrors the live Deck vector instead of the parked pose."""
        from tilt_control.settings import ControlMode
        router, tmpdir = self._make_router()
        try:
            settings_path = str(Path(tmpdir) / "tilt-settings.json")
            router.start_tilt(settings_path)
            adapter = router._tilt_adapter
            if adapter is not None:
                adapter.select_mode(ControlMode.TILT)
                adapter._open = True
                adapter._estimator._last_ts = __import__('time').monotonic()
                transport = _FakeSensorTransport()
                router._sensor_transport = transport
                with mock.patch.object(adapter, 'consume_motion') as consume, \
                     mock.patch.object(adapter, 'sensor_acceleration', return_value=(1.0, 2.0, 3.0)):
                    router._mirror_native_sensor()
                consume.assert_not_called()
                self.assertEqual(transport.pushes, [((1.0, 2.0, 3.0), False)])
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
    def test_window_guard_starts_for_any_visible_x11_session(self):
        for desktop, headless, expected in (("Hyprland", False, True),
                                             ("gamescope", False, True),
                                             ("Hyprland", True, False)):
            with self.subTest(desktop=desktop, headless=headless):
                launcher = object.__new__(runner.Launcher)
                launcher.args = mock.Mock(headless=headless)
                launcher.emulator = mock.Mock()
                launcher.emulator.process.pid = 123
                launcher.run_dir = Path("/tmp/unused-jcs2-unit-test")
                launcher.spawn = mock.Mock()
                launcher.window_guard = None
                with mock.patch.dict(os.environ,
                                     {"XDG_CURRENT_DESKTOP": desktop, "DISPLAY": ":0"},
                                     clear=True):
                    launcher.start_gamescope_window_guard()
                self.assertEqual(launcher.spawn.called, expected)
                if expected:
                    argv = launcher.spawn.call_args.args[1]
                    self.assertEqual(argv[argv.index("--pid") + 1], "123")
                    self.assertIn(str(launcher.run_dir / "gamescope-window-ready.json"), argv)
    def test_warning_seed_is_per_avd_at_qsettings_root(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "Emulator.conf"
            config.write_text("[set]\nshowCompatibilityWarning=false\nclipboardSharing=true\n",
                              encoding="utf-8")
            path, key, changed = seed_compatibility_warning_suppression(
                "jcs2-fresh", config)
            self.assertEqual(path, config)
            self.assertEqual(key, "showCompatibilityWarning_jcs2-fresh")
            self.assertTrue(changed)
            text = config.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("showCompatibilityWarning_jcs2-fresh=false\n[set]\n"))
            self.assertIn("showCompatibilityWarning=false\nclipboardSharing=true\n", text)
            self.assertFalse(seed_compatibility_warning_suppression(
                "jcs2-fresh", config)[2])

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

    def test_every_bridge_button_is_mapped_or_dropped_by_the_runner(self):
        # An unmapped name aborts the controller's live replay, which takes the
        # whole lane down (2026-09-18: the controller exited with "invalid
        # live button VIEW" ~40 s after start), so the bridge's vocabulary
        # must stay covered by either
        # controller/mapping.json or the runner's own drop list.
        import joystick_bridge
        mapping = json.loads((Path(__file__).resolve().parents[1]
                              / "controller/mapping.json").read_text())
        mapped = set(mapping["buttons"])
        for name in sorted(set(joystick_bridge.BUTTON_CODES.values())):
            with self.subTest(button=name):
                self.assertTrue(name in mapped or name in runner.UNASSIGNED_BUTTONS,
                                f"{name} would kill the controller's live replay")

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


class _FakePadCommands:
    """Records pad-ownership commands and mirrors their real side effects.

    Stopping the mapper unit runs its ExecStopPost, which switches the firmware
    back to its own mouse/keyboard mode ('Y'); ``deck-lizard-mode off`` is what
    turns it off again.  Both effects are reproduced so the tests pin the ORDER
    the lane depends on, not merely the argv list.
    """

    def __init__(self, node: Path, active: bool = True, readable: bool = True):
        self.node = node
        self.active = active
        self.readable = readable
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            reported = ("active" if self.active else "inactive") if self.readable else ""
            return subprocess.CompletedProcess(argv, 0, f"{reported}\n", "")
        if argv[:3] == ["systemctl", "--user", "stop"]:
            self.active = False
            self.node.write_text("Y\n", encoding="utf-8")
        elif argv[:3] == ["systemctl", "--user", "start"]:
            self.active = True
            self.node.write_text("N\n", encoding="utf-8")
        elif argv[0] == "sudo":
            self.node.write_text("N\n" if argv[-1] == "off" else "Y\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")


class _FailingStop(_FakePadCommands):
    """The mapper cannot be stopped (systemd refuses)."""

    def __call__(self, argv, **kwargs):
        if list(argv)[:3] == ["systemctl", "--user", "stop"]:
            self.calls.append(list(argv))  # the attempt happened, and it failed
            raise subprocess.CalledProcessError(1, argv, "", "Failed to stop unit")
        return super().__call__(argv, **kwargs)


class DeckPadOwnershipTests(unittest.TestCase):
    """Desktop Mode gives the pad to deck-input-mapper; the lane must claim it."""

    def _node(self, value: str) -> Path:
        directory = Path(tempfile.mkdtemp())
        node = directory / "lizard_mode"
        node.write_text(f"{value}\n", encoding="utf-8")
        return node

    def _launcher(self) -> runner.Launcher:
        launcher = object.__new__(runner.Launcher)
        launcher.run_dir = Path(tempfile.mkdtemp())
        launcher.args = mock.Mock(input_mode="joystick")
        launcher._deck_pad_state = None
        return launcher

    def _patched(self, node: Path, fake):
        """Run one launcher call with the pad seams faked."""
        return mock.patch.multiple(deck_pad, _run=fake, LIZARD_MODE_NODE=node)

    def _rows(self, launcher: runner.Launcher) -> list[dict]:
        text = (launcher.run_dir / "launcher.ndjson").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def test_claim_stops_the_mapper_then_forces_pad_mode(self):
        """The hijack is broken in the one order that works on this Deck."""
        node = self._node("N")
        fake = _FakePadCommands(node)
        launcher = self._launcher()
        with self._patched(node, fake):
            launcher.claim_deck_pad()
        self.assertEqual(fake.calls, [
            ["systemctl", "--user", "is-active", deck_pad.MAPPER_UNIT],
            ["systemctl", "--user", "stop", deck_pad.MAPPER_UNIT],
            ["sudo", "-n", deck_pad.LIZARD_MODE_TOOL, "off"],
        ])
        self.assertEqual(node.read_text(encoding="utf-8").strip(), "N")
        marker = json.loads((launcher.run_dir / "deck-pad-ownership.json").read_text(encoding="utf-8"))
        self.assertTrue(marker["mapper_stopped"])
        self.assertEqual(marker["lizard_before"], "N")
        self.assertEqual(marker["lizard_forced"], "Y")   # ExecStopPost flipped it on
        self.assertEqual(marker["lizard_after"], "N")    # and the lane turned it off
        self.assertEqual(launcher._deck_pad_state, marker)
        row = self._rows(launcher)[-1]
        self.assertEqual(row["message"], "stage=deck-pad-claimed")
        self.assertEqual(row["lizard_after"], "N")

    def test_cleanup_returns_the_pad_it_claimed(self):
        """A lane that stopped the mapper restarts it and touches nothing else."""
        node = self._node("N")
        fake = _FakePadCommands(node)
        launcher = self._launcher()
        launcher.router = None
        launcher.controller_input = None
        launcher.controller = None
        launcher.children = []
        launcher._profile_lock = None
        launcher._endpoint_lock = None
        with self._patched(node, fake):
            launcher.claim_deck_pad()
            fake.calls.clear()
            launcher.cleanup()
        self.assertEqual(fake.calls, [["systemctl", "--user", "start", deck_pad.MAPPER_UNIT]])
        self.assertIsNone(launcher._deck_pad_state)
        self.assertEqual(self._rows(launcher)[-2]["message"], "stage=deck-pad-released")
        self.assertTrue(self._rows(launcher)[-2]["mapper_restarted"])
        self.assertTrue(fake.active)

    def test_firmware_only_hijack_is_restored_without_touching_the_mapper(self):
        """With no mapper running, only lizard mode was changed - so only it is put back."""
        node = self._node("Y")
        fake = _FakePadCommands(node, active=False)
        state = deck_pad.claim_pad(run=fake, node=node)
        self.assertEqual(fake.calls, [
            ["systemctl", "--user", "is-active", deck_pad.MAPPER_UNIT],
            ["sudo", "-n", deck_pad.LIZARD_MODE_TOOL, "off"],
        ])
        self.assertFalse(state["mapper_stopped"])
        self.assertEqual(state["lizard_forced"], "Y")
        fake.calls.clear()
        self.assertEqual(deck_pad.release_pad(state, run=fake),
                         {"mapper_restarted": False, "lizard_restored": "Y"})
        self.assertEqual(fake.calls, [["sudo", "-n", deck_pad.LIZARD_MODE_TOOL, "on"]])
        self.assertEqual(node.read_text(encoding="utf-8").strip(), "Y")

    def test_already_claimed_pad_costs_no_command(self):
        """A lane starting with no mapper and pad-mode firmware changes nothing."""
        node = self._node("N")
        fake = _FakePadCommands(node, active=False)
        state = deck_pad.claim_pad(run=fake, node=node)
        self.assertEqual(fake.calls, [["systemctl", "--user", "is-active", deck_pad.MAPPER_UNIT]])
        self.assertEqual(deck_pad.release_pad(state, run=fake), 
                         {"mapper_restarted": False, "lizard_restored": ""})
        self.assertEqual(len(fake.calls), 1)

    def test_unreadable_mapper_state_leaves_the_pad_alone(self):
        """Unknown ownership is never guessed at: the lane launches blind instead."""
        node = self._node("N")
        fake = _FakePadCommands(node, readable=False)
        launcher = self._launcher()
        with self._patched(node, fake):
            launcher.claim_deck_pad()
        self.assertEqual(fake.calls, [["systemctl", "--user", "is-active", deck_pad.MAPPER_UNIT]])
        self.assertIsNone(launcher._deck_pad_state)
        self.assertFalse((launcher.run_dir / "deck-pad-ownership.json").exists())
        row = self._rows(launcher)[-1]
        self.assertEqual(row["message"], "stage=deck-pad-claim-skipped")
        self.assertIn("deck-input-mapper.service", row["error"])

    def test_failed_mapper_stop_is_logged_not_fatal(self):
        """A systemd refusal must not stop a lane; the bridge then reports the stall."""
        node = self._node("N")
        fake = _FailingStop(node)
        launcher = self._launcher()
        with self._patched(node, fake):
            launcher.claim_deck_pad()
        self.assertEqual(fake.calls, [
            ["systemctl", "--user", "is-active", deck_pad.MAPPER_UNIT],
            ["systemctl", "--user", "stop", deck_pad.MAPPER_UNIT],
        ])
        self.assertIsNone(launcher._deck_pad_state)
        self.assertEqual(self._rows(launcher)[-1]["message"], "stage=deck-pad-claim-skipped")

    def test_qa_socket_lane_leaves_the_pad_with_the_desktop(self):
        """Only a lane that reads js0 may take the pad from the desktop mapper."""
        node = self._node("N")
        fake = _FakePadCommands(node)
        launcher = self._launcher()
        launcher.args = mock.Mock(input_mode="qa-socket")
        with self._patched(node, fake):
            launcher.claim_deck_pad()
        self.assertEqual(fake.calls, [])
        self.assertIsNone(launcher._deck_pad_state)
        row = self._rows(launcher)[-1]
        self.assertEqual(row["message"], "stage=deck-pad-claim-skipped")
        self.assertIn("qa-socket", row["reason"])


class DisplayGuardTests(unittest.TestCase):
    """The rotation guard corrects one flip, then stays quiet (user directive)."""

    PARK = runner.format_sensor_set(runner.NEUTRAL_ACCELERATION).decode().strip()

    def _launcher(self, rotation: int) -> runner.Launcher:
        launcher = object.__new__(runner.Launcher)
        launcher.args = mock.Mock(headless=False)
        launcher.log = mock.Mock()
        launcher.rotates: list[str] = []
        launcher.console_send = launcher.rotates.append
        launcher.guest_display_rotation = lambda: rotation
        launcher._aligned_guest_rotation = 1
        launcher._guard_last_rotate_at = None
        launcher._guard_rotated_for = None
        launcher._guard_reported_unaligned = None
        launcher._guard_quiet_until = 0.0
        launcher._guest_rotation_locked = False
        return launcher

    def test_the_guard_leaves_a_pinned_quarter_alone(self):
        launcher = self._launcher(3)
        launcher._guest_rotation_locked = True
        launcher.guard_display_orientation()
        self.assertEqual(launcher.rotates, [])

    def test_a_flip_is_corrected_once_and_the_sensor_is_re_parked(self):
        launcher = self._launcher(3)
        launcher.guard_display_orientation()
        self.assertEqual(launcher.rotates, ["rotate", "rotate", self.PARK])
        self.assertEqual(launcher._aligned_guest_rotation, 3)
        self.assertEqual(launcher.log.call_args_list[-1].args[0], "display-orientation-repaired")

    def test_a_flap_cannot_rotate_again_inside_the_cooldown(self):
        launcher = self._launcher(3)
        launcher.guard_display_orientation()
        fired = list(launcher.rotates)
        launcher._guard_quiet_until = 0.0                     # settle window passed
        launcher.guest_display_rotation = lambda: 1           # guest flaps back
        launcher.guard_display_orientation()
        self.assertEqual(launcher.rotates, fired)             # five-minute cooldown holds

    def test_the_settle_window_suppresses_the_very_next_tick(self):
        launcher = self._launcher(3)
        launcher.guard_display_orientation()
        fired = list(launcher.rotates)
        launcher.guest_display_rotation = lambda: 1
        launcher.guard_display_orientation()                  # still settling
        self.assertEqual(launcher.rotates, fired)

    def test_a_skipped_alignment_still_pins_the_quarter_the_guest_settled_on(self):
        launcher = object.__new__(runner.Launcher)
        launcher.args = mock.Mock(headless=False)
        launcher.log = mock.Mock()
        launcher.run_dir = Path(tempfile.mkdtemp())  # no persisted display pin here
        launcher._aligned_guest_rotation = None
        launcher.hyprctl_clients = mock.Mock(return_value=[])  # desktop session
        launcher.wait_for_guest_display_rotation = mock.Mock(return_value=3)
        launcher.provoke_guest_display_rotation = mock.Mock(return_value=None)
        launcher.lock_guest_display_rotation = mock.Mock(return_value=True)
        with mock.patch.object(runner, "hyprland_signature_candidates", return_value=["sig"]):
            launcher.align_visible_emulator()
        launcher.lock_guest_display_rotation.assert_called_once_with(3)
        self.assertEqual(launcher.log.call_args_list[-1].args[0], "display-alignment-skipped")
        self.assertIsNone(launcher._aligned_guest_rotation)

    def test_children_never_inherit_steams_gamescope_wsi_layer(self):
        """Steam sets ENABLE_GAMESCOPE_WSI; that layer kills the emulator's boot.

        Evidence: a Steam-launched lane in Gaming Mode died with "emulator exited
        during boot" after emulator.log showed the Gamescope WSI layer taking
        over qemu-system-x86_64, while the same lane started by hand boots fine.
        """
        launcher = object.__new__(runner.Launcher)
        with mock.patch.dict(os.environ, {"ENABLE_GAMESCOPE_WSI": "1"}):
            env = launcher.environment()
        self.assertEqual(env["ENABLE_GAMESCOPE_WSI"], "0")
        self.assertEqual(env["ADB_SERVER_PORT"], str(runner.ADB_PORT))

    def test_gpu_mode_follows_the_render_node_and_honours_the_override(self):
        """`-gpu host` needs a usable render node, not a queryable compositor.

        The Hyprland probe answers whether the lane can measure the host window;
        tying the renderer to it pushed every session without Hyprland (gamescope,
        stock SteamOS desktops) onto software rendering even though the GPU was
        there - measured 2026-09-20: software 22.5 fps menus / 29.07-29.59 fps
        race versus 59.14 fps for host-GPU menus.
        """
        launcher = object.__new__(runner.Launcher)
        launcher.log = mock.Mock()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JCS2_GPU", None)
            launcher.host_gpu_capable = mock.Mock(return_value=True)
            self.assertEqual(launcher.gpu_mode(), "host")
            os.environ["JCS2_GPU"] = "swiftshader_indirect"
            self.assertEqual(launcher.gpu_mode(), "swiftshader_indirect")
            del os.environ["JCS2_GPU"]
            launcher.host_gpu_capable = mock.Mock(return_value=False)
            self.assertEqual(launcher.gpu_mode(), "swiftshader_indirect")

    def test_render_node_probe_decides_and_reports_the_reason(self):
        """A usable render node selects the host; no node means software.

        Capability, not session: a machine with no usable /dev/dri/renderD* node
        still logs why it fell back, so the lane log explains the renderer
        without anyone having to infer it from frame rates.
        """
        launcher = object.__new__(runner.Launcher)
        launcher.log = mock.Mock()
        node = Path(tempfile.mkdtemp()) / "renderD128"
        node.write_bytes(b"")
        with mock.patch.object(runner, "DRM_DIR", node.parent), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JCS2_GPU", None)
            self.assertTrue(launcher.host_gpu_capable())
            self.assertEqual(launcher.gpu_mode_decision()["mode"], "host")
            empty = Path(tempfile.mkdtemp())
            launcher._host_gpu = None  # the probe is cached per session
            with mock.patch.object(runner, "DRM_DIR", empty):
                self.assertFalse(launcher.host_gpu_capable())
                decision = launcher.gpu_mode_decision()
            self.assertEqual(decision["mode"], "swiftshader_indirect")
            self.assertIn("render node", decision["reason"])

    def test_no_desktop_compositor_never_provokes_a_guest_rotation(self):
        """Gaming Mode: gamescope owns the output, so the host is left alone.

        Provoking a rotation there restarts the game's SENSOR_LANDSCAPE
        activity and drops it out of the foreground (2026-09-19), so the align
        stage must lock the parked quarter instead of measuring or provoking.
        """
        launcher = object.__new__(runner.Launcher)
        launcher.args = mock.Mock(headless=False)
        launcher.log = mock.Mock()
        launcher.run_dir = Path(tempfile.mkdtemp())
        launcher._aligned_guest_rotation = None
        launcher.hyprctl_clients = mock.Mock(return_value=None)  # no Hyprland
        launcher.wait_for_guest_display_rotation = mock.Mock(
            side_effect=AssertionError("must not measure without a compositor"))
        launcher.provoke_guest_display_rotation = mock.Mock(
            side_effect=AssertionError("must not provoke without a compositor"))
        launcher.lock_guest_display_rotation = mock.Mock(return_value=True)
        with mock.patch.object(runner, "hyprland_signature_candidates", return_value=["sig"]):
            launcher.align_visible_emulator()
        launcher.lock_guest_display_rotation.assert_called_once_with(runner.PARKED_GUEST_ROTATION)
        self.assertEqual(launcher.log.call_args_list[-1].args[0], "display-alignment-skipped")

    def test_a_never_aligned_lane_never_probes_or_rotates(self):
        launcher = self._launcher(3)
        launcher._aligned_guest_rotation = None
        launcher.guest_display_rotation = mock.Mock(side_effect=AssertionError("must not read"))
        launcher.guard_display_orientation()
        launcher.guard_display_orientation()
        self.assertEqual(launcher.rotates, [])
        self.assertEqual([call.args[0] for call in launcher.log.call_args_list], [])


class GuestRotationLockTests(unittest.TestCase):
    """The guest display quarter is pinned so Tilt Drive cannot flip the window."""

    def _launcher(self, readback: str) -> tuple[runner.Launcher, list]:
        launcher = object.__new__(runner.Launcher)
        launcher.log = mock.Mock()
        launcher._guest_rotation_locked = False
        calls: list = []

        def fake_adb(*args, **kwargs):
            calls.append(args)
            result = mock.Mock()
            result.stdout = readback
            return result

        launcher.adb = fake_adb
        return launcher, calls

    def test_the_quarter_is_pinned_with_accelerometer_rotation_off(self):
        launcher, calls = self._launcher("1\n")
        self.assertTrue(launcher.lock_guest_display_rotation(1))
        self.assertEqual(calls, [
            ("shell", "settings", "put", "system", "accelerometer_rotation", "0"),
            ("shell", "settings", "put", "system", "user_rotation", "1"),
            ("shell", "settings", "get", "system", "user_rotation"),
        ])
        self.assertEqual(launcher.log.call_args_list[-1].args[0], "stage=guest-display-rotation-locked")
        self.assertTrue(launcher._guest_rotation_locked)

    def test_an_unconfirmed_pin_is_reported_and_not_claimed(self):
        launcher, _ = self._launcher("0\n")
        self.assertFalse(launcher.lock_guest_display_rotation(1))
        self.assertFalse(launcher._guest_rotation_locked)
        self.assertEqual(launcher.log.call_args_list[-1].args[0],
                         "guest-display-rotation-lock-unconfirmed")


class RenderVerifyTests(unittest.TestCase):
    """The align stage checks the picture it produced, not only its arithmetic."""

    @staticmethod
    def _textured(columns: int = 40, rows: int = 25, blank_band: bool = False):
        """A frame with real picture in it (uniform frames carry no signal)."""
        grid = []
        for row in range(rows):
            line = []
            for column in range(columns):
                value = 1.0 if (row * 3 + column) % 7 < 3 else 0.0
                inside_band = blank_band and column >= int(columns * 0.55)
                line.append((0.85, 0.85, 0.85) if inside_band else (value, value, value))
            grid.append(line)
        return grid

    def test_an_upright_frame_needs_no_rotation(self):
        guest = self._textured()
        offset, error = runner.render_offset(guest, guest)
        self.assertEqual(offset, 0)
        self.assertAlmostEqual(error, 0.0, places=6)

    def test_an_upside_down_frame_is_detected(self):
        guest = self._textured()
        offset, error = runner.render_offset(runner.rotate_grid(guest, 2), guest)
        self.assertEqual(offset, 2)
        self.assertAlmostEqual(error, 0.0, places=6)

    def test_a_portrait_strip_is_detected_as_not_upright(self):
        # A 90-degree window error scales the guest into a portrait strip with a
        # wide blank band; no rotation of the guest frame can match that.
        guest = self._textured()
        offset, error = runner.render_offset(self._textured(blank_band=True), guest)
        self.assertEqual(offset, 1)
        self.assertGreater(error, 0.25)

    def test_an_unrelated_frame_reports_a_large_error(self):
        guest = [[(0.0, 0.0, 0.0) for _ in range(40)] for _ in range(25)]
        host = [[(1.0, 1.0, 1.0) if (row + column) % 2 else (0.0, 0.0, 0.0)
                 for column in range(40)] for row in range(25)]
        error = runner.render_offset(host, guest)[1]
        self.assertGreater(error, 0.3)  # a mismatched pair must not read as a match

    def test_host_sampling_uses_only_the_emulator_window(self):
        # The deck screen has desktop around the window; sampling it whole made an
        # upright picture read as a 0.6 mismatch (measured 2026-09-18).
        row = bytes([0, 0, 0]) * 40 + bytes([255, 255, 255]) * 60  # black left, white right
        ppm = b"P6\n100 50\n255\n" + row * 50
        whole = runner.downsample_ppm(ppm, 5, 1)
        window = runner.downsample_ppm(ppm, 5, 1, (40, 0, 60, 50))
        # Whole screen: the dark desktop half and the bright window half both
        # show, so the comparison sees a mix that the window crop removes.
        self.assertEqual([round(pixel[0], 2) for pixel in whole[0]],
                         [0.0, 0.0, 1.0, 1.0, 1.0])
        # Window only: every cell is inside the bright block.
        self.assertEqual([round(pixel[0], 2) for pixel in window[0]], [1.0] * 5)
        # Sampling outside the image is clamped rather than raising.
        self.assertEqual(len(runner.downsample_ppm(ppm, 5, 1, (-50, -50, 500, 500))[0]), 5)

    def _verify_launcher(self, host_grid, offsets):
        """Launcher whose render comparison follows a fixed offset script."""
        launcher = object.__new__(runner.Launcher)
        launcher.args = mock.Mock(headless=False)
        launcher.log = mock.Mock()
        launcher.sent: list[str] = []
        launcher.console_send = launcher.sent.append
        launcher.unpinned: list[bool] = []
        launcher.unpin_guest_display_rotation = lambda: launcher.unpinned.append(True)
        launcher.host_render_grid = lambda: host_grid
        launcher.guest_render_grid = lambda: host_grid
        launcher.offsets = list(offsets)
        return launcher

    def _run_verify(self, launcher):
        script = launcher.offsets
        with mock.patch.object(runner, "render_offset",
                               lambda host, guest: script.pop(0) if script else (0, 0.0)), \
                mock.patch.object(runner, "RENDER_VERIFY_SETTLE_SECONDS", 0):
            return launcher.verify_visible_render()

    def test_verification_rotates_until_the_render_matches(self):
        # A strip that settles (two agreeing reads on a painted frame) is
        # recovered, and the loop stops the moment the picture measures upright.
        launcher = self._verify_launcher(self._textured(), [(1, 0.5)] * 2 + [(0, 0.02)] * 2)
        self.assertEqual(self._run_verify(launcher), 0)
        park = runner.format_sensor_set(runner.NEUTRAL_ACCELERATION).decode().strip()
        probe = runner.format_sensor_set(runner.RENDER_RECOVERY_ACCELERATION).decode().strip()
        self.assertEqual(launcher.sent, [probe, park])
        self.assertEqual(launcher.unpinned, [True])  # the sensor channel needs the pin released
        rows = [call.args[0] for call in launcher.log.call_args_list if call.args]
        self.assertEqual(rows[-1], "stage=display-render-verified")
        self.assertEqual(launcher._last_measured_offset, 0)

    def test_a_render_that_never_measures_upright_is_reported_not_claimed(self):
        launcher = self._verify_launcher(self._textured(), [(1, 0.6)] * 20)
        self.assertIsNone(self._run_verify(launcher))
        park = runner.format_sensor_set(runner.NEUTRAL_ACCELERATION).decode().strip()
        probe = runner.format_sensor_set(runner.RENDER_RECOVERY_ACCELERATION).decode().strip()
        # One bounded pass through the plan, then an honest failure row.
        self.assertEqual(launcher.sent, [probe, park, "rotate", park, probe, park])
        rows = [call.args[0] for call in launcher.log.call_args_list if call.args]
        self.assertEqual(rows[-1], "stage=display-render-unverified")
        self.assertEqual(launcher.log.call_args_list[-1].kwargs["last_offset"], 1)

    def test_a_window_that_has_not_painted_yet_is_not_acted_on(self):
        # Measured 2026-09-18: the align stage read band 1.0 then 0.6 seconds
        # after the game resumed and "recovered" a picture that was already fine.
        launcher = self._verify_launcher([[(0.5, 0.5, 0.5)]] * 25, [(1, 1.0)] * 20)
        launcher.args = mock.Mock(headless=False)
        self.assertIsNone(self._run_verify(launcher))
        self.assertEqual(launcher.sent, [])  # no rotate, no probe on a blank window
        self.assertEqual(launcher.unpinned, [])
        self.assertEqual(launcher.log.call_args_list[-1].args[0], "stage=display-render-unverified")
        self.assertIn("not presented", launcher.log.call_args_list[-1].kwargs["error"])

    def test_a_stale_compositor_signature_is_healed_from_the_runtime_dir(self):
        # A supervisor's pinned HYPRLAND_INSTANCE_SIGNATURE goes stale across a
        # reboot and every hyprctl call then fails (rc=4), which is what left the
        # window region unknown and made the align stage judge the desktop
        # against the guest frame (measured 2026-09-18).
        launcher = object.__new__(runner.Launcher)
        launcher.log = mock.Mock()
        launcher.hyprctl_clients = mock.Mock(side_effect=[
            None, [{"class": "Emulator", "at": [0, 0], "size": [1024, 640]}]])
        with mock.patch.dict(os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": "stale"}), \
                mock.patch.object(runner, "hyprland_signature_candidates",
                                  return_value=["stale", "live"]):
            self.assertEqual(launcher.hyprland_window_region(), (0, 0, 1024, 640))
        self.assertEqual(launcher.hyprctl_clients.call_args_list,
                         [mock.call("stale"), mock.call("live")])
        row = launcher.log.call_args_list[-1]
        self.assertEqual(row.args[0], "stage=hyprland-signature-healed")
        self.assertEqual(row.kwargs["signature"], "live")
        self.assertEqual(row.kwargs["configured"], "stale")

    def test_an_unresolvable_window_is_never_a_whole_screen_capture(self):
        launcher = object.__new__(runner.Launcher)
        launcher.log = mock.Mock()
        launcher.run_dir = Path(tempfile.mkdtemp())
        launcher.run = mock.Mock(return_value=mock.Mock(returncode=0))
        launcher.hyprctl_clients = mock.Mock(return_value=None)
        with mock.patch.object(runner, "hyprland_signature_candidates", return_value=["stale"]):
            self.assertIsNone(launcher.hyprland_window_region())
            (launcher.run_dir / "render-verify.ppm").write_bytes(b"P6\n2 1\n255\n" + b"\x00" * 6)
            # grim succeeded, but with no window crop there is no measurement.
            self.assertIsNone(launcher.host_render_grid())

    def test_compositor_candidates_put_the_configured_signature_first(self):
        runtime = Path(tempfile.mkdtemp())
        (runtime / "hypr" / "old").mkdir(parents=True)
        (runtime / "hypr" / "live").mkdir(parents=True)
        os.utime(runtime / "hypr" / "old", (1000, 1000))
        os.utime(runtime / "hypr" / "live", (2000, 2000))  # the running compositor
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime),
                                          "HYPRLAND_INSTANCE_SIGNATURE": "old"}):
            self.assertEqual(runner.hyprland_signature_candidates(), ["old", "live"])
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime / "absent"),
                                          "HYPRLAND_INSTANCE_SIGNATURE": ""}):
            self.assertEqual(runner.hyprland_signature_candidates(), [])

    def test_window_region_extraction_ignores_other_clients(self):
        clients = [{"class": "firefox", "at": [0, 0], "size": [800, 600]},
                   {"class": "Emulator", "at": [1, 2], "size": [1024, 640]},
                   {"class": "Emulator", "at": None, "size": [1, 1]}]
        self.assertEqual(runner.emulator_window_region(clients), (1, 2, 1024, 640))
        self.assertIsNone(runner.emulator_window_region([]))
        self.assertIsNone(runner.emulator_window_region(None))

    def test_verification_reports_when_it_cannot_see_the_screen(self):
        launcher = object.__new__(runner.Launcher)
        launcher.args = mock.Mock(headless=False)
        launcher.log = mock.Mock()
        launcher.host_render_grid = lambda: None
        launcher.guest_render_grid = lambda: None
        launcher.console_send = mock.Mock()
        self.assertIsNone(launcher.verify_visible_render())
        launcher.console_send.assert_not_called()
        self.assertEqual(launcher.log.call_args_list[-1].args[0],
                         "stage=display-render-unverified")


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
