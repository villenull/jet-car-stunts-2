#!/usr/bin/env python3
"""Offline settings/axis routing tests; no displays or input devices opened."""
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from controls_settings import save_selection, show_settings, request_switch, read_status, sensor_summary
import json
from tilt_control.adapter import TiltAdapter
from tilt_control.settings import TiltSettings, ControlMode


class ControlSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state/settings.json'
        self.settings = TiltSettings(self.path)

    def test_tilt_mode_controls_both_axes_and_sensor_failure_keeps_stick(self):
        save_selection(self.settings, 'tilt')
        adapter = TiltAdapter(self.settings)
        self.assertEqual(adapter.mode, ControlMode.TILT)
        self.assertEqual(adapter.controlled_axes, frozenset())
        adapter._open = True  # synthetic sensor state, no device call
        self.assertEqual(adapter.controlled_axes, frozenset({'LX', 'LY'}))

    def test_joystick_mode_does_not_suppress_either_axis(self):
        save_selection(self.settings, 'gamepad')
        adapter = TiltAdapter(self.settings)
        adapter._open = True
        self.assertEqual(adapter.controlled_axes, frozenset())

    def test_mixed_legacy_choice_defaults_to_joystick(self):
        self.settings.update(mode='tilt', steering_mode='tilt', pitch_mode='gamepad')
        self.assertEqual(self.settings.mode, ControlMode.GAMEPAD)
        self.assertEqual(self.settings.axis_mode('LX'), ControlMode.GAMEPAD)
        self.assertEqual(self.settings.axis_mode('LY'), ControlMode.GAMEPAD)

    def test_save_normalizes_legacy_fields_in_single_write(self):
        self.settings.update(steering_mode='tilt', pitch_mode='gamepad')
        with patch.object(self.settings, '_save', wraps=self.settings._save) as save:
            save_selection(self.settings, 'tilt')
        self.assertEqual(save.call_count, 1)
        stored = self.settings._load()
        self.assertEqual([stored[k] for k in ('mode', 'steering_mode', 'pitch_mode')], ['tilt'] * 3)

    def test_legacy_mode_applies_to_both_axes(self):
        self.settings.update(mode='tilt')
        self.assertEqual(self.settings.axis_mode('LX'), ControlMode.TILT)
        self.assertEqual(self.settings.axis_mode('LY'), ControlMode.TILT)
        self.settings.mode = ControlMode.GAMEPAD
        self.assertEqual(self.settings.axis_mode('LX'), ControlMode.GAMEPAD)

    def test_reject_selection_without_mutation(self):
        with self.assertRaises(ValueError):
            save_selection(self.settings, 'buttons')
        self.assertFalse(self.path.exists())

    def test_malformed_settings_fall_back(self):
        self.path.parent.mkdir()
        self.path.write_text('[]')
        self.assertEqual(self.settings.axis_mode('LX'), ControlMode.GAMEPAD)
        self.path.write_text('{"steering_mode": []}')
        self.assertEqual(self.settings.axis_mode('LX'), ControlMode.GAMEPAD)

    def fake_tk(self, action):
        state = {'buttons': {}, 'destroyed': False}
        class Widget:
            def __init__(self, *args, **kwargs):
                if 'command' in kwargs:
                    state['buttons'][kwargs['text']] = kwargs['command']
                if kwargs.get('text'):
                    state.setdefault('labels', []).append(kwargs['text'])
            def pack(self, *args, **kwargs): pass
            def configure(self, **kwargs): state.setdefault("updates", []).append(kwargs)
        class Window(Widget):
            def title(self, *args): pass
            def geometry(self, *args): pass
            def configure(self, **kwargs): pass
            def attributes(self, *args): pass
            def protocol(self, *args): pass
            def destroy(self): state['destroyed'] = True
            def mainloop(self): state['buttons'][action]()
            def after(self, delay, callback): callback()
        class Variable:
            def __init__(self, value): self.value = value
            def get(self): return self.value
            def set(self, value): self.value = value
        module = types.ModuleType('tkinter')
        module.Tk = Window
        module.Label = module.Frame = module.Button = module.Radiobutton = Widget
        module.StringVar = Variable
        module.messagebox = types.SimpleNamespace(showerror=lambda *a, **kw: None)
        return module, state

    def test_prelaunch_play_proceeds_without_touching_saved_mode(self):
        # Play without picking a mode writes nothing; existing saved values
        # stay exactly as the user left them.
        tk, state = self.fake_tk('Play')
        with patch.dict('sys.modules', {'tkinter': tk}):
            self.assertTrue(show_settings(self.path))
        self.assertTrue(state['destroyed'])
        self.assertFalse(self.path.exists())
        shown = ' '.join(state.get('labels', []))
        self.assertIn('Tilt Drive', shown)
        self.assertIn('Gamepad (sticks)', shown)
        self.assertIn('Keep the in-game Gamepad toggle ON', shown)
        self.assertNotIn('Gamepad OFF = Deck tilt', shown)
        self.assertIn('UNVERIFIED', shown)

    def test_prelaunch_pick_tilt_drive_persists_reversibly(self):
        # Picking Tilt Drive persists tilt; picking Gamepad restores sticks.
        tk, state = self.fake_tk('Tilt Drive')
        with patch.dict('sys.modules', {'tkinter': tk}):
            show_settings(self.path)
        self.assertEqual(self.settings.mode, ControlMode.TILT)
        tk, state = self.fake_tk('Gamepad (sticks)')
        with patch.dict('sys.modules', {'tkinter': tk}):
            show_settings(self.path)
        self.assertEqual(self.settings.mode, ControlMode.GAMEPAD)

    def test_prelaunch_never_saves_even_with_existing_file(self):
        self.settings.mode = ControlMode.TILT
        before = self.path.read_text()
        tk, state = self.fake_tk('Play')
        with patch.dict('sys.modules', {'tkinter': tk}):
            self.assertTrue(show_settings(self.path))
        self.assertEqual(self.path.read_text(), before)

    def test_live_return_closes_without_router_or_disk_writes(self):
        tk, state = self.fake_tk('Return to game')
        with patch.dict('sys.modules', {'tkinter': tk}), \
             patch('controls_settings.request_switch',
                   side_effect=AssertionError('guidance panel must not request')) , \
             patch('controls_settings.save_selection',
                   side_effect=AssertionError('guidance panel must not persist')):
            self.assertTrue(show_settings(self.path, Path(self.temp.name)))
        self.assertTrue(state['destroyed'])
        self.assertFalse(self.path.exists())

    def test_live_panel_shows_live_sensor_status(self):
        run_dir = Path(self.temp.name)
        (run_dir / 'control-mode.json').write_text(json.dumps({
            'mode': 'gamepad', 'requested_mode': 'gamepad',
            'sensor_available': True, 'error': None, 'request_id': None,
            'native_sensor': 'live', 'native_error': None}))
        tk, state = self.fake_tk('Return to game')
        with patch.dict('sys.modules', {'tkinter': tk}):
            self.assertTrue(show_settings(self.path, run_dir))
        shown = ' '.join(state.get('labels', []))
        self.assertIn('game tilt feed live', shown)

    def test_live_panel_shows_actionable_native_error(self):
        run_dir = Path(self.temp.name)
        (run_dir / 'control-mode.json').write_text(json.dumps({
            'mode': 'gamepad', 'requested_mode': 'gamepad',
            'sensor_available': True, 'error': None, 'request_id': None,
            'native_sensor': 'degraded', 'native_error': 'turn the GAME Gamepad ON'}))
        tk, state = self.fake_tk('Return to game')
        with patch.dict('sys.modules', {'tkinter': tk}):
            self.assertTrue(show_settings(self.path, run_dir))
        shown = ' '.join(state.get('labels', []))
        self.assertIn('Gamepad ON', shown)

    def test_sensor_summary_without_run_dir(self):
        summary = sensor_summary(None)
        self.assertIn('checked automatically', summary)

    def test_sensor_summary_unknown_status_file(self):
        summary = sensor_summary(Path(self.temp.name))
        self.assertIn('unavailable', summary)
        self.assertIn('stick', summary)

    def test_live_request_protocol_uses_owned_socket(self):
        with patch('controls_settings.socket.socket') as factory:
            request_id = request_switch(Path(self.temp.name), 'tilt')
        client = factory.return_value.__enter__.return_value
        client.connect.assert_called_once_with(str(Path(self.temp.name) / 'input.sock'))
        payload = json.loads(client.sendall.call_args.args[0])
        self.assertEqual(payload, {'type': 'control_mode', 'mode': 'tilt', 'request_id': request_id})
        self.assertFalse(self.path.exists())

    def test_status_requires_matching_acknowledgement(self):
        run_dir = Path(self.temp.name)
        status = run_dir / 'control-mode.json'
        status.write_text('{"mode":"tilt", "request_id":"old"}')
        self.assertIsNone(read_status(run_dir, 'new'))
        self.assertEqual(read_status(run_dir, 'old')['mode'], 'tilt')
        status.write_text('[]')
        self.assertIsNone(read_status(run_dir))

    def test_panel_cancel_closes_without_save(self):
        tk, state = self.fake_tk('Cancel')
        with patch.dict('sys.modules', {'tkinter': tk}):
            self.assertFalse(show_settings(self.path))
        self.assertTrue(state['destroyed'])
        self.assertFalse(self.path.exists())

    def test_entrypoint_passes_prelaunch_and_live_arguments_and_reports_ui_failure(self):
        import controls_settings
        with patch('controls_settings.show_settings', return_value=True) as shown:
            self.assertEqual(controls_settings.main(['--settings', str(self.path)]), controls_settings.PLAY)
        shown.assert_called_once_with(self.path, run_dir=None)
        with patch('controls_settings.show_settings', return_value=False) as shown:
            self.assertEqual(controls_settings.main(['--run-dir', self.temp.name]), controls_settings.CANCEL)
        shown.assert_called_once_with(None, run_dir=Path(self.temp.name))
        with patch('controls_settings.show_settings', side_effect=ImportError('libtk8.6.so')), \
             patch('controls_settings.traceback.print_exc'):
            self.assertEqual(controls_settings.main([]), controls_settings.PANEL_ERROR)


if __name__ == '__main__':
    unittest.main()
