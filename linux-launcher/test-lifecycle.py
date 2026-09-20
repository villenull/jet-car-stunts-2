#!/usr/bin/env python3
"""Offline lifecycle regressions: fake processes/signals, temporary file locks."""
import argparse
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import runner
from gamescope_window import Window

ENDPOINT_LOCK_PATH = runner.Launcher.endpoint_lock_path


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.avd_home = self.root / 'avds'
        (self.avd_home / 'test.avd').mkdir(parents=True)
        for patcher in (mock.patch.object(runner, 'AVD_HOME', self.avd_home),
                        mock.patch.object(runner, 'AVD', 'test'),
                        mock.patch.object(runner.Launcher, 'endpoint_lock_path', return_value=self.root / 'ports.lock'),
                        mock.patch.dict(runner.os.environ, {'JCS2_LOGDIR': str(self.root / 'logs')})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def launcher(self):
        launcher = runner.Launcher(argparse.Namespace())
        launcher.log = mock.Mock()
        return launcher

    def process(self, pid=321):
        return mock.Mock(pid=pid, stdin=io.BytesIO(), stdout=io.BytesIO(),
                         stderr=io.BytesIO(), returncode=0,
                         poll=mock.Mock(return_value=0), wait=mock.Mock(return_value=0))

    def test_group_is_captured_without_looking_up_exited_leader(self):
        process = self.process()
        with mock.patch.object(runner.os, 'getpgid', side_effect=ProcessLookupError):
            owned = runner.OwnedProcess('fake', process)
            self.assertEqual(owned.pgid, 321)

    def test_exited_leader_still_terminates_owned_descendants(self):
        launcher = self.launcher()
        process = self.process()
        with mock.patch.object(runner.os, 'killpg', side_effect=[None, ProcessLookupError]) as killpg:
            launcher.stop_owned(runner.OwnedProcess('fake', process))
        self.assertEqual(killpg.call_args_list, [mock.call(321, runner.signal.SIGTERM), mock.call(321, 0)])
        self.assertTrue(all(h.closed for h in (process.stdin, process.stdout, process.stderr)))

    def test_remaining_group_is_killed_after_bounded_term_grace(self):
        launcher = self.launcher()
        process = self.process()
        with mock.patch.object(runner.os, 'killpg') as killpg, \
             mock.patch.object(runner.time, 'monotonic', side_effect=[0, 4]):
            launcher.stop_owned(runner.OwnedProcess('fake', process))
        self.assertEqual(killpg.call_args_list, [mock.call(321, runner.signal.SIGTERM),
                                                mock.call(321, 0), mock.call(321, runner.signal.SIGKILL)])

    def test_failed_signal_still_closes_all_process_resources(self):
        launcher = self.launcher()
        process = self.process()
        log = io.StringIO()
        with mock.patch.object(runner.os, 'killpg', side_effect=PermissionError), self.assertRaises(PermissionError):
            launcher.stop_owned(runner.OwnedProcess('fake', process, (log,)))
        self.assertTrue(all(h.closed for h in (log, process.stdin, process.stdout, process.stderr)))

    def test_failed_resource_close_does_not_skip_other_handles(self):
        launcher = self.launcher()
        process = self.process()
        broken = mock.Mock(close=mock.Mock(side_effect=OSError('close failed')))
        with mock.patch.object(runner.os, 'killpg', side_effect=ProcessLookupError), \
             self.assertRaisesRegex(runner.LauncherError, 'resources'):
            launcher.stop_owned(runner.OwnedProcess('fake', process, (broken,)))
        self.assertTrue(all(h.closed for h in (process.stdin, process.stdout, process.stderr)))

    def test_spawn_failure_closes_both_logs(self):
        launcher = self.launcher()
        captured = []
        def fail(*_args, **kwargs):
            captured.extend((kwargs['stdout'], kwargs['stderr']))
            raise OSError('fake Popen failure')
        with mock.patch.object(runner.subprocess, 'Popen', side_effect=fail), self.assertRaises(OSError):
            launcher.spawn('fake', ['not-executed'], 'out.log', 'err.log')
        self.assertTrue(all(h.closed for h in captured))
        self.assertEqual(launcher.children, [])

    def test_stderr_open_failure_closes_already_open_stdout(self):
        launcher = self.launcher()
        handle = io.StringIO()
        with mock.patch.object(Path, 'open', side_effect=[handle, OSError('fake open failure')]), \
             mock.patch.object(runner.subprocess, 'Popen') as popen, self.assertRaises(OSError):
            launcher.spawn('fake', ['not-executed'], 'out.log', 'err.log')
        self.assertTrue(handle.closed)
        popen.assert_not_called()

    def test_same_profile_lock_excludes_second_launcher_and_can_be_reacquired(self):
        first, second = self.launcher(), self.launcher()
        first.acquire_profile_lock()
        try:
            with self.assertRaisesRegex(runner.LauncherError, 'already owned'):
                second.acquire_profile_lock()
            self.assertIsNone(second._profile_lock)
        finally:
            first.release_profile_lock()
        second.acquire_profile_lock()
        second.release_profile_lock()

    def test_lock_is_acquired_before_preflight_and_released_after_failure(self):
        first, second = self.launcher(), self.launcher()
        def preflight():
            with self.assertRaisesRegex(runner.LauncherError, 'already owned'):
                second.acquire_profile_lock()
            raise runner.LauncherError('fake preflight failure')
        first.preflight = preflight
        with self.assertRaisesRegex(runner.LauncherError, 'fake preflight'):
            first.main()
        first.cleanup()
        second.acquire_profile_lock()
        second.release_profile_lock()

    def test_cleanup_attempts_all_steps_and_releases_lock_despite_errors(self):
        first, second = self.launcher(), self.launcher()
        first.acquire_profile_lock()
        first.router = mock.Mock(close=mock.Mock(side_effect=OSError('router failed')))
        first.controller_input = io.BytesIO()
        first.children = [runner.OwnedProcess('one', self.process(123)), runner.OwnedProcess('two', self.process(456))]
        first.stop_owned = mock.Mock(side_effect=[OSError('child failed'), None])
        first.log.side_effect = OSError('disk failed')
        with self.assertRaisesRegex(runner.LauncherError, 'cleanup incomplete'):
            first.cleanup()
        self.assertEqual([c.args[0].name for c in first.stop_owned.call_args_list], ['two', 'one'])
        first.router.close.assert_called_once()
        self.assertTrue(first.controller_input.closed)
        second.acquire_profile_lock()
        second.release_profile_lock()

    def test_router_closes_remaining_resources_after_one_client_fails(self):
        router = runner.InputRouter(self.launcher(), io.BytesIO())
        broken = mock.Mock(close=mock.Mock(side_effect=OSError('client failed')))
        other = mock.Mock()
        router.clients = {broken: bytearray(), other: bytearray()}
        selector = router.selector
        with mock.patch.object(selector, 'close', wraps=selector.close) as close, \
             self.assertRaisesRegex(runner.LauncherError, 'input resources'):
            router.close()
        other.close.assert_called_once()
        close.assert_called_once()

    def test_bridge_is_owned_before_selector_setup_can_fail(self):
        launcher = self.launcher()
        router = runner.InputRouter(launcher, io.BytesIO())
        process = self.process()
        process.stdout = mock.Mock(fileno=mock.Mock(return_value=99))
        try:
            with mock.patch.object(runner, 'BridgeSideChannel', None), \
                 mock.patch.object(runner.subprocess, 'Popen', return_value=process), \
                 mock.patch.object(runner.os, 'set_blocking'), \
                 mock.patch.object(router.selector, 'register', side_effect=OSError('selector failed')), \
                 self.assertRaises(OSError):
                router.start_bridge(Path('not-executed'))
            self.assertEqual(len(launcher.children), 1)
            self.assertIs(launcher.children[0].process, process)
            self.assertIs(router.bridge, launcher.children[0])
        finally:
            router.close()
            for owned in launcher.children:
                for handle in owned.log_handles:
                    handle.close()

    def test_endpoint_lock_path_is_independent_of_session_environment(self):
        info = mock.Mock(st_mode=0o40700, st_uid=1234)
        with mock.patch.object(runner.os, 'getuid', return_value=1234), \
             mock.patch.object(Path, 'mkdir'), mock.patch.object(Path, 'lstat', return_value=info):
            with mock.patch.dict(runner.os.environ, {'XDG_RUNTIME_DIR': '/run/user/1234'}):
                desktop_path = ENDPOINT_LOCK_PATH()
            with mock.patch.dict(runner.os.environ, {}, clear=True):
                terminal_path = ENDPOINT_LOCK_PATH()
        self.assertEqual(desktop_path, terminal_path)
        self.assertEqual(desktop_path, Path('/tmp/jcs2-launcher-1234/ports-5038-5594-5595.lock'))

    def test_endpoint_lock_excludes_different_avd_and_releases_after_cleanup_error(self):
        first, second = self.launcher(), self.launcher()
        first.acquire_launch_locks()
        (self.avd_home / 'other.avd').mkdir()
        with mock.patch.object(runner, 'AVD', 'other'):
            with self.assertRaisesRegex(runner.LauncherError, 'ports already owned'):
                second.acquire_launch_locks()
            first.router = mock.Mock(close=mock.Mock(side_effect=OSError('fake cleanup failure')))
            with self.assertRaises(runner.LauncherError):
                first.cleanup()
            second.acquire_launch_locks()
            second.cleanup()

    def test_profile_lock_failure_releases_endpoint_lock(self):
        first, second = self.launcher(), self.launcher()
        first.acquire_profile_lock()
        with self.assertRaisesRegex(runner.LauncherError, 'AVD test already owned'):
            second.acquire_launch_locks()
        self.assertIsNone(second._endpoint_lock)
        first.release_profile_lock()
        second.acquire_launch_locks()
        second.cleanup()

    def test_stop_after_each_startup_stage_prevents_later_stages(self):
        stages = ('acquire_launch_locks', 'preflight', 'start_server', 'start_emulator',
                  'orient_visible_emulator', 'isolate_guest',
                  'verify_packages', 'start_controller', 'start_input', 'launch_game',
                  'verify_gamescope_window', 'align_visible_emulator')
        for index, cancelled_stage in enumerate(stages):
            with self.subTest(stage=cancelled_stage):
                launcher = self.launcher()
                for name in (*stages, 'run_until_stop'):
                    setattr(launcher, name, mock.Mock())
                getattr(launcher, cancelled_stage).side_effect = lambda: setattr(launcher, 'stop_requested', True)
                self.assertEqual(launcher.main(), 130)
                for name in stages[index + 1:]:
                    getattr(launcher, name).assert_not_called()
                launcher.run_until_stop.assert_not_called()

    def rotation_launcher(self):
        launcher = self.launcher()
        launcher.args = argparse.Namespace(headless=False)
        launcher.console_send = mock.Mock()
        return launcher

    def test_host_rotation_steps_keep_the_render_invariant(self):
        # The host window renders upright only while emulator offset +
        # guest display rotation == 0 (mod 4); each rotate advances the offset
        # by one, so the steps must preserve that relation across a change.
        for aligned_rotation in range(4):
            offset = (4 - aligned_rotation) % 4  # the pair that renders upright
            for moved_rotation in range(4):
                with self.subTest(aligned=aligned_rotation, moved=moved_rotation):
                    steps = runner.Launcher.host_rotation_steps(moved_rotation, aligned_rotation)
                    self.assertLess(steps, 4)
                    self.assertEqual((offset + steps + moved_rotation) % 4, 0)

    def test_align_provokes_one_quarter_turn_then_lets_the_picture_decide(self):
        launcher = self.rotation_launcher()
        launcher.wait_for_guest_display_rotation = mock.Mock(return_value=3)
        launcher.provoke_guest_display_rotation = mock.Mock(return_value=1)
        launcher.verify_visible_render = mock.Mock(return_value=0)
        launcher.guest_display_rotation = mock.Mock(return_value=1)
        launcher.lock_guest_display_rotation = mock.Mock(return_value=True)
        launcher.align_visible_emulator()
        # Guest moved 3 -> 1 (delta 2): two rotates are the candidate, and the
        # measured picture is what confirms (or corrects) them.
        self.assertEqual(launcher.console_send.call_args_list, [mock.call('rotate')] * 2)
        launcher.verify_visible_render.assert_called_once_with(1)
        self.assertEqual(launcher._aligned_guest_rotation, 1)
        launcher.lock_guest_display_rotation.assert_called_once_with(1)
        rows = [call.args[0] for call in launcher.log.call_args_list if call.args]
        self.assertIn('stage=display-orientation-candidate', rows)
        self.assertIn('stage=display-orientation-aligned', rows)

    def test_align_pins_the_quarter_the_corrections_settled_on(self):
        # Rotating the window also moves the guest quarter, so the pin must
        # follow the corrections, not the delta the candidate was built from.
        launcher = self.rotation_launcher()
        launcher.wait_for_guest_display_rotation = mock.Mock(return_value=3)
        launcher.provoke_guest_display_rotation = mock.Mock(return_value=1)
        launcher.verify_visible_render = mock.Mock(return_value=0)
        launcher.guest_display_rotation = mock.Mock(return_value=3)
        launcher.lock_guest_display_rotation = mock.Mock(return_value=True)
        launcher.align_visible_emulator()
        launcher.lock_guest_display_rotation.assert_called_once_with(3)
        self.assertEqual(launcher._aligned_guest_rotation, 3)

    def test_align_never_pins_a_render_it_measured_wrong(self):
        # The pin is what stops the accelerometer from moving the display, so
        # pinning a measured-wrong render is what made a strip permanent
        # (measured 2026-09-18): the align stage must leave it unpinned instead.
        launcher = self.rotation_launcher()
        launcher.wait_for_guest_display_rotation = mock.Mock(return_value=3)
        launcher.provoke_guest_display_rotation = mock.Mock(return_value=1)
        launcher.verify_visible_render = mock.Mock(return_value=None)
        launcher._last_measured_offset = 1
        launcher.guest_display_rotation = mock.Mock(return_value=1)
        launcher.lock_guest_display_rotation = mock.Mock(return_value=True)
        launcher.unpin_guest_display_rotation = mock.Mock()
        launcher.align_visible_emulator()
        launcher.lock_guest_display_rotation.assert_not_called()
        launcher.unpin_guest_display_rotation.assert_called_once_with()
        rows = [call.args[0] for call in launcher.log.call_args_list if call.args]
        self.assertIn('stage=display-orientation-unpinned', rows)

    def test_a_persisted_pin_is_held_without_any_probe_cycle(self):
        # The provocation cycle exists only to make the emulator re-lay-out its
        # window; a lane whose last render measured upright already has that
        # layout, so the hold re-applies the proven state and just measures it.
        launcher = self.rotation_launcher()
        launcher.load_display_pin = mock.Mock(return_value=1)
        launcher.lock_guest_display_rotation = mock.Mock(return_value=True)
        launcher.measure_settled_frame = mock.Mock(return_value=(0, 0.05))
        launcher.save_display_pin = mock.Mock()
        launcher.wait_for_guest_display_rotation = mock.Mock(side_effect=AssertionError("no probe cycle"))
        launcher.provoke_guest_display_rotation = mock.Mock(side_effect=AssertionError("no probe cycle"))
        with mock.patch.object(runner, 'RENDER_VERIFY_SETTLE_SECONDS', 0):
            launcher.align_visible_emulator()
        park = runner.format_sensor_set(runner.NEUTRAL_ACCELERATION).decode().strip()
        self.assertEqual(launcher.console_send.call_args_list, [mock.call(park)])
        launcher.lock_guest_display_rotation.assert_called_once_with(1)
        launcher.save_display_pin.assert_called_once_with(1)
        self.assertEqual(launcher._aligned_guest_rotation, 1)
        rows = [call.args[0] for call in launcher.log.call_args_list if call.args]
        self.assertIn('stage=display-pin-honoured', rows)

    def test_a_pin_that_loses_the_picture_mid_streak_is_not_honoured(self):
        # The window is still settling for the first seconds after the game
        # resumes, so an upright read is not a verdict on its own: the hold is
        # honoured only while every settled read stays upright.
        launcher = self.rotation_launcher()
        launcher.load_display_pin = mock.Mock(return_value=1)
        launcher.lock_guest_display_rotation = mock.Mock(return_value=True)
        launcher.measure_settled_frame = mock.Mock(side_effect=[(0, 0.05), (0, 0.05), (1, 0.6)])
        launcher.unpin_guest_display_rotation = mock.Mock()
        launcher.save_display_pin = mock.Mock()
        launcher.wait_for_guest_display_rotation = mock.Mock(return_value=1)
        launcher.provoke_guest_display_rotation = mock.Mock(return_value=3)
        launcher.verify_visible_render = mock.Mock(return_value=0)
        launcher.guest_display_rotation = mock.Mock(return_value=3)
        with mock.patch.object(runner, 'RENDER_VERIFY_SETTLE_SECONDS', 0):
            launcher.align_visible_emulator()
        launcher.unpin_guest_display_rotation.assert_called_once_with()
        launcher.save_display_pin.assert_called_once_with(3)
        self.assertEqual(launcher.measure_settled_frame.call_count, 3)
        rows = [call.args[0] for call in launcher.log.call_args_list if call.args]
        self.assertIn('stage=display-pin-missed', rows)

    def test_a_pin_that_does_not_measure_upright_runs_the_full_align(self):
        launcher = self.rotation_launcher()
        launcher.load_display_pin = mock.Mock(return_value=3)
        launcher.lock_guest_display_rotation = mock.Mock(return_value=True)
        launcher.measure_settled_frame = mock.Mock(return_value=(1, 0.6))
        launcher.unpin_guest_display_rotation = mock.Mock()
        launcher.save_display_pin = mock.Mock()
        launcher.wait_for_guest_display_rotation = mock.Mock(return_value=1)
        launcher.provoke_guest_display_rotation = mock.Mock(return_value=3)
        launcher.verify_visible_render = mock.Mock(return_value=0)
        launcher.guest_display_rotation = mock.Mock(return_value=3)
        with mock.patch.object(runner, 'RENDER_VERIFY_SETTLE_SECONDS', 0):
            launcher.align_visible_emulator()
        launcher.unpin_guest_display_rotation.assert_called_once_with()
        launcher.verify_visible_render.assert_called_once_with(3)
        launcher.save_display_pin.assert_called_once_with(3)
        self.assertEqual(launcher._aligned_guest_rotation, 3)
        rows = [call.args[0] for call in launcher.log.call_args_list if call.args]
        self.assertIn('stage=display-pin-missed', rows)

    def test_the_pin_round_trips_and_unusable_state_is_ignored(self):
        launcher = self.launcher()
        self.assertIsNone(launcher.load_display_pin())          # nothing persisted yet
        launcher.save_display_pin(3)
        self.assertEqual(launcher.load_display_pin(), 3)
        path = launcher.display_pin_path()
        path.write_text("not json", encoding="utf-8")
        self.assertIsNone(launcher.load_display_pin())
        path.write_text(json.dumps({"schema": 1, "user_rotation": 9}), encoding="utf-8")
        self.assertIsNone(launcher.load_display_pin())

    def test_align_skips_when_the_guest_never_moves(self):
        launcher = self.rotation_launcher()
        launcher.wait_for_guest_display_rotation = mock.Mock(return_value=3)
        launcher.provoke_guest_display_rotation = mock.Mock(return_value=None)
        launcher.align_visible_emulator()
        launcher.console_send.assert_not_called()
        self.assertIsNone(launcher._aligned_guest_rotation)
        self.assertTrue(any(call.args and call.args[0] == 'display-alignment-skipped'
                            for call in launcher.log.call_args_list))

    def test_provoke_moves_the_guest_quarter_turn_then_parks_the_sensor(self):
        launcher = self.rotation_launcher()
        launcher.wait_for_guest_display_rotation = mock.Mock(return_value=1)
        self.assertEqual(launcher.provoke_guest_display_rotation(3), 1)
        self.assertEqual(launcher.console_send.call_args_list, [
            mock.call('sensor set acceleration 9.810:0.000:0.000'),
            mock.call(runner.format_sensor_set(runner.NEUTRAL_ACCELERATION).decode().strip())])
        self.assertEqual(launcher.wait_for_guest_display_rotation.call_args_list,
                         [mock.call(exclude=3)])
        # Portrait has no landscape probe: never nudge the guest there.
        launcher.console_send.reset_mock()
        self.assertIsNone(launcher.provoke_guest_display_rotation(0))
        launcher.console_send.assert_not_called()

    def test_align_does_nothing_when_the_guest_rotation_never_settles_landscape(self):
        launcher = self.rotation_launcher()
        launcher.wait_for_guest_display_rotation = mock.Mock(return_value=None)
        launcher.align_visible_emulator()
        launcher.console_send.assert_not_called()
        self.assertIsNone(launcher._aligned_guest_rotation)
        self.assertTrue(any(call.args and call.args[0] == 'display-alignment-skipped'
                            for call in launcher.log.call_args_list))

    def test_guard_repairs_only_after_the_guest_rotation_moves(self):
        launcher = self.rotation_launcher()
        launcher.adb = mock.Mock(return_value=mock.Mock(
            returncode=0, stdout='    mCurrentOrientation=3\n', stderr=''))
        launcher._aligned_guest_rotation = 3
        launcher.guard_display_orientation()
        launcher.console_send.assert_not_called()
        # Guest flipped to the other landscape quarter: two steps restore it.
        launcher.adb.return_value = mock.Mock(returncode=0, stdout='    mCurrentOrientation=1\n', stderr='')
        launcher.guard_display_orientation()
        # A guard burst also re-parks the accelerometer (user directive
        # 2026-09-18): leaving the feed live is what let a corrected rotation
        # move again.
        self.assertEqual(
            launcher.console_send.call_args_list,
            [mock.call('rotate')] * 2
            + [mock.call(runner.format_sensor_set(runner.NEUTRAL_ACCELERATION).decode().strip())],
        )
        self.assertEqual(launcher._aligned_guest_rotation, 1)
        self.assertTrue(any(call.args and call.args[0] == 'display-orientation-repaired'
                            for call in launcher.log.call_args_list))
        # An unarmed launcher (never aligned) never probes or rotates.
        quiet = self.rotation_launcher()
        quiet.adb = mock.Mock()
        quiet.guard_display_orientation()
        quiet.adb.assert_not_called()
        quiet.console_send.assert_not_called()

    def test_top_level_preserves_failure_and_reports_cleanup_failure(self):
        launcher = mock.Mock()
        launcher.main.side_effect = runner.LauncherError('original startup failure')
        launcher.cleanup.side_effect = runner.LauncherError('cleanup failed too')
        with mock.patch.object(runner, 'Launcher', return_value=launcher), \
             mock.patch.object(runner, 'parse_args'), mock.patch.object(runner.signal, 'signal'):
            self.assertEqual(runner.main(), 1)
        self.assertEqual(launcher.log.call_args_list, [
            mock.call('error', error='original startup failure'),
            mock.call('cleanup-error', error='cleanup failed too')])

    def test_top_level_preserves_cancel_status_when_cleanup_logging_fails(self):
        launcher = mock.Mock()
        launcher.main.return_value = 130
        launcher.cleanup.side_effect = OSError('cleanup failed')
        launcher.log.side_effect = OSError('log failed')
        stderr = io.StringIO()
        with mock.patch.object(runner, 'Launcher', return_value=launcher), \
             mock.patch.object(runner, 'parse_args'), mock.patch.object(runner.signal, 'signal'), \
             mock.patch.object(runner.sys, 'stderr', stderr):
            self.assertEqual(runner.main(), 130)
        self.assertIn('cleanup-error: cleanup failed', stderr.getvalue())

    def test_success_is_not_reported_when_cleanup_fails(self):
        launcher = mock.Mock()
        launcher.main.return_value = 0
        launcher.cleanup.side_effect = OSError('cleanup failed')
        with mock.patch.object(runner, 'Launcher', return_value=launcher), \
             mock.patch.object(runner, 'parse_args'), mock.patch.object(runner.signal, 'signal'):
            self.assertEqual(runner.main(), 1)


GAME_DUMPSYS = ("mResumedActivity: ActivityRecord{u0 "
                "com.trueaxis.jetcarstunts2/.Jetcarstunts2Activity t1}")
HOME_DUMPSYS = ("mResumedActivity: ActivityRecord{u0 "
                "com.google.android.apps.nexuslauncher/.NexusLauncherActivity t2}")
SETTINGS_DUMPSYS = ("mResumedActivity: ActivityRecord{u0 "
                    "com.android.settings/.Settings t3}")
NO_RESUMED_LINE = "mCurrentFocus=Window{u0 com.trueaxis.jetcarstunts2/.MainActivity}"
# Home foreground, game Activity finished (no game task listed): a live
# process here is only cached. Shape mirrors the real Sept-14 user dumpsys
# (Stack #0 home TaskRecord only, game task gone).
HOME_ONLY_DUMPSYS = (
    "mResumedActivity: ActivityRecord{c094ace u0 "
    "com.google.android.apps.nexuslauncher/.NexusLauncherActivity t2}\n"
    " * TaskRecord{e3c316f #2 "
    "I=com.google.android.apps.nexuslauncher/.NexusLauncherActivity U=0 StackId=0 sz=1}\n"
    " Activities=[ActivityRecord{c094ace u0 "
    "com.google.android.apps.nexuslauncher/.NexusLauncherActivity t2}]")
# Home foreground with the game task still listed (Home-button
# backgrounding, Activity NOT finished).
HOME_WITH_GAME_TASK_DUMPSYS = (
    "mResumedActivity: ActivityRecord{c094ace u0 "
    "com.google.android.apps.nexuslauncher/.NexusLauncherActivity t2}\n"
    " * TaskRecord{6cf0c4e #84 A=com.trueaxis.jetcarstunts2 U=0 StackId=1 sz=1}\n"
    " Activities=[ActivityRecord{bb9aeb7 u0 "
    "com.trueaxis.jetcarstunts2/.Jetcarstunts2Activity t84}]\n"
    " * TaskRecord{e3c316f #2 "
    "I=com.google.android.apps.nexuslauncher/.NexusLauncherActivity U=0 StackId=0 sz=1}")


def _adb_result(stdout, returncode=0):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr="")


