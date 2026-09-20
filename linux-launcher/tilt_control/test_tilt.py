#!/usr/bin/env python3
"""Unit tests for tilt control: estimator, settings, adapter.

Tests orientations, signs, filter math, calibration, stale state,
mode switching, and mocked captures. Physical direction validation
must be reported pending user tilt.
"""

import contextlib
import io
import json
import math
import os
import struct
import tempfile
import time
import unittest

from .estimator import TiltEstimator, ACCEL_RESOLUTION, GYRO_RESOLUTION, STALE_TIMEOUT_S
from .motion_reader import (
    MotionReader, MotionSample, _discover_motion_node, discovery_snapshot,
)
from .settings import TiltSettings, ControlMode
from .adapter import TiltAdapter, run_cli, MAX_TILT_ANGLE_RAD
from .stick_gate import GATED_AXES, stick_gate_allows


def make_sample(
    acc_x=0, acc_y=0, acc_z=-16384,
    gyro_x=0, gyro_y=0, gyro_z=0,
    timestamp=1.0,
) -> MotionSample:
    s = MotionSample()
    s.acc_x = acc_x
    s.acc_y = acc_y
    s.acc_z = acc_z
    s.gyro_x = gyro_x
    s.gyro_y = gyro_y
    s.gyro_z = gyro_z
    s.timestamp = timestamp
    return s


class TestEstimatorOrientations(unittest.TestCase):
    """Verify roll/pitch signs for known physical orientations."""

    def test_flat_neutral(self):
        est = TiltEstimator(alpha=0.0)
        est.calibrate(0, 0, -16384)
        roll, pitch = est.update(0, 0, -16384, 0, 0, 0, 1.0)
        self.assertAlmostEqual(roll, 0.0, places=3)
        self.assertAlmostEqual(pitch, 0.0, places=3)

    def test_tilt_right_positive_roll(self):
        est = TiltEstimator(alpha=0.0)
        est.calibrate(0, 0, -16384)
        tilted_x = int(16384 * math.sin(math.radians(20)))
        tilted_z = int(-16384 * math.cos(math.radians(20)))
        roll, pitch = est.update(tilted_x, 0, tilted_z, 0, 0, 0, 1.0)
        self.assertGreater(roll, 0.0, "tilting right should give positive roll")
        self.assertAlmostEqual(roll, math.radians(20), delta=0.02)

    def test_tilt_left_negative_roll(self):
        est = TiltEstimator(alpha=0.0)
        est.calibrate(0, 0, -16384)
        tilted_x = int(-16384 * math.sin(math.radians(20)))
        tilted_z = int(-16384 * math.cos(math.radians(20)))
        roll, pitch = est.update(tilted_x, 0, tilted_z, 0, 0, 0, 1.0)
        self.assertLess(roll, 0.0, "tilting left should give negative roll")

    def test_tilt_forward_positive_pitch(self):
        est = TiltEstimator(alpha=0.0)
        est.calibrate(0, 0, -16384)
        tilted_y = int(16384 * math.sin(math.radians(15)))
        tilted_z = int(-16384 * math.cos(math.radians(15)))
        roll, pitch = est.update(0, tilted_y, tilted_z, 0, 0, 0, 1.0)
        self.assertGreater(pitch, 0.0, "tilting forward should give positive pitch")

    def test_tilt_backward_negative_pitch(self):
        est = TiltEstimator(alpha=0.0)
        est.calibrate(0, 0, -16384)
        tilted_y = int(-16384 * math.sin(math.radians(15)))
        tilted_z = int(-16384 * math.cos(math.radians(15)))
        roll, pitch = est.update(0, tilted_y, tilted_z, 0, 0, 0, 1.0)
        self.assertLess(pitch, 0.0)

    def test_calibration_offsets_natural_hold(self):
        """Calibrating at a tilted angle should zero that angle."""
        est = TiltEstimator(alpha=0.0)
        est.calibrate(510, 8680, -13985)
        roll, pitch = est.update(510, 8680, -13985, 0, 0, 0, 1.0)
        self.assertAlmostEqual(roll, 0.0, places=3)
        self.assertAlmostEqual(pitch, 0.0, places=3)

    def test_bounded_output(self):
        est = TiltEstimator(alpha=0.0)
        est.calibrate(0, 0, -16384)
        roll, pitch = est.update(16384, 16384, 0, 0, 0, 0, 1.0)
        self.assertLessEqual(abs(roll), math.pi / 2)
        self.assertLessEqual(abs(pitch), math.pi / 2)


class TestEstimatorFilter(unittest.TestCase):
    """Verify complementary filter math."""

    def test_pure_accel_when_alpha_zero(self):
        est = TiltEstimator(alpha=0.0)
        est.calibrate(0, 0, -16384)
        r1, _ = est.update(0, 0, -16384, 0, 0, 0, 1.0)
        tilted_x = int(16384 * math.sin(math.radians(10)))
        tilted_z = int(-16384 * math.cos(math.radians(10)))
        r2, _ = est.update(tilted_x, 0, tilted_z, 0, 0, 0, 1.004)
        self.assertAlmostEqual(r2, math.radians(10), delta=0.02)

    def test_gyro_integration_contribution(self):
        est = TiltEstimator(alpha=1.0)
        est.calibrate(0, 0, -16384)
        est.update(0, 0, -16384, 0, 0, 0, 1.0)
        gyro_x_raw = int(90 * GYRO_RESOLUTION)
        _, _ = est.update(0, 0, -16384, gyro_x_raw, 0, 0, 1.004)
        expected = math.radians(90) * 0.004
        self.assertAlmostEqual(est.roll, expected, delta=0.01)

    def test_blended_output(self):
        """Alpha=0.5 should blend accel and gyro equally."""
        est = TiltEstimator(alpha=0.5)
        est.calibrate(0, 0, -16384)
        est.update(0, 0, -16384, 0, 0, 0, 1.0)
        tilted_x = int(16384 * math.sin(math.radians(10)))
        tilted_z = int(-16384 * math.cos(math.radians(10)))
        roll, _ = est.update(tilted_x, 0, tilted_z, 0, 0, 0, 1.004)
        pure_accel = math.radians(10)
        self.assertGreater(roll, 0.0)
        self.assertLess(roll, pure_accel + 0.01)

    def test_large_dt_resets_to_accel(self):
        est = TiltEstimator(alpha=0.95)
        est.calibrate(0, 0, -16384)
        est.update(0, 0, -16384, 0, 0, 0, 1.0)
        tilted_x = int(16384 * math.sin(math.radians(15)))
        tilted_z = int(-16384 * math.cos(math.radians(15)))
        roll, _ = est.update(tilted_x, 0, tilted_z, 0, 0, 0, 1.1)
        self.assertAlmostEqual(roll, math.radians(15), delta=0.02)


class TestEstimatorStale(unittest.TestCase):

    def test_stale_when_no_updates(self):
        est = TiltEstimator()
        self.assertTrue(est.is_stale())

    def test_not_stale_after_recent_update(self):
        est = TiltEstimator()
        est.update(0, 0, -16384, 0, 0, 0, time.monotonic())
        self.assertFalse(est.is_stale())

    def test_stale_after_timeout(self):
        est = TiltEstimator()
        old = time.monotonic() - STALE_TIMEOUT_S - 0.1
        est.update(0, 0, -16384, 0, 0, 0, old)
        est._last_ts = old
        self.assertTrue(est.is_stale())


