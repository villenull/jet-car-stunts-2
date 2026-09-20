#!/usr/bin/env python3
"""Audio configuration regression tests using only temporary files and fakes."""
from pathlib import Path
import tempfile
import unittest
from audio_config import (MEDIA_VOLUME_TARGET, audio_config_status, media_volume_command,
                          parse_volume_index, seed_media_volume)


class AudioConfigTests(unittest.TestCase):
    def status(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.ini'
            path.write_text(text)
            before = path.read_bytes()
            result = audio_config_status(path)
            self.assertEqual(path.read_bytes(), before)
            return result

    def test_preserved_profile_disabled_output_is_detected(self):
        self.assertTrue(self.status('hw.audioInput = no\nhw.audioOutput = no\n')['output_disabled'])

    def test_output_enabled_does_not_require_microphone(self):
        result = self.status('hw.audioInput = no\nhw.audioOutput = yes\n')
        self.assertEqual(result['input'], 'no')
        self.assertEqual(result['output'], 'yes')
        self.assertFalse(result['output_disabled'])

    def test_missing_flags_use_emulator_defaults(self):
        result = self.status('hw.ramSize = 1536\n')
        self.assertEqual((result['input'], result['output']), ('yes', 'yes'))

    def test_comments_spacing_and_false_values(self):
        result = self.status('# hw.audioOutput = yes\n; hw.audioOutput = yes\n  hw.audioOutput=FALSE\n')
        self.assertTrue(result['output_disabled'])


class MediaVolumeSeedTests(unittest.TestCase):
    class FakeAdb:
        """Records ADB argv and replays canned stdout for the readback call."""

        def __init__(self, readback):
            self.readback = readback
            self.calls = []

        def __call__(self, *args):
            self.calls.append(args)
            stdout = self.readback if 'settings' in args else ''
            return type('Result', (), {'stdout': stdout, 'returncode': 0})()

    def test_parse_index_accepts_guest_integer(self):
        self.assertEqual(parse_volume_index('15'), 15)
        self.assertEqual(parse_volume_index(' 5\n'), 5)
        self.assertEqual(parse_volume_index('0'), 0)

    def test_parse_index_rejects_unset_and_out_of_range(self):
        for text in ('null', '', '   ', '-1', '16', 'fifteen', '\u00b2'):
            self.assertIsNone(parse_volume_index(text), text)

    def test_out_of_range_target_is_rejected(self):
        with self.assertRaises(ValueError):
            media_volume_command(MEDIA_VOLUME_TARGET + 1)

    def test_seed_reports_readback_after_writing(self):
        adb = self.FakeAdb(str(MEDIA_VOLUME_TARGET))
        report = seed_media_volume(adb)
        self.assertEqual(report, {'target': MEDIA_VOLUME_TARGET, 'index': MEDIA_VOLUME_TARGET, 'seeded': True})
        self.assertEqual(len(adb.calls), 2)
        self.assertEqual(adb.calls[1], ('shell', 'settings', 'get', 'system', 'volume_music_speaker'))

    def test_seed_reports_low_readback_instead_of_claiming_success(self):
        report = seed_media_volume(self.FakeAdb('5'))
        self.assertEqual(report, {'target': MEDIA_VOLUME_TARGET, 'index': 5, 'seeded': False})

    def test_seed_reports_unset_readback_instead_of_claiming_success(self):
        report = seed_media_volume(self.FakeAdb('null'))
        self.assertEqual(report, {'target': MEDIA_VOLUME_TARGET, 'index': None, 'seeded': False})


if __name__ == '__main__':
    unittest.main()
