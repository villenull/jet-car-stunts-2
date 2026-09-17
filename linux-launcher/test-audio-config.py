#!/usr/bin/env python3
"""Audio configuration regression tests using only temporary files."""
from pathlib import Path
import tempfile
import unittest
from audio_config import audio_config_status


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


if __name__ == '__main__':
    unittest.main()