class TestEstimatorCalibration(unittest.TestCase):

    def test_calibrate_returns_offsets(self):
        est = TiltEstimator()
        cal = est.calibrate(510, 8680, -13985)
        self.assertIn("roll_offset", cal)
        self.assertIn("pitch_offset", cal)
        self.assertTrue(est.is_calibrated)

    def test_load_calibration(self):
        est = TiltEstimator()
        cal = est.calibrate(510, 8680, -13985)
        est2 = TiltEstimator()
        est2.load_calibration(cal)
        self.assertTrue(est2.is_calibrated)
        roll, pitch = est2.update(510, 8680, -13985, 0, 0, 0, 1.0)
        self.assertAlmostEqual(roll, 0.0, places=2)
        self.assertAlmostEqual(pitch, 0.0, places=2)

    def test_reset_to_neutral(self):
        est = TiltEstimator()
        est.calibrate(0, 0, -16384)
        tilted_x = int(16384 * math.sin(math.radians(20)))
        tilted_z = int(-16384 * math.cos(math.radians(20)))
        est.update(tilted_x, 0, tilted_z, 0, 0, 0, 1.0)
        self.assertNotEqual(est.roll, 0.0)
        est.reset_to_neutral()
        self.assertEqual(est.roll, 0.0)
        self.assertEqual(est.pitch, 0.0)

    def test_load_calibration_rejects_partial(self):
        """Partial/legacy calibration dicts must not crash update()."""
        est = TiltEstimator()
        self.assertFalse(est.load_calibration({"roll_offset": 0.1}))
        self.assertFalse(est.load_calibration({"pitch_offset": 0.1}))
        self.assertFalse(est.load_calibration({}))
        self.assertFalse(est.load_calibration(None))
        self.assertFalse(est.is_calibrated)
        # After a valid roundtrip, update() must not raise KeyError.
        cal = est.calibrate(510, 8680, -13985)
        self.assertTrue(est.load_calibration(cal))
        est.update(510, 8680, -13985, 0, 0, 0, 1.0)
        self.assertAlmostEqual(est.roll, 0.0, places=2)

    def test_alpha_property_clamps(self):
        est = TiltEstimator(alpha=5.0)
        self.assertEqual(est.alpha, 1.0)
        est.alpha = -2.0
        self.assertEqual(est.alpha, 0.0)
        est.alpha = 0.5
        self.assertEqual(est.alpha, 0.5)