PIDOF_ALIVE = _adb_result("3211\n", returncode=0)
PIDOF_DEAD = _adb_result("", returncode=1)


class GameQuitWatcherTests(unittest.TestCase):
    """Native-Quit lifecycle: fake ADB foreground polls, fake clock, no devices."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.avd_home = self.root / 'avds'
        (self.avd_home / 'test.avd').mkdir(parents=True)
        for patcher in (mock.patch.object(runner, 'AVD_HOME', self.avd_home),
                        mock.patch.object(runner, 'AVD', 'test'),
                        mock.patch.object(runner.Launcher, 'endpoint_lock_path', return_value=self.root / 'ports.lock'),
                        mock.patch.dict(runner.os.environ, {'JCS2_LOGDIR': str(self.root / 'logs')})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clock = [100.0]
        clock_patcher = mock.patch.object(runner.time, 'monotonic', side_effect=lambda: self.clock[0])
        clock_patcher.start()
        self.addCleanup(clock_patcher.stop)

    def launcher(self):
        launcher = runner.Launcher(argparse.Namespace())
        launcher.log = mock.Mock()
        for owned in ('emulator', 'controller'):
            child = mock.Mock()
            child.process.poll.return_value = None  # alive
            setattr(launcher, owned, child)
        launcher.window_guard = None
        launcher.router = mock.Mock()
        launcher.router.bridge = None
        # Each pump advances the fake clock past the poll interval so every
        # loop iteration performs exactly one foreground poll.
        def pump(_duration):
            self.clock[0] += runner.GAME_QUIT_POLL_INTERVAL + 1.0
        launcher.router.pump.side_effect = pump
        return launcher

    def drive(self, launcher, dumpsys_script, pidof_script=(), max_polls=12):
        """Serve fake dumpsys/pidof polls; stop the loop after max_polls.

        The stop is counted on pump iterations (one per loop) because a
        suppressed poll (open panel) or a quit return performs no adb call.
        pidof is only queried on home/launcher foregrounds; scripts repeat
        their last entry when the loop outruns them.
        """
        adb_calls = []
        pumps = []
        counters = {"dumpsys": 0, "pidof": 0}

        def pump(_duration):
            self.clock[0] += runner.GAME_QUIT_POLL_INTERVAL + 1.0
            pumps.append(1)
            if len(pumps) >= max_polls:
                launcher.stop_requested = True

        launcher.router.pump.side_effect = pump

        def fake_adb(*args, **kwargs):
            adb_calls.append(args)
            kind = "pidof" if "pidof" in args else "dumpsys"
            counters[kind] += 1
            script = (pidof_script or [PIDOF_DEAD]) if kind == "pidof" else dumpsys_script
            step = script[min(counters[kind] - 1, len(script) - 1)]
            if isinstance(step, BaseException):
                raise step
            return step

        launcher.adb = mock.Mock(side_effect=fake_adb)
        result = launcher.run_until_stop()
        return result, adb_calls

    def logged(self, launcher, message):
        return [c for c in launcher.log.call_args_list if c.args and c.args[0] == message]

    def test_genuine_quit_home_plus_ended_process_returns_zero(self):
        launcher = self.launcher()
        dumpsys = [_adb_result(GAME_DUMPSYS)] + [_adb_result(HOME_DUMPSYS)] * 3
        result, calls = self.drive(launcher, dumpsys, [PIDOF_DEAD] * 3)
        self.assertEqual(result, 0)
        quits = self.logged(launcher, 'stage=game-quit')
        self.assertEqual(len(quits), 1)
        self.assertEqual(quits[0].kwargs['resumed_package'],
                         'com.google.android.apps.nexuslauncher')
        self.assertEqual(quits[0].kwargs['game_process'], 'ended')
        candidates = self.logged(launcher, 'game-poll-quit-candidate')
        self.assertEqual([c.kwargs['streak'] for c in candidates], [1, 2, 3])
        self.assertTrue(all(c.kwargs['game_process'] == 'ended' for c in candidates))
        self.assertTrue(any('pidof' in c for c in calls))

    def test_unrelated_foreground_never_quits_even_when_sustained(self):
        launcher = self.launcher()
        dumpsys = [_adb_result(SETTINGS_DUMPSYS)] * 6
        result, calls = self.drive(launcher, dumpsys, max_polls=6)
        self.assertEqual(result, 130)
        self.assertEqual(self.logged(launcher, 'stage=game-quit'), [])
        self.assertEqual(self.logged(launcher, 'game-poll-quit-candidate'), [])
        self.assertTrue(self.logged(launcher, 'game-poll-unrelated'))
        # No game-liveness probe is needed off the home path.
        self.assertFalse(any('pidof' in c for c in calls))

    def test_home_with_live_game_is_background_not_quit(self):
        launcher = self.launcher()
        dumpsys = [_adb_result(HOME_DUMPSYS)] * 6
        result, _ = self.drive(launcher, dumpsys, [PIDOF_ALIVE] * 6, max_polls=6)
        self.assertEqual(result, 130)
        self.assertEqual(self.logged(launcher, 'stage=game-quit'), [])
        self.assertEqual(self.logged(launcher, 'game-poll-quit-candidate'), [])
        self.assertTrue(self.logged(launcher, 'game-poll-backgrounded'))

    def test_home_with_live_game_task_present_is_background_not_quit(self):
        launcher = self.launcher()
        dumpsys = [_adb_result(HOME_WITH_GAME_TASK_DUMPSYS)] * 6
        result, _ = self.drive(launcher, dumpsys, [PIDOF_ALIVE] * 6, max_polls=6)
        self.assertEqual(result, 130)
        self.assertEqual(self.logged(launcher, 'stage=game-quit'), [])
        self.assertEqual(self.logged(launcher, 'game-poll-quit-candidate'), [])
        self.assertTrue(self.logged(launcher, 'game-poll-backgrounded'))

    def test_cached_process_with_finished_task_still_quits(self):
        launcher = self.launcher()
        launcher.begin_quit_teardown = mock.Mock()
        dumpsys = [_adb_result(HOME_ONLY_DUMPSYS)] * 4
        result, _ = self.drive(launcher, dumpsys, [PIDOF_ALIVE] * 4)
        self.assertEqual(result, 0)
        quits = self.logged(launcher, 'stage=game-quit')
        self.assertEqual(len(quits), 1)
        self.assertEqual(quits[0].kwargs['game_process'], 'cached-task-finished')
        self.assertEqual(quits[0].kwargs['game_task'], 'finished')
        candidates = self.logged(launcher, 'game-poll-quit-candidate')
        self.assertEqual([c.kwargs['streak'] for c in candidates], [1, 2, 3])
        self.assertTrue(all(c.kwargs['game_process'] == 'cached-task-finished'
                            for c in candidates))
        launcher.begin_quit_teardown.assert_called_once_with()

    def test_quit_begins_owned_teardown_promptly(self):
        launcher = self.launcher()
        launcher.begin_quit_teardown = mock.Mock()
        dumpsys = [_adb_result(GAME_DUMPSYS)] + [_adb_result(HOME_DUMPSYS)] * 3
        result, _ = self.drive(launcher, dumpsys, [PIDOF_DEAD] * 3)
        self.assertEqual(result, 0)
        launcher.begin_quit_teardown.assert_called_once_with()

    def test_teardown_failure_does_not_block_quit(self):
        launcher = self.launcher()
        launcher.begin_quit_teardown = mock.Mock(side_effect=RuntimeError('fake teardown failure'))
        dumpsys = [_adb_result(GAME_DUMPSYS)] + [_adb_result(HOME_DUMPSYS)] * 3
        result, _ = self.drive(launcher, dumpsys, [PIDOF_DEAD] * 3)
        self.assertEqual(result, 0)
        self.assertEqual(len(self.logged(launcher, 'stage=game-quit')), 1)

    def test_hide_skips_without_owned_emulator(self):
        launcher = self.launcher()
        launcher.emulator = None
        launcher.hide_owned_emulator_window = runner.Launcher.hide_owned_emulator_window.__get__(launcher)
        dumpsys = [_adb_result(GAME_DUMPSYS)] + [_adb_result(HOME_DUMPSYS)] * 3
        result, _ = self.drive(launcher, dumpsys, [PIDOF_DEAD] * 3)
        self.assertEqual(result, 0)
        skips = self.logged(launcher, 'game-hide-skip')
        self.assertTrue(any(c.kwargs.get('reason') == 'no-owned-emulator' for c in skips))

    def test_single_transient_home_poll_does_not_quit(self):
        launcher = self.launcher()
        dumpsys = [_adb_result(GAME_DUMPSYS), _adb_result(HOME_DUMPSYS),
                   _adb_result(GAME_DUMPSYS)]  # recovery resets the streak
        result, _ = self.drive(launcher, dumpsys, [PIDOF_DEAD], max_polls=6)
        self.assertEqual(result, 130)
        self.assertEqual(self.logged(launcher, 'stage=game-quit'), [])
        self.assertEqual(len(self.logged(launcher, 'game-poll-recovered')), 1)

    def test_two_consecutive_then_recovery_needs_full_streak_again(self):
        launcher = self.launcher()
        dumpsys = [_adb_result(GAME_DUMPSYS),
                   _adb_result(HOME_DUMPSYS), _adb_result(HOME_DUMPSYS),
                   _adb_result(GAME_DUMPSYS),
                   _adb_result(HOME_DUMPSYS), _adb_result(HOME_DUMPSYS)]
        result, _ = self.drive(launcher, dumpsys, [PIDOF_DEAD] * 6, max_polls=6)
        self.assertEqual(result, 130)
        self.assertEqual(self.logged(launcher, 'stage=game-quit'), [])

    def test_unknown_sample_resets_window_instead_of_accumulating(self):
        launcher = self.launcher()
        dumpsys = [_adb_result(HOME_DUMPSYS), _adb_result(HOME_DUMPSYS),
                   _adb_result("", returncode=1),  # ADB glitch: unknown
                   _adb_result(HOME_DUMPSYS), _adb_result(HOME_DUMPSYS),
                   _adb_result(HOME_DUMPSYS)]
        result, _ = self.drive(launcher, dumpsys, [PIDOF_DEAD] * 6, max_polls=6)
        # The glitch reset the 2-streak; the genuine quit still completes on
        # three fresh consecutive confirmations right after.
        self.assertEqual(result, 0)
        self.assertEqual(len(self.logged(launcher, 'stage=game-quit')), 1)
        resets = self.logged(launcher, 'game-poll-reset')
        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0].kwargs['streak'], 2)

    def test_missing_line_and_pidof_failure_reset_without_false_quit(self):
        launcher = self.launcher()
        dumpsys = [_adb_result(GAME_DUMPSYS),
                   _adb_result(HOME_DUMPSYS),
                   _adb_result(NO_RESUMED_LINE),  # transition: unknown
                   _adb_result(GAME_DUMPSYS)]     # recovery before confirmation
        result, _ = self.drive(launcher, dumpsys, [PIDOF_DEAD], max_polls=6)
        self.assertEqual(result, 130)
        self.assertEqual(self.logged(launcher, 'stage=game-quit'), [])
        resets = self.logged(launcher, 'game-poll-reset')
        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0].kwargs['streak'], 1)
        # A failed liveness probe is unknown too, never quit evidence.
        launcher2 = self.launcher()
        dumpsys2 = [_adb_result(HOME_DUMPSYS)] * 3
        pidof2 = [runner.LauncherError('fake transport cut')] + [PIDOF_DEAD] * 2
        result2, _ = self.drive(launcher2, dumpsys2, pidof2, max_polls=3)
        self.assertEqual(result2, 130)
        self.assertEqual(self.logged(launcher2, 'stage=game-quit'), [])

    def test_emulator_crash_still_raises_instead_of_clean_quit(self):
        launcher = self.launcher()
        launcher.emulator.process.poll.return_value = 1  # owned emulator gone
        dumpsys = [_adb_result(HOME_DUMPSYS)] * 4
        calls = []

        def fake_adb(*args, **kwargs):
            calls.append(args)
            return dumpsys[0]

        launcher.adb = mock.Mock(side_effect=fake_adb)
        with self.assertRaisesRegex(runner.LauncherError, 'emulator exited'):
            launcher.run_until_stop()
        self.assertEqual(calls, [])  # crash is reported before any quit poll
        self.assertEqual(self.logged(launcher, 'stage=game-quit'), [])


class FakeHideAdapter:
    """Fake X11 opener for hide tests: scripted inventory, recorded unmaps."""

    def __init__(self, windows):
        self._windows = windows
        self.unmapped = []
        self.closed = False

    def inventory(self):
        return list(self._windows)

    def unmap(self, xid):
        self.unmapped.append(xid)

    def close(self):
        self.closed = True


def _owned_main(xid=10, pid=123, mapped=True):
    return Window(xid, pid, ("qemu-system-x86_64", "Emulator"),
                  "Android Emulator - hardened_api28:5594", 1280, 800, mapped)


def _owned_toolbar(xid=11, pid=123, mapped=False):
    return Window(xid, pid, ("qemu-system-x86_64", "Emulator"),
                  "Emulator", 54, 506, mapped)


def _owned_transient(xid=12, pid=123, mapped=True):
    # Shutdown popup shape seen Sept-14 (owned xid 10485785, 700x84 mapped
    # while only the main was hidden on the quit path).
    return Window(xid, pid, ("qemu-system-x86_64", "Emulator"),
                  "Emulator", 700, 84, mapped)


class HideOwnedWindowTests(unittest.TestCase):
    """Hide uses the recorded ready-file identity, validated pid/class."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.avd_home = self.root / 'avds'
        (self.avd_home / 'test.avd').mkdir(parents=True)
        for patcher in (mock.patch.object(runner, 'AVD_HOME', self.avd_home),
                        mock.patch.object(runner, 'AVD', 'test'),
                        mock.patch.object(runner.Launcher, 'endpoint_lock_path', return_value=self.root / 'ports.lock'),
                        mock.patch.dict(runner.os.environ, {'JCS2_LOGDIR': str(self.root / 'logs'),
                                                            'DISPLAY': ':99'})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def launcher(self, pid=123):
        launcher = runner.Launcher(argparse.Namespace())
        launcher.log = mock.Mock()
        emulator = mock.Mock()
        emulator.process.pid = pid
        emulator.process.poll.return_value = None
        emulator.pgid = pid
        launcher.emulator = emulator
        return launcher

    def hide(self, launcher, windows, ready=None):
        if ready is not None:
            (launcher.run_dir / "gamescope-window-ready.json").write_text(json.dumps(ready))
        adapter = FakeHideAdapter(windows)
        launcher.hide_owned_emulator_window(opener=mock.Mock(return_value=adapter))
        return adapter

    def logged(self, launcher, message):
        return [c for c in launcher.log.call_args_list if c.args and c.args[0] == message]

    def test_recorded_identity_unmaps_only_recorded_main(self):
        launcher = self.launcher()
        foreign = Window(99, 456, ("qemu-system-x86_64", "Emulator"),
                         "Android Emulator - hardened_api28:5594", 1280, 800, True)
        adapter = self.hide(launcher, [_owned_main(), _owned_toolbar(), foreign],
                            ready={"pid": 123, "main_xid": 10, "toolbar_xids": [11]})
        self.assertEqual(adapter.unmapped, [10])
        hides = self.logged(launcher, 'stage=game-hide')
        self.assertEqual(len(hides), 1)
        self.assertEqual(hides[0].kwargs['via'], 'recorded-identity')
        self.assertTrue(adapter.closed)

    def test_missing_ready_file_falls_back_to_validated_inventory(self):
        launcher = self.launcher()
        adapter = self.hide(launcher, [_owned_main(), _owned_toolbar()])
        self.assertEqual(adapter.unmapped, [10])
        hides = self.logged(launcher, 'stage=game-hide')
        self.assertEqual(hides[0].kwargs['via'], 'inventory-fallback')

    def test_stale_recorded_xid_falls_back_to_recreated_main(self):
        launcher = self.launcher()
        adapter = self.hide(launcher, [_owned_main(xid=15), _owned_toolbar()],
                            ready={"pid": 123, "main_xid": 10, "toolbar_xids": [11]})
        self.assertEqual(adapter.unmapped, [15])
        hides = self.logged(launcher, 'stage=game-hide')
        self.assertEqual(hides[0].kwargs['via'], 'inventory-fallback')

    def test_recorded_pid_mismatch_ignores_ready_file(self):
        launcher = self.launcher()
        adapter = self.hide(launcher, [_owned_main(), _owned_toolbar()],
                            ready={"pid": 999, "main_xid": 10, "toolbar_xids": [11]})
        self.assertEqual(adapter.unmapped, [10])
        hides = self.logged(launcher, 'stage=game-hide')
        self.assertEqual(hides[0].kwargs['via'], 'inventory-fallback')

    def test_recorded_identity_also_hides_owned_transient(self):
        launcher = self.launcher()
        foreign = Window(99, 456, ("qemu-system-x86_64", "Emulator"),
                         "Android Emulator - hardened_api28:5594", 1280, 800, True)
        adapter = self.hide(launcher,
                            [_owned_main(), _owned_toolbar(),
                             _owned_transient(), foreign],
                            ready={"pid": 123, "main_xid": 10, "toolbar_xids": [11]})
        self.assertEqual(adapter.unmapped, [10, 12])
        hides = self.logged(launcher, 'stage=game-hide')
        self.assertEqual(len(hides), 1)
        self.assertEqual(hides[0].kwargs['via'], 'recorded-identity')
        self.assertEqual(sorted(hides[0].kwargs['xids']), [10, 12])
        self.assertTrue(adapter.closed)

    def test_transient_only_hide_without_mapped_main(self):
        launcher = self.launcher()
        adapter = self.hide(launcher,
                            [_owned_main(mapped=False), _owned_toolbar(),
                             _owned_transient()],
                            ready={"pid": 123, "main_xid": 10, "toolbar_xids": [11]})
        self.assertEqual(adapter.unmapped, [12])
        hides = self.logged(launcher, 'stage=game-hide')
        self.assertEqual(len(hides), 1)
        self.assertEqual(hides[0].kwargs['via'], 'owned-transients-only')

    def test_fallback_main_plus_owned_transient(self):
        launcher = self.launcher()
        adapter = self.hide(launcher, [_owned_main(), _owned_toolbar(),
                                       _owned_transient()])
        self.assertEqual(adapter.unmapped, [10, 12])
        hides = self.logged(launcher, 'stage=game-hide')
        self.assertEqual(hides[0].kwargs['via'], 'inventory-fallback')

    def test_no_display_never_touches_windows_or_ready(self):
        launcher = self.launcher()
        opener = mock.Mock()
        with mock.patch.dict(runner.os.environ, {}, clear=False):
            runner.os.environ.pop('DISPLAY', None)
            (launcher.run_dir / "gamescope-window-ready.json").write_text(
                json.dumps({"pid": 123, "main_xid": 10, "toolbar_xids": []}))
            launcher.hide_owned_emulator_window(opener=opener)
        opener.assert_not_called()
        skips = self.logged(launcher, 'game-hide-skip')
        self.assertTrue(any(c.kwargs.get('reason') == 'no-display' for c in skips))
        # The hide never marks anything ready: no ready write, no present.
        self.assertEqual(self.logged(launcher, 'stage=game-hide'), [])


class BeginQuitTeardownTests(unittest.TestCase):
    """Confirmed Quit starts the owned emulator exiting immediately."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.avd_home = self.root / 'avds'
        (self.avd_home / 'test.avd').mkdir(parents=True)
        for patcher in (mock.patch.object(runner, 'AVD_HOME', self.avd_home),
                        mock.patch.object(runner, 'AVD', 'test'),
                        mock.patch.object(runner.Launcher, 'endpoint_lock_path', return_value=self.root / 'ports.lock'),
                        mock.patch.dict(runner.os.environ, {'JCS2_LOGDIR': str(self.root / 'logs')})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def launcher(self):
        launcher = runner.Launcher(argparse.Namespace())
        launcher.log = mock.Mock()
        emulator = mock.Mock()
        emulator.process.pid = 123
        emulator.process.poll.return_value = None  # alive
        emulator.pgid = 123
        launcher.emulator = emulator
        launcher.hide_owned_emulator_window = mock.Mock()
        return launcher

    def logged(self, launcher, message):
        return [c for c in launcher.log.call_args_list if c.args and c.args[0] == message]

    def test_teardown_hides_then_terms_owned_emulator_group_only(self):
        launcher = self.launcher()
        with mock.patch.object(runner.os, 'killpg') as killpg:
            launcher.begin_quit_teardown()
        launcher.hide_owned_emulator_window.assert_called_once_with()
        killpg.assert_called_once_with(123, runner.signal.SIGTERM)
        downs = self.logged(launcher, 'stage=game-teardown')
        self.assertEqual(len(downs), 1)
        self.assertEqual(downs[0].kwargs['pid'], 123)

    def test_teardown_skips_term_when_emulator_already_exited(self):
        launcher = self.launcher()
        launcher.emulator.process.poll.return_value = 1
        with mock.patch.object(runner.os, 'killpg') as killpg:
            launcher.begin_quit_teardown()
        killpg.assert_not_called()
        self.assertEqual(self.logged(launcher, 'stage=game-teardown'), [])

    def test_teardown_term_failure_never_raises(self):
        launcher = self.launcher()
        with mock.patch.object(runner.os, 'killpg', side_effect=OSError('fake signal failure')):
            launcher.begin_quit_teardown()  # must not raise
        self.assertTrue(self.logged(launcher, 'game-teardown-skip'))


if __name__ == '__main__':
    unittest.main()
