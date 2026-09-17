#!/usr/bin/env python3
"""Runtime mode changes with fake sensors/processes, no sockets or X calls."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import runner
from tilt_control.adapter import TiltAdapter
from tilt_control.settings import TiltSettings, ControlMode


class LiveControlsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.launcher = mock.Mock()
        self.launcher.run_dir = Path(self.temp.name) / 'run'
        self.launcher.run_dir.mkdir()
        self.launcher.window_guard = object()
        self.out = io.BytesIO()
        self.router = runner.InputRouter(self.launcher, self.out)
        self.addCleanup(self.router.close)
        self.settings = TiltSettings(Path(self.temp.name) / 'tilt.json')
        self.adapter = TiltAdapter(self.settings, device_path='/never-open')
        self.adapter._reader = mock.Mock()
        self.adapter._reader.open.return_value = True
        self.adapter._reader.read_samples.return_value = []
        # A readable sensor must also report data before Tilt is accepted.
        ready = mock.patch.object(self.adapter, 'await_motion', return_value=True)
        self.motion_ready = ready.start()
        self.addCleanup(ready.stop)
        self.router._tilt_adapter = self.adapter

    def events(self):
        return [json.loads(line) for line in self.out.getvalue().splitlines()]

    def clear(self):
        self.out.seek(0)
        self.out.truncate()

    def status(self):
        return json.loads((self.launcher.run_dir / 'control-mode.json').read_text())

    def test_physical_stick_flows_in_both_view_modes(self):
        # Gamepad mode: physical LX/LY always forwarded.
        for axis, value in (('LX', .6), ('LY', -.4)):
            self.router._forward_line(json.dumps(dict(type='axis', axis=axis, value=value)).encode())
        self.assertEqual([e['value'] for e in self.events()], [.6, -.4])

    def test_analog_gate_drops_lx_ly_only_while_tilt_armed(self):
        # Sept-14 tilt-buttons: effective TILT + owned IMU drops analog
        # turning/pitch so stick and native tilt cannot double-drive.
        # Triggers, bumpers, and every other button always flow.
        self.router.change_control_mode('tilt', 'one')
        self.assertEqual(self.adapter.mode, ControlMode.TILT)
        self.assertTrue(self.adapter._open)
        self.clear()
        self.router._forward_line(b'{"type":"axis","axis":"LX","value":0.8}')
        self.router._forward_line(b'{"type":"axis","axis":"LY","value":0.2}')
        self.assertEqual(self.events(), [])
        for raw in (b'{"type":"axis","axis":"RT","value":0.9}',
                    b'{"type":"axis","axis":"LT","value":0.4}',
                    b'{"type":"button","key":"RB","action":"down"}',
                    b'{"type":"button","key":"LB","action":"down"}',
                    b'{"type":"button","key":"START","action":"down"}',
                    b'{"type":"button","key":"Y","action":"down"}'):
            self.router._forward_line(raw)
        seen = [(e.get('axis', e.get('key')), e.get('value', e.get('action')))
                for e in self.events()]
        self.assertEqual(seen, [('RT', 0.9), ('LT', 0.4), ('RB', 'down'),
                                ('LB', 'down'), ('START', 'down'), ('Y', 'down')])
        self.router.change_control_mode('gamepad', 'two')
        self.assertEqual(self.settings.mode, ControlMode.GAMEPAD)

    def test_unowned_tilt_request_stays_armed_fail_closed(self):
        # Requested Tilt Drive without an owned sensor PERSISTS Tilt Drive:
        # sticks stay dropped (no silent fallback re-enables them); the dead
        # feed is reported loudly so the user selects Gamepad explicitly.
        self.adapter._reader.open.return_value = False
        self.router.change_control_mode('tilt', 'dead-stick-guard')
        self.assertEqual(self.status()['mode'], 'tilt')
        self.assertEqual(self.settings.mode, ControlMode.TILT)
        self.assertFalse(self.status()['sensor_available'])
        self.assertIn('Tilt Drive', self.status()['error'])
        self.clear()
        self.router._forward_line(b'{"type":"axis","axis":"LX","value":0.8}')
        self.router._forward_line(b'{"type":"button","key":"RB","action":"down"}')
        self.router._forward_line(b'{"type":"axis","axis":"LT","value":1.0}')
        self.assertEqual([e.get('axis', e.get('key')) for e in self.events()],
                         ['RB', 'LT'])

    def test_sensor_drop_mid_session_keeps_gate_armed(self):
        # A mid-session IMU drop must NOT release the gate (fail-closed):
        # sticks stay dropped; recovery needs no re-arm and leaks nothing.
        self.router.change_control_mode('tilt', 'drop')
        self.clear()
        self.router._forward_line(b'{"type":"axis","axis":"LX","value":0.8}')
        self.assertEqual(self.events(), [])
        self.adapter._open = False  # read failure closed the node
        self.router._forward_line(b'{"type":"axis","axis":"LX","value":0.8}')
        self.assertEqual(self.events(), [])
        self.router._forward_line(b'{"type":"button","key":"RB","action":"down"}')
        self.assertEqual([e.get('key') for e in self.events()], ['RB'])

    def test_gate_engage_levels_held_stick_and_release_restores(self):
        self.router._handle_side_channel_event(
            dict(type='axis', axis='LX', value=.5, t_ms=1))
        held = self.events()[-1]['value']
        self.assertNotEqual(held, 0.0)
        self.router.change_control_mode('tilt', 'settle')
        # Engage: neutral levels the held stick, no stick restore.
        self.assertEqual([e['value'] for e in self.events()][-2:], [0, 0])
        self.clear()
        # Sensor loss alone does NOT release the gate (fail-closed).
        self.adapter._open = False
        self.router._mirror_native_sensor()
        self.router._forward_line(b'{"type":"axis","axis":"LX","value":0.8}')
        self.assertEqual(self.events(), [])
        # Only an explicit Gamepad selection restores the cached sticks
        # (neutral pair first, then cached values in axis order; the last
        # cached LX is the raw 0.8 forward above, which the gate dropped).
        self.router.change_control_mode('gamepad', 'settle-back')
        tail = [(e.get('axis'), e.get('value')) for e in self.events()[-4:]]
        self.assertEqual(tail[-4:-2], [('LX', 0.0), ('LY', 0.0)])
        self.assertEqual(tail[-2], ('LX', 0.8))

    def test_production_route_tilt_drive_drops_sticks_preserves_pedals_buttons(self):
        # Production-route smoke (not unit metrics): real DeckControls
        # mapping through the real forward path. Tilt Drive drops LX/LY
        # (curved or not — the gate sees raw events first) while analog
        # pedals and every button flow; Gamepad restores full forwarding.
        from joystick_bridge import DeckControls
        self.router._deck_controls = DeckControls()
        self.router.change_control_mode('tilt', 'smoke-tilt')
        self.clear()
        drive = [
            dict(type='axis', axis='LX', value=0.8, t_ms=1),
            dict(type='axis', axis='LY', value=-0.5, t_ms=2),
            dict(type='axis', axis='RT', value=0.9, t_ms=3),
            dict(type='axis', axis='LT', value=0.4, t_ms=4),
            dict(type='button', key='RB', action='down', t_ms=5),
            dict(type='button', key='LB', action='down', t_ms=6),
            dict(type='button', key='START', action='down', t_ms=7),
            dict(type='button', key='Y', action='down', t_ms=8),
        ]
        for event in drive:
            self.router._handle_side_channel_event(dict(event))
        seen = [(e.get('axis', e.get('key')), e.get('value', e.get('action')))
                for e in self.events()]
        axes = [axis for axis, _ in seen]
        self.assertNotIn('LX', axes)
        self.assertNotIn('LY', axes)
        # Pedals/buttons preserved through the production mapping
        # (physical RT gas -> digital RB key; physical LT -> RT axis;
        # physical RB -> LB key; physical LB -> LT axis; START -> BACK).
        self.assertEqual(seen, [('RB', 'down'), ('RT', 0.4), ('LB', 'down'),
                                ('LT', 1.0), ('BACK', 'down'), ('Y', 'down')])
        # Reversible: explicit Gamepad restores stick forwarding.
        self.router.change_control_mode('gamepad', 'smoke-back')
        self.clear()
        self.router._handle_side_channel_event(
            dict(type='axis', axis='LX', value=0.8, t_ms=9))
        self.assertTrue(any(e.get('axis') == 'LX' for e in self.events()))

    def test_unavailable_sensor_persists_tilt_fail_closed(self):
        self.adapter._reader.open.return_value = False
        self.router.change_control_mode('tilt', 'unavailable')
        self.assertEqual(self.status()['mode'], 'tilt')
        self.assertEqual(self.status()['requested_mode'], 'tilt')
        self.assertFalse(self.status()['sensor_available'])
        self.assertIn('unavailable', self.status()['error'])
        self.assertIn('Tilt Drive', self.status()['error'])
        self.assertEqual(self.settings.mode, ControlMode.TILT)
        self.clear()
        self.router._forward_line(b'{"type":"axis","axis":"LX","value":0.8}')
        self.router._forward_line(b'{"type":"button","key":"RB","action":"down"}')
        self.assertEqual([e.get('axis', e.get('key')) for e in self.events()], ['RB'])

    def test_silent_opened_sensor_stays_armed_and_keeps_physical_buttons(self):
        self.motion_ready.return_value = False
        self.router.change_control_mode('tilt', 'silent')
        self.motion_ready.assert_called_once_with(runner.MOTION_READY_TIMEOUT)
        self.assertEqual(self.status()['mode'], 'tilt')
        self.assertFalse(self.status()['sensor_available'])
        self.assertIn('no data', self.status()['error'])
        self.assertEqual(self.settings.mode, ControlMode.TILT)
        self.clear()
        self.router._forward_line(b'{"type":"axis","axis":"LX","value":0.8}')
        self.router._forward_line(b'{"type":"axis","axis":"LT","value":1.0}')
        self.assertEqual([e.get('axis') for e in self.events()], ['LT'])

    def test_startup_tilt_unavailable_keeps_persisted_tilt(self):
        # Fail-closed startup: persisted Tilt Drive + dead sensor keeps Tilt
        # (gate armed) instead of rewriting the settings to Gamepad.
        self.settings.mode = ControlMode.TILT
        with mock.patch.object(runner.TiltAdapter, 'open', return_value=False), \
             mock.patch.object(runner.TiltAdapter, 'await_motion', return_value=False):
            self.assertTrue(self.router.start_tilt(str(self.settings._path)))
        self.assertEqual(self.settings.mode, ControlMode.TILT)
        status = json.loads((self.launcher.run_dir / 'control-mode.json').read_text())
        self.assertEqual(status['mode'], 'tilt')
        self.assertIn('Tilt Drive', status['error'])
        self.clear()
        self.router._forward_line(b'{"type":"axis","axis":"LX","value":0.8}')
        self.router._forward_line(b'{"type":"button","key":"RB","action":"down"}')
        self.assertEqual([e.get('axis', e.get('key')) for e in self.events()], ['RB'])

    def test_saved_tilt_with_silent_sensor_stays_armed(self):
        self.settings.mode = ControlMode.TILT
        self.router._tilt_adapter = None
        with mock.patch.object(runner.TiltAdapter, 'open', return_value=True), \
                mock.patch.object(runner.TiltAdapter, 'await_motion', return_value=False), \
                mock.patch.object(runner.TiltAdapter, 'close') as closed:
            self.assertTrue(self.router.start_tilt(str(self.settings._path)))
        closed.assert_called_once()
        self.assertEqual(self.status()['mode'], 'tilt')
        self.assertEqual(self.settings.mode, ControlMode.TILT)
        self.assertIn('Tilt Drive', self.status()['error'])
        self.assertIn('no data', self.status()['error'])
        self.assertTrue(self.router.tilt_mode)

    def test_failed_persistence_keeps_old_effective_mode_and_reports_error(self):
        with mock.patch.object(self.settings, '_save', side_effect=OSError('disk full')):
            self.router.change_control_mode('tilt', 'fail')
        self.assertEqual(self.adapter.mode, ControlMode.GAMEPAD)
        self.assertEqual(self.status()['mode'], 'gamepad')
        self.assertIn('disk full', self.status()['error'])
        self.assertEqual(self.events(), [])

    def test_no_synthetic_imu_axes_ever_forwarded(self):
        # Corrected-C: the router never calls consume_motion. Deck tilt
        # reaches the game ONLY as native accelerometer vectors.
        self.router.change_control_mode('tilt')
        self.router._sensor_transport = FakeSensorTransport()
        self.adapter._estimator._roll = 0.2
        self.adapter._estimator._pitch = -0.1
        self.adapter._estimator._last_ts = __import__('time').monotonic()
        self.clear()
        with mock.patch.object(self.adapter, 'consume_motion') as consume:
            self.router._mirror_native_sensor()
        consume.assert_not_called()
        self.assertEqual(self.events(), [])
        self.assertEqual(len(self.router._sensor_transport.pushes), 1)

    def test_control_request_is_consumed_and_acknowledged_not_sent_to_guest(self):
        self.router._forward_line(b'{"type":"control_mode","mode":"tilt","request_id":"abc"}')
        self.assertEqual(self.status()['request_id'], 'abc')
        self.assertTrue(all(e['type'] == 'axis' for e in self.events()))

    def test_view_opens_once_release_consumed_and_start_pause_preserved(self):
        self.router._forward_line(b'{"type":"button","key":"BACK","action":"down"}')
        self.assertEqual(self.events()[-1]['key'], 'BACK')
        for action in ('down', 'down', 'up'):
            self.router._forward_line(json.dumps(dict(type='button', key='VIEW', action=action)).encode())
        self.launcher.spawn.assert_called_once()
        self.assertFalse(any(e.get('key') == 'VIEW' for e in self.events()))
        self.assertTrue((self.launcher.run_dir / 'controls-panel-active').exists())
        self.clear()
        self.router._forward_line(b'{"type":"axis","axis":"LX","value":0.7}')
        self.router._forward_line(b'{"type":"button","key":"X","action":"down"}')
        self.assertEqual(self.events(), [])
        self.router._controls_panel.process.poll.return_value = 0
        self.router._poll_controls_panel()
        self.assertFalse((self.launcher.run_dir / 'controls-panel-active').exists())
        self.assertEqual(self.events()[-2]['value'], .7)

    def test_panel_holds_neutral_during_mode_change_then_restores_on_return(self):
        self.router._stick_axes = {'LX': .5, 'LY': -.2}
        self.router.open_controls_panel()
        self.clear()
        self.router.change_control_mode('gamepad', 'panel')
        self.assertEqual([e['value'] for e in self.events()], [0, 0])
        with mock.patch.object(self.adapter, 'consume_motion') as consume:
            self.router._mirror_native_sensor()
        consume.assert_not_called()

    def test_panel_spawn_failure_keeps_game_running_and_removes_focus_marker(self):
        self.launcher.spawn.side_effect = OSError('spawn failed')
        self.router.open_controls_panel()
        self.assertIsNone(self.router._controls_panel)
        self.assertFalse((self.launcher.run_dir / 'controls-panel-active').exists())
        self.launcher.log.assert_called_with('controls-panel-error', error='spawn failed')

    def test_raw_stick_restoration_uses_same_curve_as_normal_forwarding(self):
        event = dict(type='axis', axis='LX', value=.5, t_ms=1)
        self.router._handle_side_channel_event(event)
        normal = self.events()[-1]['value']
        self.router.change_control_mode('tilt')
        self.clear()
        self.router.change_control_mode('gamepad')
        # Neutral pair, then cached stick restore (LX restored, LY last).
        self.assertEqual([e['value'] for e in self.events()][:2], [0, 0])
        self.assertEqual(self.events()[-2]['value'], normal)
        self.assertEqual(event['value'], .5)

    def test_native_poll_cadence_follows_imu_ownership_not_view_mode(self):
        # Re-probe must not recover here: the reader stays closed so the
        # not-owned phases keep slow cadence; owned phase stays fast.
        self.adapter._reader.open.return_value = False
        with mock.patch.object(self.router.selector, 'select', return_value=[]) as select:
            self.router.pump(.25)
            select.assert_called_with(.25)
            self.adapter._open = True
            self.router.pump(.25)
            select.assert_called_with(.01)
            self.adapter._open = False
            self.router.pump(.25)
            select.assert_called_with(.25)

    def test_imu_reprobe_recovers_after_transient_open_failure(self):
        # Startup probe failed (_open False) but the device is back: the
        # next mirror re-opens (bounded) and reports recovery.
        self.adapter._open = False
        self.router._mirror_native_sensor()
        self.assertTrue(self.adapter._open)
        self.launcher.log.assert_any_call('tilt-imu-recovered', device_path=mock.ANY,
                                          match_tier=mock.ANY)

    def test_imu_reprobe_never_spins_per_frame(self):
        # A persistently closed reader costs one open attempt per 5 s
        # window, not one per pump.
        self.adapter._reader.open.return_value = False
        self.adapter._open = False
        self.router._mirror_native_sensor()
        self.router._mirror_native_sensor()
        self.router._mirror_native_sensor()
        self.assertEqual(self.adapter._reader.open.call_count, 1)
        self.assertFalse(self.adapter._open)

    def test_gamepad_setup_still_probes_imu_for_native_feed(self):
        # Always-on design: the IMU is owned in every View mode so the
        # game's own Gamepad toggle alone selects the driving source.
        self.router._tilt_adapter = None
        with mock.patch.object(runner.TiltAdapter, 'open', return_value=True) as opened, \
             mock.patch.object(runner.TiltAdapter, 'await_motion', return_value=True):
            self.assertTrue(self.router.start_tilt(str(self.settings._path)))
        opened.assert_called_once_with()
        self.assertEqual(self.status()['mode'], 'gamepad')
        self.assertTrue(self.status()['sensor_available'])


class FakeSensorTransport:
    """Records accelerometer pushes; never touches sockets/ADB/devices."""

    def __init__(self, connected=True):
        self.pushes: list[tuple[tuple[float, float, float], bool]] = []
        self.connected = connected
        self.is_open = connected
        self.last_error = None if connected else "fake console down"
        self.closed = False

    def connect(self):
        self.is_open = self.connected
        return self.connected

    def set_acceleration(self, vector, *, force=False):
        if not self.connected:
            self.last_error = "fake console down"
            self.is_open = False
            return False
        self.pushes.append((tuple(vector), force))
        return True

    def close(self):
        self.closed = True
        self.is_open = False


class NativeSensorTests(unittest.TestCase):
    """Native accelerometer mirror: guest sensor vs virtual stick routing."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.launcher = mock.Mock()
        self.launcher.run_dir = Path(self.temp.name) / 'run'
        self.launcher.run_dir.mkdir()
        self.launcher.window_guard = object()
        self.out = io.BytesIO()
        self.router = runner.InputRouter(self.launcher, self.out)
        self.addCleanup(self.router.close)
        self.settings = TiltSettings(Path(self.temp.name) / 'tilt.json')
        self.adapter = TiltAdapter(self.settings, device_path='/never-open')
        self.adapter._reader = mock.Mock()
        self.adapter._reader.open.return_value = True
        self.adapter._reader.read_samples.return_value = []
        ready = mock.patch.object(self.adapter, 'await_motion', return_value=True)
        self.motion_ready = ready.start()
        self.addCleanup(ready.stop)
        self.router._tilt_adapter = self.adapter
        self.launcher.run.side_effect = AssertionError("no ADB per sensor frame")

    def _tilt_on(self):
        self.router.change_control_mode('tilt', 'native-one')
        self.assertEqual(self.status()['mode'], 'tilt')

    def status(self):
        return json.loads((self.launcher.run_dir / 'control-mode.json').read_text())

    def events(self):
        return [json.loads(line) for line in self.out.getvalue().splitlines()]

    def clear(self):
        self.out.seek(0)
        self.out.truncate()

    def test_tilt_pushes_native_accelerometer_alongside_virtual_axes(self):
        transport = FakeSensorTransport()
        self.router._sensor_transport = transport
        self._tilt_on()
        self.adapter._estimator._roll = 0.2
        self.adapter._estimator._pitch = -0.1
        self.adapter._estimator._last_ts = __import__('time').monotonic()
        self.clear()  # drop switch-time neutral/restore frames
        with mock.patch.object(self.adapter, 'consume_motion') as consume:
            self.router._mirror_native_sensor()
        consume.assert_not_called()
        self.assertEqual(len(transport.pushes), 1)
        ax, ay, az = transport.pushes[0][0]
        self.assertGreater(ax, 0.0)
        self.assertLess(ay, 0.0)
        self.assertGreater(az, 9.0)
        self.assertFalse(transport.pushes[0][1])  # live frame, not forced
        self.assertEqual(self.events(), [])  # no synthetic axes, ever

    def test_gamepad_view_still_mirrors_native_tilt(self):
        # Core always-on semantics: View GAMEPAD governs nothing about
        # the sticks (always forwarded); the native feed runs so GAME
        # Gamepad OFF drives from tilt with no View change.
        transport = FakeSensorTransport()
        self.router._sensor_transport = transport
        self.router.change_control_mode('gamepad', 'native-gamepad')
        self.assertEqual(self.status()['mode'], 'gamepad')
        self.adapter._open = True
        self.adapter._estimator._roll = 0.15
        self.adapter._estimator._pitch = 0.1
        self.adapter._estimator._last_ts = __import__('time').monotonic()
        self.out.seek(0)
        self.out.truncate()
        with mock.patch.object(self.adapter, 'consume_motion') as consume:
            self.router._mirror_native_sensor()
        consume.assert_not_called()
        self.assertEqual(len(transport.pushes), 1)
        self.assertGreater(transport.pushes[0][0][0], 0.0)
        self.assertEqual(self.out.getvalue(), b"")  # no virtual stick frames

    def test_physical_buttons_unaffected_by_live_native_push(self):
        # Analog-only gate: armed tilt drops LX/LY (no double-drive with
        # the native feed) while every button/trigger still reaches the
        # guest alongside the accelerometer push.
        transport = FakeSensorTransport()
        self.router._sensor_transport = transport
        self._tilt_on()
        self.adapter._estimator._last_ts = __import__('time').monotonic()
        self.router._mirror_native_sensor()
        self.clear()
        self.router._forward_line(b'{"type":"axis","axis":"LX","value":0.8}')
        self.router._forward_line(b'{"type":"button","key":"RB","action":"down"}')
        self.router._forward_line(b'{"type":"axis","axis":"LT","value":1.0}')
        self.assertEqual([e.get('axis', e.get('key')) for e in self.events()],
                         ['RB', 'LT'])
        self.assertTrue(transport.pushes)

    def test_leaving_tilt_keeps_native_session_open(self):
        transport = FakeSensorTransport()
        self.router._sensor_transport = transport
        self._tilt_on()
        self.router.change_control_mode('gamepad', 'native-two')
        self.assertIs(self.router._sensor_transport, transport)
        self.assertFalse(transport.closed)
        self.adapter._estimator._roll = 0.1
        self.adapter._estimator._last_ts = __import__('time').monotonic()
        self.router._mirror_native_sensor()
        self.assertTrue(transport.pushes)

    def test_dead_imu_leaves_guest_sensor_untouched(self):
        self.router._tilt_adapter = self.adapter  # _open False: no IMU owned
        self.assertFalse(self.adapter._open)
        with mock.patch.object(self.adapter, 'consume_motion') as consume:
            self.router._mirror_native_sensor()
        consume.assert_not_called()
        self.assertIsNone(self.router._sensor_transport)
        self.assertNotEqual(self.router._native_state, "live")

    def test_stale_imu_levels_guest_once_then_quiet(self):
        transport = FakeSensorTransport()
        self.router._sensor_transport = transport
        self._tilt_on()
        self.adapter._estimator._roll = 0.4
        self.adapter._estimator._last_ts = __import__('time').monotonic() - 10.0
        with mock.patch.object(self.adapter, 'sensor_acceleration',
                                  side_effect=AssertionError("stale must not use live angles")):
            self.router._mirror_native_sensor()
            self.router._mirror_native_sensor()
        self.assertEqual([p[0] for p in transport.pushes], [(0.0, 0.0, 9.81)])
        self.assertTrue(all(p[1] for p in transport.pushes))

    def test_reconnect_backoff_never_spins_per_frame(self):
        created = []
        def factory(*args, **kwargs):
            transport = FakeSensorTransport(connected=False)
            created.append(transport)
            return transport
        self._tilt_on()
        with mock.patch.object(runner, 'ConsoleSensorTransport', side_effect=factory):
            self.router._push_tilt_to_guest()
            self.router._push_tilt_to_guest()
        self.assertEqual(len(created), 1)
        self.assertEqual(self.router._native_state, "unavailable")
        self.assertIn("Gamepad", self.router._native_error)

    def test_panel_open_levels_sensor_and_suppresses_live_push(self):
        transport = FakeSensorTransport()
        self.router._sensor_transport = transport
        self._tilt_on()
        transport.pushes.clear()
        self.router.open_controls_panel()
        self.assertTrue(transport.pushes)
        self.assertEqual(transport.pushes[-1][0], (0.0, 0.0, 9.81))
        transport.pushes.clear()
        with mock.patch.object(self.adapter, 'consume_motion') as consume:
            self.router._mirror_native_sensor()
        consume.assert_not_called()
        self.assertEqual(transport.pushes, [])

    def test_native_failure_keeps_gate_and_spares_buttons(self):
        self.router._sensor_transport = FakeSensorTransport(connected=False)
        self._tilt_on()
        self.adapter._estimator._last_ts = __import__('time').monotonic()
        self.router._mirror_native_sensor()
        self.assertEqual(self.router._native_state, "unavailable")
        self.assertIn("Gamepad", self.router._native_error)
        self.assertIn("Tilt Drive", self.router._native_error)
        # Analog sticks stay gated while tilt is armed; buttons are spared.
        self.clear()
        self.router._forward_line(b'{"type":"axis","axis":"LX","value":0.8}')
        self.router._forward_line(b'{"type":"button","key":"RB","action":"down"}')
        self.assertEqual([e.get('axis', e.get('key')) for e in self.events()], ['RB'])

    def test_mirror_feeds_real_samples_before_pushing_vector(self):
        # Sept-14 wiring regression: the runner pushed
        # sensor_acceleration() without ever feeding IMU samples into the
        # estimator, so the guest vector was frozen at neutral while the
        # status claimed live. Real reader samples (not preset angles,
        # no mocked sensor_acceleration) must move the pushed vector.
        import math as _math
        import time as _time
        from tilt_control.motion_reader import MotionSample as _Sample
        transport = FakeSensorTransport()
        self.router._sensor_transport = transport
        self._tilt_on()
        self.adapter._open = True
        tilted_x = int(16384 * _math.sin(_math.radians(20)))
        tilted_z = int(-16384 * _math.cos(_math.radians(20)))
        now = _time.monotonic()
        samples = []
        for i in range(5):
            s = _Sample()
            s.acc_x, s.acc_y, s.acc_z = tilted_x, 0, tilted_z
            s.gyro_x = s.gyro_y = s.gyro_z = 0
            s.timestamp = now + i * 0.004
            samples.append(s)
        self.adapter._reader.read_samples.side_effect = [samples, []]
        self.clear()
        with mock.patch.object(self.adapter, 'consume_motion') as consume:
            self.router._mirror_native_sensor()
        consume.assert_not_called()
        self.assertTrue(transport.pushes)
        ax, ay, az = transport.pushes[-1][0]
        # 20-degree right tilt must read as positive ax, far from neutral.
        self.assertGreater(ax, 1.0)
        self.assertEqual(self.events(), [])  # no synthetic axes, ever

    def test_capture_replay_vectors_stable_without_zero_corruption(self):
        # Sept-14 user capture (evdev-user-capture-20260914T203207Z):
        # kernel suppresses unchanged axes per SYN group, so the filed
        # rows carry artifact zeros. Rebuild kernel-style groups (exact
        # zeros omitted — a live value is virtually never exactly 0),
        # drive the PRODUCTION reader + estimator + console transport,
        # and require stable live vectors (old zero-emitting reader
        # snapped attitudes between pushes).
        import math as _math
        import struct as _struct
        import time as _time
        from tilt_control.motion_reader import MotionReader as _Reader
        quiescent = [
            (2208, 0, 108, 0, 0, 0), (0, 16492, 0, 2, 0, -2),
            (0, 16519, 102, 4, 0, 0), (0, 16526, 97, 0, 0, 0),
            (2234, 0, 0, 2, 0, 0), (2340, 16438, 101, 0, 0, 0),
            (2230, 16458, 107, 0, 0, -2), (2234, 16549, 100, 0, 0, 0),
            (0, 0, 95, 0, 0, 0), (0, 16555, 116, 0, 0, 0),
            (2257, 16548, 47, 0, 0, 0), (2276, 16526, 73, 0, 2, 0),
        ]
        burst = [
            (2267, 0, 0, 0, 0, 0), (0, 0, 0, 0, 0, 0),
            (0, 16468, 87, 0, -2, 0), (0, 16548, 71, 0, 0, -4),
            (0, 16555, 76, 2, 0, 0), (0, 16560, 80, 0, 0, 0),
            (2094, 16350, 0, 0, 0, 0), (2180, 15969, 0, 0, -3, 0),
            (2149, 15253, -237, 15, 2, -13), (2141, 0, 0, 0, 0, 0),
            (741, 19447, 950, -19, -1, -23),
            (2500, 22080, 932, -32, -6, 1),
        ]
        reader = _Reader()
        r, w = __import__("os").pipe()
        try:
            __import__("fcntl").fcntl(
                r, __import__("fcntl").F_SETFL,
                __import__("os").O_NONBLOCK)
            reader._fd = r
            fmt = _struct.Struct("llHHi")
            base = 1770000000.0
            blob = bytearray()
            for index, row in enumerate(quiescent + burst):
                sec = int(base + index * 0.004)
                usec = int(round((base + index * 0.004 - sec) * 1_000_000))
                framed = False
                for code, value in enumerate(row):
                    if value != 0:
                        blob += fmt.pack(sec, usec, 3, code, value)
                        framed = True
                if framed:
                    blob += fmt.pack(sec, usec, 0, 0, 0)
            __import__("os").write(w, bytes(blob))
            adapter = TiltAdapter(self.settings, device_path="/never-open")
            adapter._reader = reader
            adapter._open = True
            # Production flow calibrates at neutral hold first: rest
            # attitude here is ≈(2260, 16500, 90), so seed that neutral
            # (zeros are kernel-suppression artifacts, never neutral).
            from tilt_control.motion_reader import MotionSample as _Sample
            seed = _Sample()
            seed.acc_x, seed.acc_y, seed.acc_z = 2260, 16500, 90
            seed.timestamp = _time.monotonic()
            adapter.calibrate(samples=[seed] * 5)
            self.router._tilt_adapter = adapter
            transport = FakeSensorTransport()
            self.router._sensor_transport = transport
            with __import__("unittest.mock", fromlist=["x"]).patch.object(
                    adapter, "consume_motion") as consume:
                self.router._push_tilt_to_guest()
                first = list(transport.pushes)
                self.router._push_tilt_to_guest()
                second = list(transport.pushes)[len(first):]
            consume.assert_not_called()
            pushes = first + second
            self.assertTrue(pushes, "fixture must drive at least one push")
            self.assertTrue(all(not force for _, force in pushes),
                            "pushes must be live frames, not forced neutral")
            vectors = [vector for vector, _ in pushes]
            for vector in vectors:
                norm = _math.sqrt(sum(c * c for c in vector))
                self.assertAlmostEqual(norm, 9.81, delta=0.6)
            for prev, cur in zip(vectors, vectors[1:]):
                dot = sum(a * b for a, b in zip(prev, cur)) / (
                    _math.sqrt(sum(a * a for a in prev)) *
                    _math.sqrt(sum(c * c for c in cur)))
                angle = _math.degrees(
                    _math.acos(max(-1.0, min(1.0, dot))))
                self.assertLess(
                    angle, 30.0,
                    "zero-corruption snaps vectors between pushes")
        finally:
            reader._fd = None
            __import__("os").close(r)
            __import__("os").close(w)


if __name__ == '__main__':
    unittest.main()