class TestSettings(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._path = os.path.join(self._tmpdir, "test-tilt-settings.json")
        self.settings = TiltSettings(self._path)

    def tearDown(self):
        try:
            os.unlink(self._path)
        except OSError:
            pass
        os.rmdir(self._tmpdir)

    def test_default_mode_gamepad(self):
        self.assertEqual(self.settings.mode, ControlMode.GAMEPAD)

    def test_set_mode_persists(self):
        self.settings.mode = ControlMode.TILT
        s2 = TiltSettings(self._path)
        self.assertEqual(s2.mode, ControlMode.TILT)

    def test_calibration_roundtrip(self):
        cal = {"roll_offset": 0.1, "pitch_offset": 0.5, "acc_x": 500}
        self.settings.calibration = cal
        loaded = self.settings.calibration
        self.assertAlmostEqual(loaded["roll_offset"], 0.1)
        self.assertIn("timestamp", loaded)

    def test_calibration_none_default(self):
        self.assertIsNone(self.settings.calibration)

    def test_update_merges(self):
        self.settings.update(deadzone=0.15, tilt_gain_lx=1.5)
        self.assertAlmostEqual(self.settings.deadzone, 0.15)
        self.assertAlmostEqual(self.settings.tilt_gain_lx, 1.5)

    def test_deadzone_clamped(self):
        self.settings.update(deadzone=5.0)
        self.assertAlmostEqual(self.settings.deadzone, 0.99)
        self.settings.update(deadzone=-1.0)
        self.assertAlmostEqual(self.settings.deadzone, 0.0)

    def test_smoothing_alpha_clamped(self):
        self.settings.update(smoothing_alpha=3.0)
        self.assertAlmostEqual(self.settings.smoothing_alpha, 1.0)

    def test_update_rejects_unknown_key(self):
        with self.assertRaises(ValueError):
            self.settings.update(deadzone_typo=0.5)
        # Rejection must not have written anything.
        self.assertAlmostEqual(self.settings.deadzone, 0.10)

    def test_corrupt_file_returns_defaults(self):
        with open(self._path, "w") as f:
            f.write("NOT JSON{{{")
        self.assertEqual(self.settings.mode, ControlMode.GAMEPAD)
        self.assertAlmostEqual(self.settings.deadzone, 0.10)

    def test_as_dict(self):
        d = self.settings.as_dict()
        self.assertIn("mode", d)
        self.assertIn("deadzone", d)


class TestAdapterMocked(unittest.TestCase):
    """Adapter tests with mocked MotionReader (no real device access)."""

    def _make_adapter(self) -> TiltAdapter:
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "test-tilt.json")
        settings = TiltSettings(path)
        adapter = TiltAdapter(settings=settings, device_path="/dev/null")
        adapter._tmpdir = tmpdir
        adapter._tmppath = path
        return adapter

    def _cleanup(self, adapter):
        try:
            os.unlink(adapter._tmppath)
        except OSError:
            pass
        try:
            os.rmdir(adapter._tmpdir)
        except OSError:
            pass

    def test_gamepad_mode_returns_empty(self):
        adapter = self._make_adapter()
        adapter._open = True
        adapter.select_mode(ControlMode.GAMEPAD)
        events = adapter.consume_motion()
        self.assertEqual(events, [])
        self._cleanup(adapter)

    def test_tilt_mode_not_open_returns_empty(self):
        """TILT mode with no reader (device never opened) emits nothing."""
        adapter = self._make_adapter()
        adapter.select_mode(ControlMode.TILT)
        adapter._open = False
        self.assertEqual(adapter.consume_motion(), [])
        self.assertEqual(adapter.axes, {"LX": 0.0, "LY": 0.0})
        self._cleanup(adapter)

    def test_tilt_mode_produces_lx_ly(self):
        adapter = self._make_adapter()
        adapter.select_mode(ControlMode.TILT)
        adapter._open = True
        adapter._estimator.calibrate(0, 0, -16384)
        tilted_x = int(16384 * math.sin(math.radians(20)))
        tilted_z = int(-16384 * math.cos(math.radians(20)))
        samples = [make_sample(acc_x=tilted_x, acc_z=tilted_z, timestamp=time.monotonic())]

        class FakeReader:
            device_path = "/dev/null"
            def read_samples(self_):
                return samples
            def close(self_):
                pass
            def open(self_):
                return True
            @property
            def is_open(self_):
                return True

        adapter._reader = FakeReader()
        events = adapter.consume_motion()
        axes = {e["axis"]: e["value"] for e in events}
        self.assertIn("LX", axes)
        self.assertGreater(axes["LX"], 0.0, "right tilt -> positive LX")
        self._cleanup(adapter)

    def test_tilt_mode_emits_only_lx_ly(self):
        """Selector scope: TILT mode must never emit non-LX/LY axes."""
        adapter = self._make_adapter()
        adapter.select_mode(ControlMode.TILT)
        adapter._open = True
        adapter._estimator.calibrate(0, 0, -16384)
        tilted_x = int(16384 * math.sin(math.radians(15)))
        tilted_z = int(-16384 * math.cos(math.radians(15)))
        samples = [make_sample(acc_x=tilted_x, acc_z=tilted_z, timestamp=time.monotonic())]

        class FakeReader:
            device_path = "/dev/null"
            def read_samples(self_):
                return samples

        adapter._reader = FakeReader()
        events = adapter.consume_motion()
        self.assertTrue(len(events) > 0)
        self.assertTrue(all(e["type"] == "axis" and e["axis"] in ("LX", "LY") for e in events))
        self._cleanup(adapter)

    def test_dynamic_alpha_syncs_from_settings(self):
        """A persisted smoothing_alpha change must take effect at runtime."""
        adapter = self._make_adapter()
        adapter.select_mode(ControlMode.TILT)
        adapter._open = True
        adapter.settings.update(smoothing_alpha=0.1)

        class EmptyReader:
            device_path = "/dev/null"
            def read_samples(self_):
                return []

        adapter._reader = EmptyReader()
        adapter.consume_motion()
        self.assertAlmostEqual(adapter._estimator.alpha, 0.1)
        self._cleanup(adapter)

    def test_load_calibration_validates_on_init(self):
        """Adapter must tolerate a corrupt/partial persisted calibration."""
        adapter = self._make_adapter()
        adapter.settings.update(calibration={"roll_offset": 0.1})
        adapter2 = TiltAdapter(
            settings=TiltSettings(adapter._tmppath), device_path="/dev/null")
        self.assertFalse(adapter2._estimator.is_calibrated)
        adapter2._estimator.update(0, 0, -16384, 0, 0, 0, 1.0)  # must not raise
        adapter2._tmpdir = adapter._tmpdir
        adapter2._tmppath = adapter._tmppath
        self._cleanup(adapter2)
        self._cleanup(adapter)
        adapter = self._make_adapter()
        adapter.select_mode(ControlMode.TILT)
        adapter._open = True
        adapter._estimator.calibrate(0, 0, -16384)
        tilted_x = int(16384 * math.sin(math.radians(20)))
        tilted_z = int(-16384 * math.cos(math.radians(20)))
        samples = [make_sample(acc_x=tilted_x, acc_z=tilted_z, timestamp=time.monotonic())]

        class FakeReader:
            device_path = "/dev/null"
            def read_samples(self_):
                return samples
            def close(self_):
                pass
            def open(self_):
                return True
            @property
            def is_open(self_):
                return True

        adapter._reader = FakeReader()
        events = adapter.consume_motion()
        axes = {e["axis"]: e["value"] for e in events}
        self.assertIn("LX", axes)
        self.assertGreater(axes["LX"], 0.0, "right tilt -> positive LX")
        self._cleanup(adapter)

    def test_mode_switch_claims_both_axes_together(self):
        adapter = self._make_adapter()
        try:
            adapter._open = True
            adapter.select_mode(ControlMode.GAMEPAD)
            self.assertEqual(adapter.controlled_axes, frozenset())
            adapter.select_mode(ControlMode.TILT)
            self.assertEqual(adapter.controlled_axes, frozenset(("LX", "LY")))
            adapter._lx, adapter._ly = 0.5, -0.5
            adapter.select_mode(ControlMode.GAMEPAD)
            self.assertEqual(adapter.controlled_axes, frozenset())
            self.assertEqual(adapter.axes, {"LX": 0.0, "LY": 0.0})
            self.assertEqual(adapter.consume_motion(), [])
        finally:
            self._cleanup(adapter)

    def test_unavailable_sensor_keeps_both_stick_axes_authoritative(self):
        from unittest import mock
        adapter = self._make_adapter()
        try:
            adapter.select_mode(ControlMode.TILT)
            with mock.patch.object(adapter._reader, 'open', return_value=False):
                self.assertFalse(adapter.open())
            self.assertEqual(adapter.controlled_axes, frozenset())
            self.assertEqual(adapter.consume_motion(), [])
        finally:
            self._cleanup(adapter)

    def test_stale_forces_neutral(self):
        adapter = self._make_adapter()
        adapter.select_mode(ControlMode.TILT)
        adapter._open = True
        adapter._lx = 0.5
        adapter._ly = 0.3
        adapter._estimator._last_ts = time.monotonic() - 1.0

        class EmptyReader:
            device_path = "/dev/null"
            def read_samples(self_):
                return []
            def close(self_):
                pass

        adapter._reader = EmptyReader()
        events = adapter.consume_motion()
        axes = {e["axis"]: e["value"] for e in events}
        self.assertEqual(axes.get("LX", 0.0), 0.0)
        self.assertEqual(axes.get("LY", 0.0), 0.0)
        self._cleanup(adapter)

    def test_mode_switch_zeros_axes(self):
        adapter = self._make_adapter()
        adapter._lx = 0.5
        adapter._ly = 0.3
        adapter.select_mode(ControlMode.GAMEPAD)
        self.assertEqual(adapter._lx, 0.0)
        self.assertEqual(adapter._ly, 0.0)
        self.assertEqual(adapter.mode, ControlMode.GAMEPAD)
        self._cleanup(adapter)

    def test_mode_persists(self):
        adapter = self._make_adapter()
        adapter.select_mode(ControlMode.TILT)
        s2 = TiltSettings(adapter._tmppath)
        self.assertEqual(s2.mode, ControlMode.TILT)
        self._cleanup(adapter)

    def test_deadzone_applied(self):
        val = TiltAdapter._apply_deadzone_and_clamp(0.05, 0.10)
        self.assertEqual(val, 0.0)
        val2 = TiltAdapter._apply_deadzone_and_clamp(0.55, 0.10)
        self.assertGreater(val2, 0.0)
        self.assertLessEqual(val2, 1.0)

    def test_deadzone_degenerate_ranges(self):
        """Out-of-range persisted deadzone must never divide by zero."""
        self.assertEqual(TiltAdapter._apply_deadzone_and_clamp(0.9, 1.0), 0.0)
        self.assertEqual(TiltAdapter._apply_deadzone_and_clamp(-0.9, 1.0), 0.0)
        self.assertAlmostEqual(TiltAdapter._apply_deadzone_and_clamp(0.5, 0.0), 0.5)
        self.assertAlmostEqual(TiltAdapter._apply_deadzone_and_clamp(-0.5, -1.0), -0.5)
        self.assertAlmostEqual(TiltAdapter._apply_deadzone_and_clamp(2.0, 0.0), 1.0)

    def test_clamp_at_one(self):
        val = TiltAdapter._apply_deadzone_and_clamp(2.0, 0.10)
        self.assertEqual(val, 1.0)
        val_neg = TiltAdapter._apply_deadzone_and_clamp(-2.0, 0.10)
        self.assertEqual(val_neg, -1.0)

    def test_calibrate_from_samples(self):
        adapter = self._make_adapter()
        adapter._open = True
        samples = [make_sample(acc_x=510, acc_y=8680, acc_z=-13985, timestamp=1.0 + i * 0.004) for i in range(50)]
        cal = adapter.calibrate(samples=samples)
        self.assertIsNotNone(cal)
        self.assertIn("roll_offset", cal)
        self.assertIn("sample_count", cal)
        self.assertEqual(cal["sample_count"], 50)
        self._cleanup(adapter)

    def test_health_report(self):
        adapter = self._make_adapter()
        h = adapter.health()
        self.assertIn("mode", h)
        self.assertIn("device_open", h)
        self.assertIn("calibrated", h)
        self.assertIn("stale", h)
        self.assertIn("lx", h)
        self.assertIn("ly", h)
        self._cleanup(adapter)

    def test_axes_property(self):
        adapter = self._make_adapter()
        a = adapter.axes
        self.assertEqual(a["LX"], 0.0)
        self.assertEqual(a["LY"], 0.0)
        self._cleanup(adapter)

    def test_close_zeros_and_resets(self):
        adapter = self._make_adapter()
        adapter._lx = 0.8
        adapter._ly = 0.3
        adapter._open = True
        adapter.close()
        self.assertFalse(adapter._open)
        self.assertEqual(adapter._lx, 0.0)
        self.assertEqual(adapter._ly, 0.0)
        self._cleanup(adapter)

    def test_left_tilt_negative_lx(self):
        adapter = self._make_adapter()
        adapter.select_mode(ControlMode.TILT)
        adapter._open = True
        adapter._estimator.calibrate(0, 0, -16384)
        tilted_x = int(-16384 * math.sin(math.radians(20)))
        tilted_z = int(-16384 * math.cos(math.radians(20)))
        samples = [make_sample(acc_x=tilted_x, acc_z=tilted_z, timestamp=time.monotonic())]

        class FakeReader:
            device_path = "/dev/null"
            def read_samples(self_):
                return samples

        adapter._reader = FakeReader()
        events = adapter.consume_motion()
        axes = {e["axis"]: e["value"] for e in events}
        self.assertIn("LX", axes)
        self.assertLess(axes["LX"], 0.0, "left tilt -> negative LX")
        self._cleanup(adapter)


class TestPollNative(unittest.TestCase):
    """Native-feed estimator pump: the runner's always-on path.

    Regression for the Sept-14 root cause #2: the runner called
    ``sensor_acceleration()`` without ever feeding IMU samples into the
    estimator, so the guest vector was frozen at neutral and physical
    rotation could never reach the game. ``poll_native()`` is the
    runner-called feed; it must move the vector with controlled input,
    never emit synthetic axis events, and work in every View mode.
    """

    def _make_adapter(self) -> TiltAdapter:
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "test-poll-native.json")
        settings = TiltSettings(path)
        adapter = TiltAdapter(settings=settings, device_path="/dev/null")
        adapter._tmpdir = tmpdir
        adapter._tmppath = path
        return adapter

    def _cleanup(self, adapter):
        try:
            os.unlink(adapter._tmppath)
        except OSError:
            pass
        try:
            os.rmdir(adapter._tmpdir)
        except OSError:
            pass

    def test_poll_feeds_estimator_and_moves_sensor_vector(self):
        """Controlled input (tilted samples) changes the native vector."""
        adapter = self._make_adapter()
        adapter._open = True
        tilted_x = int(-16384 * math.sin(math.radians(20)))
        tilted_z = int(-16384 * math.cos(math.radians(20)))
        samples = [make_sample(acc_x=tilted_x, acc_z=tilted_z,
                               timestamp=time.monotonic() + i * 0.004)
                   for i in range(5)]

        class FakeReader:
            device_path = "/dev/null"
            last_error = None

            def read_samples(self_):
                return samples

        adapter._reader = FakeReader()
        before = adapter.sensor_acceleration()
        self.assertTrue(adapter.poll_native())
        after = adapter.sensor_acceleration()
        # Roll moved: ax must differ from the neutral vector.
        self.assertNotAlmostEqual(after[0], before[0], places=3)
        # No synthetic axis state is touched by the native poll.
        self.assertEqual(adapter.axes, {"LX": 0.0, "LY": 0.0})
        self.assertEqual(adapter.consume_motion(), [])
        self._cleanup(adapter)

    def test_poll_returns_false_when_closed_without_touching_reader(self):
        from unittest import mock as _mock
        adapter = self._make_adapter()
        adapter._reader = _mock.Mock()
        self.assertFalse(adapter.poll_native())
        adapter._reader.read_samples.assert_not_called()
        self._cleanup(adapter)

    def test_poll_empty_keeps_estimator_untouched(self):
        from unittest import mock as _mock
        adapter = self._make_adapter()
        adapter._open = True
        adapter._reader = _mock.Mock()
        adapter._reader.read_samples.return_value = []
        roll_before = adapter._estimator.roll
        self.assertFalse(adapter.poll_native())
        self.assertEqual(adapter._estimator.roll, roll_before)
        self._cleanup(adapter)

    def test_poll_runs_in_gamepad_mode_for_always_on_feed(self):
        """The native feed is mode-independent; View GAMEPAD still polls."""
        adapter = self._make_adapter()
        self.assertEqual(adapter.mode, ControlMode.GAMEPAD)
        adapter._open = True
        samples = [make_sample(acc_x=2000, acc_z=-15000,
                               timestamp=time.monotonic())]

        class FakeReader:
            device_path = "/dev/null"
            last_error = None

            def read_samples(self_):
                return samples

        adapter._reader = FakeReader()
        self.assertTrue(adapter.poll_native())
        self.assertNotEqual(adapter._estimator.roll, 0.0)
        self._cleanup(adapter)


class TestMotionSample(unittest.TestCase):

    def test_default_values(self):
        s = MotionSample()
        self.assertEqual(s.acc_x, 0)
        self.assertEqual(s.acc_y, 0)
        self.assertEqual(s.acc_z, 0)
        self.assertEqual(s.gyro_x, 0)
        self.assertEqual(s.gyro_y, 0)
        self.assertEqual(s.gyro_z, 0)
        self.assertEqual(s.timestamp, 0.0)


class TestMotionReader(unittest.TestCase):
    """Parsing / timestamp normalization without a real IMU device."""

    @staticmethod
    def _frame_buffer(kernel_ts: float, base_value: int = 1000) -> bytes:
        sec = int(kernel_ts)
        usec = int(round((kernel_ts - sec) * 1_000_000))
        fmt = struct.Struct("llHHi")
        parts = []
        for code in range(6):  # acc_x..gyro_z
            parts.append(fmt.pack(sec, usec, 3, code, base_value))
        parts.append(fmt.pack(sec, usec, 0, 0, 0))  # SYN_REPORT
        return b"".join(parts)

    def test_rebases_kernel_timestamps_to_monotonic(self):
        """Kernel wall-clock stamps must not poison monotonic staleness."""
        reader = MotionReader()
        r, w = os.pipe()
        try:
            os.write(w, self._frame_buffer(1_770_000_000.42))
            reader._fd = r
            samples = reader.read_samples()
            self.assertEqual(len(samples), 1)
            s = samples[0]
            self.assertEqual(s.acc_x, 1000)
            self.assertEqual(s.gyro_z, 1000)
            # Re-based to the monotonic clock: within 0.5s of right now.
            self.assertLessEqual(abs(s.timestamp - time.monotonic()), 0.5)
        finally:
            os.close(r)
            os.close(w)

    def test_await_motion_requires_real_frame_from_opened_node(self):
        """An openable but silent node is not Tilt readiness."""
        r, w = os.pipe()
        os.set_blocking(r, False)
        adapter = TiltAdapter(settings=TiltSettings(os.path.join(tempfile.mkdtemp(), "t.json")))
        try:
            self.assertFalse(adapter.await_motion(0.0))  # never opened
            adapter._reader._fd = r
            adapter._open = True
            started = time.monotonic()
            self.assertFalse(adapter.await_motion(0.05))
            self.assertLess(time.monotonic() - started, 0.5)
            os.write(w, self._frame_buffer(1_770_000_000.0))
            self.assertTrue(adapter.await_motion(0.05))
        finally:
            adapter._reader._fd = None
            os.close(r)
            os.close(w)

    def test_coalesces_multiple_frames(self):
        reader = MotionReader()
        r, w = os.pipe()
        try:
            os.write(w, self._frame_buffer(1_770_000_000.0)
                     + self._frame_buffer(1_770_000_000.02))
            reader._fd = r
            samples = reader.read_samples()
            self.assertEqual(len(samples), 2)
        finally:
            os.close(r)
            os.close(w)

    def test_empty_read_returns_empty(self):
        import fcntl
        reader = MotionReader()
        r, w = os.pipe()
        try:
            fcntl.fcntl(r, fcntl.F_SETFL, os.O_NONBLOCK)
            reader._fd = r
            self.assertEqual(reader.read_samples(), [])
        finally:
            os.close(r)
            os.close(w)

    def test_partial_group_holds_last_axis_values(self):
        """Sept-14 capture bug: evdev suppresses unchanged axes per SYN
        group (73% stillness zeros). Missing axes must hold last-known,
        never reset to 0."""
        import fcntl
        reader = MotionReader()
        r, w = os.pipe()
        try:
            fcntl.fcntl(r, fcntl.F_SETFL, os.O_NONBLOCK)
            reader._fd = r
            full = self._frame_buffer(1_770_000_000.0, base_value=1000)
            os.write(w, full)
            first = reader.read_samples()
            self.assertEqual(len(first), 1)
            self.assertEqual(
                (first[0].acc_x, first[0].acc_y, first[0].acc_z,
                 first[0].gyro_x, first[0].gyro_y, first[0].gyro_z),
                (1000,) * 6)
            # Group carrying only code 0 (X changed): rest must hold.
            fmt = struct.Struct("llHHi")
            sec, usec = 1770000000, 40000
            os.write(w, fmt.pack(sec, usec, 3, 0, 1200)
                     + fmt.pack(sec, usec, 0, 0, 0))
            second = reader.read_samples()
            self.assertEqual(len(second), 1)
            self.assertEqual(second[0].acc_x, 1200)
            self.assertEqual(
                (second[0].acc_y, second[0].acc_z, second[0].gyro_x,
                 second[0].gyro_y, second[0].gyro_z),
                (1000,) * 5)
        finally:
            reader._fd = None
            os.close(r)
            os.close(w)

    def test_no_emit_before_all_axes_initialized(self):
        """First groups must not emit until acc xyz + gyro xyz all seen."""
        import fcntl
        reader = MotionReader()
        r, w = os.pipe()
        try:
            fcntl.fcntl(r, fcntl.F_SETFL, os.O_NONBLOCK)
            reader._fd = r
            fmt = struct.Struct("llHHi")
            sec, usec = 1770000000, 40000
            os.write(w, fmt.pack(sec, usec, 3, 0, 1200)
                     + fmt.pack(sec, usec, 0, 0, 0))
            self.assertEqual(reader.read_samples(), [])
            for code in range(1, 6):
                os.write(w, fmt.pack(sec, usec + code, 3, code, 500 + code)
                         + fmt.pack(sec, usec + code, 0, 0, 0))
            samples = reader.read_samples()
            # Only the final group completes all-six initialization.
            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[-1].acc_x, 1200)
        finally:
            reader._fd = None
            os.close(r)
            os.close(w)

    def test_syn_dropped_resets_and_regates(self):
        """SYN_DROPPED drops framing state; emission waits for re-seen axes."""
        import fcntl
        reader = MotionReader()
        r, w = os.pipe()
        try:
            fcntl.fcntl(r, fcntl.F_SETFL, os.O_NONBLOCK)
            reader._fd = r
            fmt = struct.Struct("llHHi")
            sec, usec = 1770000000, 40000
            os.write(w, self._frame_buffer(1770000000.0, base_value=1000))
            self.assertEqual(len(reader.read_samples()), 1)
            os.write(w, fmt.pack(sec, usec, 0, 3, 0))  # SYN_DROPPED
            os.write(w, fmt.pack(sec, usec, 3, 0, 1200)
                     + fmt.pack(sec, usec, 0, 0, 0))
            self.assertEqual(reader.read_samples(), [])  # X alone: gated
            self.assertIsNotNone(reader.last_error)
            for code in range(1, 6):
                os.write(w, fmt.pack(sec, usec + code, 3, code, 700 + code)
                         + fmt.pack(sec, usec + code, 0, 0, 0))
            samples = reader.read_samples()
            self.assertTrue(samples)
            self.assertEqual(samples[-1].acc_x, 1200)
            self.assertEqual(samples[-1].acc_y, 701)
        finally:
            reader._fd = None
            os.close(r)
            os.close(w)

    def test_close_clears_held_axes(self):
        """Disconnect/reopen must not resurrect stale held values."""
        reader = MotionReader(device_path="/dev/definitely-not-jcs2-tilt-node")
        reader._last_axes = {0: 9999}
        reader._seen_ever = {0, 1, 2, 3, 4, 5}
        reader.close()
        self.assertEqual(reader._last_axes, {})
        self.assertEqual(reader._seen_ever, set())

    def test_verbatim_zeros_snap_attitude_held_values_do_not(self):
        """Differential proof on real capture rows: artifact zeros land in
        the estimator's atan2 denominators (+-90 deg snaps); hold-last
        stays near neutral. Same values, verbatim vs suppressed groups."""
        import fcntl
        rows = [(2208, 0, 108, 0, 0, 0), (0, 16492, 0, 2, 0, -2),
                (0, 16519, 102, 4, 0, 0), (0, 16526, 97, 0, 0, 0),
                (2234, 0, 0, 2, 0, 0), (2340, 16438, 101, 0, 0, 0),
                (2230, 16458, 107, 0, 0, -2), (2234, 16549, 100, 0, 0, 0),
                (0, 0, 95, 0, 0, 0), (0, 16555, 116, 0, 0, 0),
                (2257, 16548, 47, 0, 0, 0), (2276, 16526, 73, 0, 2, 0)]
        fmt = struct.Struct("llHHi")
        base = 1770000000.0

        def run(verbatim):
            reader = MotionReader()
            r, w = os.pipe()
            try:
                fcntl.fcntl(r, fcntl.F_SETFL, os.O_NONBLOCK)
                reader._fd = r
                blob = bytearray()
                for index, row in enumerate(rows):
                    sec = int(base + index * 0.004)
                    usec = int(round((base + index * 0.004 - sec) * 1_000_000))
                    firing = False
                    for code, value in enumerate(tuple(row) + (0, 0, 0)):
                        if verbatim or value != 0:
                            blob += fmt.pack(sec, usec, 3, code, value)
                            firing = True
                    if firing:
                        blob += fmt.pack(sec, usec, 0, 0, 0)
                os.write(w, bytes(blob))
                estimator = TiltEstimator(alpha=0.92)
                estimator.calibrate(2260, 16500, 90)
                samples = []
                while True:
                    batch = reader.read_samples()
                    if not batch:
                        break
                    samples += batch
                roll = pitch = 0.0
                for position, sample in enumerate(samples):
                    roll, pitch = estimator.update(
                        sample.acc_x, sample.acc_y, sample.acc_z,
                        sample.gyro_x, sample.gyro_y, sample.gyro_z,
                        10.0 + position * 0.004)
                return samples, roll, pitch
            finally:
                reader._fd = None
                os.close(r)
                os.close(w)

        verbatim_samples, verbatim_roll, _ = run(True)
        held_samples, held_roll, _ = run(False)
        self.assertEqual(len(verbatim_samples), 12)
        self.assertGreater(abs(math.degrees(verbatim_roll)), 10.0)
        self.assertLess(abs(math.degrees(held_roll)), 5.0)

    def test_open_nonexistent_device_returns_false(self):
        """A missing evdev node must fail closed, never raise."""
        reader = MotionReader(device_path="/dev/definitely-not-jcs2-tilt-node")
        self.assertFalse(reader.open())
        self.assertFalse(reader.is_open)
        self.assertIsNone(reader.fd)
        self.assertEqual(reader.read_samples(), [])  # closed reader -> empty

    def test_open_failure_records_path_and_errno(self):
        """Sept-14 diagnostics: WHY the IMU is not owned must be logged."""
        reader = MotionReader(device_path="/dev/definitely-not-jcs2-tilt-node")
        self.assertFalse(reader.open())
        self.assertIsNotNone(reader.last_error)
        self.assertIn("/dev/definitely-not-jcs2-tilt-node", reader.last_error)
        self.assertIn("errno", reader.last_error)

    def test_open_success_clears_error(self):
        import unittest.mock as mock
        reader = MotionReader(device_path="/dev/definitely-not-jcs2-tilt-node")
        self.assertFalse(reader.open())
        self.assertIsNotNone(reader.last_error)
        r, w = os.pipe()
        try:
            reader._device_path = None
            # Simulate a successful open path without touching hardware.
            with mock.patch("os.open", return_value=r):
                with mock.patch(
                        "tilt_control.motion_reader._discover_motion_node",
                        return_value=("/dev/fake-event", "tier1", "")):
                    self.assertTrue(reader.open())
            self.assertIsNone(reader.last_error)
            reader._fd = None  # hand fd ownership back to this test
        finally:
            os.close(r)
            os.close(w)

    def test_discovery_miss_records_reason(self):
        import unittest.mock as _mock
        reader = MotionReader()
        with _mock.patch(
                "tilt_control.motion_reader._discover_motion_node",
                return_value=(None, "miss", "nodes=0 ")):
            self.assertFalse(reader.open())
        self.assertIsNotNone(reader.last_error)
        self.assertIn("not found", reader.last_error)
        self.assertEqual(reader.match_tier, "miss")

    def test_health_carries_device_error(self):
        adapter = TiltAdapter(
            settings=TiltSettings(os.path.join(tempfile.mkdtemp(), "h.json")),
            device_path="/dev/definitely-not-jcs2-tilt-node")
        self.assertFalse(adapter.open())
        h = adapter.health()
        self.assertIn("device_error", h)
        self.assertIsNotNone(h["device_error"])

    def test_incomplete_frame_held_for_next_read(self):
        """ABS events without SYN_REPORT are retained across reads."""
        reader = MotionReader()
        r, w = os.pipe()
        try:
            fmt = struct.Struct("llHHi")
            sec, usec = 1_770_000_000, 420_000
            # First read: only acc_x has arrived (no SYN yet).
            os.write(w, fmt.pack(sec, usec, 3, 0, 500))
            reader._fd = r
            self.assertEqual(reader.read_samples(), [])
            # Second read: the REMAINING events of the same frame close it.
            tail = b"".join(
                fmt.pack(sec, usec, 3, code, 1000) for code in range(1, 6)
            ) + fmt.pack(sec, usec, 0, 0, 0)
            os.write(w, tail)
            samples = reader.read_samples()
            self.assertEqual(len(samples), 1)
            # acc_x from the earlier ABS event survives into the frame.
            self.assertEqual(samples[0].acc_x, 500)
            self.assertEqual(samples[0].gyro_z, 1000)
        finally:
            os.close(r)
            os.close(w)


class TestCli(unittest.TestCase):
    """Selector / status CLI against a temporary settings file."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._path = os.path.join(self._tmpdir, "cli-tilt.json")

    def tearDown(self):
        for p in (self._path, self._path + ".tmp"):
            try:
                os.unlink(p)
            except OSError:
                pass
        os.rmdir(self._tmpdir)

    def test_select_persists_mode_roundtrip(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_cli(["--settings", self._path, "select", "tilt"])
        self.assertEqual(rc, 0)
        self.assertEqual(TiltSettings(self._path).mode, ControlMode.TILT)
        with contextlib.redirect_stdout(buf):
            rc = run_cli(["--settings", self._path, "select", "gamepad"])
        self.assertEqual(rc, 0)
        self.assertEqual(TiltSettings(self._path).mode, ControlMode.GAMEPAD)

    def test_select_rejects_unknown_mode(self):
        with self.assertRaises(SystemExit):
            run_cli(["--settings", self._path, "select", "banana"])

    def test_status_prints_state(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_cli(["--settings", self._path, "status"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("gamepad", out)
        self.assertIn("calibrated", out)
        self.assertIn("scope", out)

    def test_status_after_calibration_persist(self):
        cal = {"roll_offset": 0.1, "pitch_offset": -0.3, "sample_count": 50}
        TiltSettings(self._path).calibration = cal
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_cli(["--settings", self._path, "status"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn('"calibrated": true', out)
        self.assertIn("50", out)

    def test_calibrate_missing_device_fails_cleanly(self):
        """calibrate with an unusable device must exit non-zero without raising."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_cli([
                "--settings", self._path,
                "calibrate", "--device", "/dev/definitely-not-jcs2-tilt-node",
            ])
        self.assertNotEqual(rc, 0)
        # Nothing persisted on the failed attempt.
        self.assertIsNone(TiltSettings(self._path).calibration)


class FakeConsoleSocket:
    """Minimal fake emulator-console socket: scripted greeting + OK replies."""

    def __init__(self, greeting=b"Android Console: Authentication required\n", fail_send=False):
        self.greeting = greeting
        self.sent: list[bytes] = []
        self.fail_send = fail_send
        self.closed = False
        self._replies = [greeting]

    def sendall(self, data: bytes) -> None:
        if self.fail_send:
            raise OSError("fake send failure")
        self.sent.append(data)
        if data.startswith(b"auth "):
            self._replies.append(b"OK\n")
        elif data.startswith(b"sensor set"):
            self._replies.append(b"OK\n")
        elif data.startswith(b"sensor status"):
            self._replies.append(b"acceleration: 0:0:0\nOK\n")
        else:
            self._replies.append(b"OK\n")

    def recv(self, limit: int) -> bytes:
        if self._replies:
            return self._replies.pop(0)
        return b"OK\n"

    def close(self) -> None:
        self.closed = True


def _fake_connect(sock):
    def connect(host, port, timeout):
        self_holder = getattr(connect, "_holder", None)
        return sock
    return connect


class TestSensorMapping(unittest.TestCase):
    """Deck roll/pitch -> Android accelerometer vector (no devices)."""

    def test_neutral_is_level_gravity(self):
        from .sensor_transport import acceleration_from_tilt, NEUTRAL_ACCELERATION, GRAVITY
        self.assertEqual(acceleration_from_tilt(0.0, 0.0), (0.0, 0.0, GRAVITY))

    def test_parked_pose_is_a_level_gravity_with_a_deterministic_roll(self):
        # The parked pose is deliberately NOT dead flat: gravity on +Z alone is
        # ambiguous for the framework's landscape quarter, so the park carries a
        # ~2 degree roll (measured 2026-09-18: one console rotate flipped the
        # guest display 1 -> 3 with the display pin in place). Magnitude stays one
        # g so the game's own tilt reading stays neutral.
        from .sensor_transport import NEUTRAL_ACCELERATION, GRAVITY, PARKED_ROLL_DEGREES
        ax, ay, az = NEUTRAL_ACCELERATION
        self.assertLess(ax, 0.0)                       # negative X -> landscape quarter 1
        self.assertEqual(ay, 0.0)
        self.assertGreater(az, 0.0)
        self.assertAlmostEqual(math.dist((0.0, 0.0, 0.0), NEUTRAL_ACCELERATION), GRAVITY, delta=0.02)
        roll = math.degrees(math.atan2(abs(ax), az))
        self.assertAlmostEqual(roll, PARKED_ROLL_DEGREES, delta=0.2)
        self.assertLess(roll, 5.0)                     # inside any input dead zone

    def test_roll_right_positive_ax(self):
        from .sensor_transport import acceleration_from_tilt, GRAVITY
        ax, _, _ = acceleration_from_tilt(math.radians(20), 0.0)
        self.assertGreater(ax, 0.0)
        self.assertAlmostEqual(ax, math.sin(math.radians(20)) * GRAVITY, places=6)

    def test_pitch_forward_positive_ay(self):
        from .sensor_transport import acceleration_from_tilt, GRAVITY
        _, ay, _ = acceleration_from_tilt(0.0, math.radians(15))
        self.assertGreater(ay, 0.0)
        self.assertAlmostEqual(ay, math.sin(math.radians(15)) * GRAVITY, places=6)

    def test_gains_scale_angle(self):
        from .sensor_transport import acceleration_from_tilt
        full = acceleration_from_tilt(0.3, 0.0, gain_lx=1.0)
        half = acceleration_from_tilt(0.3, 0.0, gain_lx=0.5)
        self.assertGreater(full[0], half[0])
        self.assertGreater(half[0], 0.0)

    def test_negative_gain_flips_axis(self):
        from .sensor_transport import acceleration_from_tilt
        _, ay, _ = acceleration_from_tilt(0.0, 0.3, gain_ly=-1.0)
        self.assertLess(ay, 0.0)

    def test_format_command_shape(self):
        from .sensor_transport import format_sensor_set
        self.assertEqual(format_sensor_set((0.0, 0.0, 9.81)), b"sensor set acceleration 0.000:0.000:9.810\n")
        with self.assertRaises(ValueError):
            format_sensor_set((float("nan"), 0.0, 9.81))
        with self.assertRaises(ValueError):
            format_sensor_set((500.0, 0.0, 0.0))

    def test_free_fall_zero_vector_never_emitted(self):
        # (0,0,0) would corrupt the game's calibration neutral; the
        # mapping below always carries ~g so only a bug could produce it.
        from .sensor_transport import format_sensor_set
        with self.assertRaises(ValueError):
            format_sensor_set((0.0, 0.0, 0.0))

    def test_mapping_magnitude_carries_gravity(self):
        # Android flat-screen-up convention: neutral is exactly +g on Z
        # and the operating range stays near-g (the game normalizes the
        # vector itself, so direction is what matters; never zero-g).
        import math as _math
        from .sensor_transport import acceleration_from_tilt, GRAVITY
        self.assertEqual(acceleration_from_tilt(0.0, 0.0), (0.0, 0.0, GRAVITY))
        for roll, pitch in ((0.3, -0.2), (0.6, 0.6), (-0.5, 0.4)):
            ax, ay, az = acceleration_from_tilt(roll, pitch)
            mag = _math.dist((ax, ay, az), (0.0, 0.0, 0.0))
            self.assertAlmostEqual(mag, GRAVITY, delta=0.06 * GRAVITY)
            self.assertGreater(az, 0.0)
            self.assertGreater(_math.dist((ax, ay, az), (0.0, 0.0, 0.0)), 1.0)

    def test_adapter_sensor_acceleration_uses_live_angles(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: os.rmdir(tmpdir))
        path = os.path.join(tmpdir, "sensor-gain.json")
        settings = TiltSettings(path)
        adapter = TiltAdapter(settings=settings, device_path="/dev/null")
        adapter._estimator._roll = math.radians(10)
        adapter._estimator._pitch = math.radians(-5)
        ax, ay, az = adapter.sensor_acceleration()
        self.assertGreater(ax, 0.0)
        self.assertLess(ay, 0.0)
        self.assertGreater(az, 9.0)


class TestConsoleSensorTransport(unittest.TestCase):
    """Console channel: auth, push, throttle, failure (fake sockets only)."""

    def _transport(self, sock, **kwargs):
        from .sensor_transport import ConsoleSensorTransport
        holder = {"sock": sock}
        def connect(host, port, timeout):
            self.assertEqual((host, port), ("127.0.0.1", 5594))
            return holder["sock"]
        kwargs.setdefault("time_fn", time.monotonic)
        return ConsoleSensorTransport(connect_fn=connect, **kwargs)

    def test_connect_authenticates_without_logging_token(self):
        import tempfile as tf
        with tf.NamedTemporaryFile("w", delete=False) as token:
            token.write("secret-token-123")
            token_path = token.name
        self.addCleanup(os.unlink, token_path)
        from pathlib import Path as _Path
        sock = FakeConsoleSocket()
        transport = self._transport(sock, token_path=_Path(token_path))
        self.assertTrue(transport.connect())
        self.assertTrue(transport.is_open)
        auth = [line for line in sock.sent if line.startswith(b"auth ")]
        self.assertEqual(len(auth), 1)
        transport.close()
        self.assertFalse(transport.is_open)
        self.assertTrue(sock.closed)

    def test_push_sends_formatted_command_and_throttles(self):
        times = [100.0]
        sock = FakeConsoleSocket(greeting=b"OK\n")
        transport = self._transport(sock, time_fn=lambda: times[0])
        self.assertTrue(transport.set_acceleration((1.0, 0.0, 9.7)))
        first = len(sock.sent)
        self.assertTrue(sock.sent[-1].startswith(b"sensor set acceleration 1.000:"))
        # Immediate redundant push is throttled (still True, no new write).
        self.assertTrue(transport.set_acceleration((1.005, 0.0, 9.7)))
        self.assertEqual(len(sock.sent), first)
        # After the interval with a real move, a new command goes out.
        times[0] += 0.05
        self.assertTrue(transport.set_acceleration((2.0, 0.0, 9.5)))
        self.assertGreater(len(sock.sent), first)
        # Forced neutral always writes (mode switch / stale / panel).
        self.assertTrue(transport.set_acceleration((0.0, 0.0, 9.81), force=True))

    def test_send_failure_closes_channel(self):
        sock = FakeConsoleSocket(greeting=b"OK\n", fail_send=True)
        transport = self._transport(sock)
        self.assertFalse(transport.set_acceleration((1.0, 0.0, 9.0)))
        self.assertFalse(transport.is_open)
        self.assertIsNotNone(transport.last_error)

    def test_console_rejection_reports_error(self):
        class Rejecting(FakeConsoleSocket):
            def sendall(self, data):
                self.sent.append(data)
                self._replies.append(b"KO: unknown sensor name\n")
        sock = Rejecting(greeting=b"OK\n")
        transport = self._transport(sock)
        self.assertFalse(transport.set_acceleration((1.0, 0.0, 9.0), force=True))
        self.assertIn("rejected", transport.last_error)

    def test_no_greeting_hang_beyond_bounded_handshake(self):
        import socket as _socket
        def connect(host, port, timeout):
            self.assertLessEqual(timeout, 2.0)
            raise OSError("connection refused")
        from .sensor_transport import ConsoleSensorTransport
        transport = ConsoleSensorTransport(connect_fn=connect)
        self.assertFalse(transport.connect())
        self.assertIsNotNone(transport.last_error)


def _write_fake_node(root, node, name, vendor="", product="", abs_caps="",
                     ev_caps=""):
    device = os.path.join(root, node, "device")
    os.makedirs(os.path.join(device, "id"), exist_ok=True)
    os.makedirs(os.path.join(device, "capabilities"), exist_ok=True)
    with open(os.path.join(device, "name"), "w") as stream:
        stream.write(name + "\n")
    if vendor:
        with open(os.path.join(device, "id", "vendor"), "w") as stream:
            stream.write(vendor + "\n")
    if product:
        with open(os.path.join(device, "id", "product"), "w") as stream:
            stream.write(product + "\n")
    if abs_caps:
        with open(os.path.join(device, "capabilities", "abs"), "w") as stream:
            stream.write(abs_caps + "\n")
    if ev_caps:
        with open(os.path.join(device, "capabilities", "ev"), "w") as stream:
            stream.write(ev_caps + "\n")


class TestTieredDiscovery(unittest.TestCase):
    """Sept-14: in-session discovery-miss needs tiers + a sysfs snapshot."""

    def test_tier1_exact_identity(self):
        with tempfile.TemporaryDirectory() as root:
            _write_fake_node(root, "event8", "Steam Deck Motion Sensors",
                             "28de", "1205", "3f", "19")
            path, tier, _snap = _discover_motion_node(root)
            self.assertEqual(path, "/dev/input/event8")
            self.assertEqual(tier, "tier1")

    def test_tier2_name_only_survives_pid_change(self):
        with tempfile.TemporaryDirectory() as root:
            _write_fake_node(root, "event9", "Steam Deck Motion Sensors",
                             "28de", "11ff", "3f", "19")
            path, tier, _snap = _discover_motion_node(root)
            self.assertEqual(path, "/dev/input/event9")
            self.assertEqual(tier, "tier2")

    def test_tier3_capability_fallback_survives_rename(self):
        with tempfile.TemporaryDirectory() as root:
            _write_fake_node(root, "event4", "Deck IMU Renamed",
                             "28de", "1205", "3f", "19")
            path, tier, _snap = _discover_motion_node(root)
            self.assertEqual(path, "/dev/input/event4")
            self.assertEqual(tier, "tier3")

    def test_tier3_rejects_keyboard_with_key_bit(self):
        with tempfile.TemporaryDirectory() as root:
            _write_fake_node(root, "event2", "AT Translated Set 2 keyboard",
                             "28de", "1205", "0", "120013")
            path, tier, _snap = _discover_motion_node(root)
            self.assertIsNone(path)
            self.assertEqual(tier, "miss")

    def test_tier3_rejects_mouse_without_six_axes(self):
        with tempfile.TemporaryDirectory() as root:
            _write_fake_node(root, "event5", "Valve Software Steam Controller",
                             "28de", "1205", "3", "b")
            path, tier, _snap = _discover_motion_node(root)
            self.assertIsNone(path)
            self.assertEqual(tier, "miss")

    def test_tier3_rejects_other_vendor_imu_lookalike(self):
        with tempfile.TemporaryDirectory() as root:
            _write_fake_node(root, "event6", "Some Other IMU",
                             "1234", "5678", "3f", "19")
            path, tier, _snap = _discover_motion_node(root)
            self.assertIsNone(path)
            self.assertEqual(tier, "miss")

    def test_miss_snapshot_names_nodes_without_env(self):
        with tempfile.TemporaryDirectory() as root:
            _write_fake_node(root, "event0", "Power Button", "0001", "0001")
            snap = discovery_snapshot(root)
            self.assertIn("nodes=1", snap)
            self.assertIn("event0:Power Button", snap)
            self.assertIn("0001:0001", snap)
            environ = dict(os.environ)
            for value in environ.values():
                if len(value) > 12:
                    self.assertNotIn(value, snap)

    def test_snapshot_is_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            for i in range(100):
                _write_fake_node(root, f"event{i}", "X" * 200, "1", "2",
                                 "f" * 100, "f" * 100)
            from .motion_reader import SNAPSHOT_MAX_CHARS
            self.assertLessEqual(len(discovery_snapshot(root)), SNAPSHOT_MAX_CHARS)

    def test_open_records_tier_and_clears_on_success(self):
        import unittest.mock as _mock
        reader = MotionReader(sysfs_root="/definitely-not-sysfs")
        self.assertFalse(reader.open())
        self.assertEqual(reader.match_tier, "miss")
        self.assertIsNotNone(reader.last_snapshot)
        self.assertIn("not found", reader.last_error)
        with tempfile.TemporaryDirectory() as root:
            _write_fake_node(root, "event8", "Steam Deck Motion Sensors",
                             "28de", "1205", "3f", "19")
            reader._sysfs_root = root
            reader._device_path = None
            with _mock.patch("os.open", return_value=123):
                self.assertTrue(reader.open())
            self.assertEqual(reader.match_tier, "tier1")
            self.assertIsNone(reader.last_error)
            reader._fd = None

    def test_adapter_health_carries_match_tier(self):
        adapter = TiltAdapter(
            settings=TiltSettings(os.path.join(tempfile.mkdtemp(), "t2.json")),
            device_path="/dev/definitely-not-jcs2-tilt-node")
        self.assertFalse(adapter.open())
        h = adapter.health()
        self.assertIn("match_tier", h)


class TestStickGate(unittest.TestCase):
    """Analog-only gate: LX/LY drop under armed tilt; buttons never drop."""

    def test_inactive_gate_forwards_everything(self):
        for event in (dict(type="axis", axis="LX", value=0.7),
                      dict(type="axis", axis="LY", value=-0.3),
                      dict(type="button", key="RB", action="down")):
            self.assertTrue(stick_gate_allows(event, False))

    def test_active_gate_drops_only_lx_ly(self):
        self.assertFalse(stick_gate_allows(
            dict(type="axis", axis="LX", value=0.7), True))
        self.assertFalse(stick_gate_allows(
            dict(type="axis", axis="LY", value=-0.3), True))
        for event in (dict(type="axis", axis="LT", value=1.0),
                      dict(type="axis", axis="RT", value=0.5),
                      dict(type="axis", axis="RX", value=0.2),
                      dict(type="button", key="RB", action="down"),
                      dict(type="button", key="LB", action="up"),
                      dict(type="button", key="START", action="down"),
                      dict(type="button", key="Y", action="down")):
            self.assertTrue(stick_gate_allows(event, True), event)

    def test_gated_axes_are_exactly_lx_ly(self):
        self.assertEqual(GATED_AXES, frozenset(("LX", "LY")))

    def test_malformed_event_fails_open(self):
        self.assertTrue(stick_gate_allows(None, True))
        self.assertTrue(stick_gate_allows("LX", True))


class TestHidrawReader(unittest.TestCase):
    """Steam-active source transport: frames without decode claims."""

    def test_pipe_frames_split_on_report_boundary(self):
        import fcntl
        from .hidraw_reader import HidrawFrameReader, REPORT_LEN
        reader = HidrawFrameReader(device_path="/dev/null")
        r, w = os.pipe()
        try:
            fcntl.fcntl(r, fcntl.F_SETFL, os.O_NONBLOCK)
            os.write(w, b"\x01" + b"\x00" * (REPORT_LEN - 1))
            os.write(w, b"\x01" + b"\x00" * (REPORT_LEN - 1))
            reader._fd = r
            frames = reader.read_frames()
            self.assertEqual(len(frames), 2)
            self.assertEqual(reader.frames_read, 2)
            self.assertTrue(all(len(raw) == REPORT_LEN for _, raw in frames))
        finally:
            reader._fd = None
            os.close(r)
            os.close(w)

    def test_closed_reader_reads_empty(self):
        from .hidraw_reader import HidrawFrameReader
        self.assertEqual(HidrawFrameReader(device_path="/dev/null").read_frames(), [])

    def test_open_failure_records_errno(self):
        from .hidraw_reader import HidrawFrameReader
        reader = HidrawFrameReader(device_path="/dev/definitely-not-hidraw")
        self.assertFalse(reader.open())
        self.assertIn("errno", reader.last_error)

    def test_analyze_variance_finds_moving_offsets(self):
        from .hidraw_reader import analyze_variance
        still = ["01" + "00" * 63] * 5
        moving = ["01" + "00" * 47 + f"{i:02x}" + "00" * 15 for i in range(5)]
        variance = analyze_variance(still + moving)
        self.assertEqual(variance[0], 1)
        self.assertGreater(variance[48], 1)

    def test_parse_deck_report_on_real_steam_active_frames(self):
        # Real frames from the Sept-14 Steam-active hidraw2 capture
        # (0003:28DE:1205.0004, 64 B, ver=1/type=9/len=64). Format-only:
        # values asserted, motion NOT validated.
        from .hidraw_reader import parse_deck_report, seq_is_monotonic
        frame0 = bytes.fromhex(
            "01000940323909000000100000000000000000007417002ed703441fb8370400"
            "0400ffff8ed1a0efd2e3448d00000000e009ae0727fb19010000d6015a002200")
        frame500 = bytes.fromhex(
            "01000940263b0900000010000000000000000000a423566554043f1f2b38fbff"
            "000000008ed196efb0e34e8d00000000010aae070bfb19010000d00352002b00")
        first = parse_deck_report(frame0)
        later = parse_deck_report(frame500)
        self.assertEqual(first["seq"], 604466)
        self.assertEqual(later["seq"], 604966)
        self.assertEqual(later["seq"] - first["seq"], 500)
        self.assertEqual(len(first["accel_raw"]), 3)
        self.assertEqual(len(first["gyro_raw"]), 3)
        self.assertEqual(len(first["sticks_raw"]), 4)
        self.assertEqual(first["accel_raw"][0], 14264)
        self.assertTrue(seq_is_monotonic([first, first, later]) is False)
        adjacent = dict(first)
        adjacent["seq"] = first["seq"] + 1
        self.assertTrue(seq_is_monotonic([first, adjacent]))

    def test_parse_deck_report_rejects_bad_header(self):
        from .hidraw_reader import parse_deck_report
        with self.assertRaises(ValueError):
            parse_deck_report(b"\x00" * 64)
        with self.assertRaises(ValueError):
            parse_deck_report(b"\x01\x00\x08\x40" + b"\x00" * 60)

    def test_resolve_imu_hidraw_selects_input2_by_identity(self):
        from .hidraw_reader import resolve_imu_hidraw
        with tempfile.TemporaryDirectory() as hid_root, \
                tempfile.TemporaryDirectory() as dev_root:
            def hid_dev(name, phys, has_hidraw, has_input):
                dev = os.path.join(hid_root, name)
                os.makedirs(dev)
                with open(os.path.join(dev, "uevent"), "w") as stream:
                    stream.write(f"HID_PHYS={phys}\n")
                if has_hidraw:
                    os.makedirs(os.path.join(dev, "hidraw", "hidraw9"))
                if has_input:
                    os.makedirs(os.path.join(dev, "input", "input99"))
            hid_dev("0003:28DE:1205.0003", "usb-0000:04:00.4-3/input2",
                    has_hidraw=False, has_input=True)
            hid_dev("0003:28DE:1205.0004", "usb-0000:04:00.4-3/input2",
                    has_hidraw=True, has_input=False)
            hid_dev("0003:28DE:1205.0001", "usb-0000:04:00.4-3/input0",
                    has_hidraw=True, has_input=True)
            node, detail = resolve_imu_hidraw(hid_root, dev_root)
            self.assertEqual(node, os.path.join(dev_root, "hidraw9"))
            self.assertIn("input2", detail)

    def test_resolve_imu_hidraw_miss_names_state(self):
        from .hidraw_reader import resolve_imu_hidraw
        with tempfile.TemporaryDirectory() as hid_root, \
                tempfile.TemporaryDirectory() as dev_root:
            node, detail = resolve_imu_hidraw(hid_root, dev_root)
            self.assertIsNone(node)
            self.assertIn("no 28de:1205", detail)

    def test_resolve_imu_hidraw_on_host_without_steam(self):
        # Read-only real sysfs: only meaningful where a Deck IMU exists.
        # Non-Deck hosts (no 28de:1205 node) skip instead of failing.
        from .hidraw_reader import resolve_imu_hidraw
        node, detail = resolve_imu_hidraw()
        if node is None and "no 28de:1205" in detail:
            self.skipTest("no Steam Deck IMU on this host")
        self.assertIsNotNone(node)
        self.assertTrue(node.startswith("/dev/hidraw"))
        self.assertIn("input2", detail)


if __name__ == "__main__":
    unittest.main()
