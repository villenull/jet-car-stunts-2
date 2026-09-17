import json
from pathlib import Path
import tempfile
import unittest
from prepare_distribution import prepare, exec_argument, desktop_string, METADATA


class DistributionTests(unittest.TestCase):
    def test_title_artwork_and_no_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'Game With Spaces'
            target = root / 'steam/jcs2-steam-launch.sh'
            target.parent.mkdir(parents=True)
            target.write_text('# placeholder; never launched')
            before = list(root.rglob('*'))
            desktop, report = prepare(root)
            self.assertIn('Name=Jet Car Stunts 2\n', desktop)
            self.assertIn('JCS2_LAYOUT=portable', desktop)
            self.assertEqual(report['steam_shortcut']['name'], 'Jet Car Stunts 2')
            self.assertFalse(report['steam_modified'])
            self.assertFalse(any(a['exists'] for a in report['artwork'].values()))
            self.assertEqual(before, list(root.rglob('*')))

    def test_exec_reserved_characters_are_escaped_without_shell(self):
        value = '/tmp/a b%"$`\\game'
        encoded = exec_argument(value)
        # Decode desktop string layer, then quoted Exec layer to recover path.
        first = encoded.replace('\\\\', '\\')
        self.assertTrue(first.startswith('"') and first.endswith('"'))
        first = first[1:-1]
        decoded = ''
        i = 0
        while i < len(first):
            if first[i] == '\\':
                i += 1
            decoded += first[i]
            i += 1
        self.assertEqual(decoded.replace('%%', '%'), value)

    def test_reject_invalid_paths(self):
        for path in ('/tmp/a\nb', '/tmp/a\x00b', '/tmp/a\rb'):
            with self.assertRaises(ValueError):
                desktop_string(path)

    def test_public_contract_excludes_personal_payloads(self):
        spec = json.loads(METADATA.read_text())
        self.assertIn('personal AVD', spec['exclude_public'])
        self.assertIn('save data', spec['exclude_public'])
        self.assertIn('User confirmed', spec['game_art_permission'])
        self.assertEqual(spec['artwork']['cover']['size'], [600, 900])


if __name__ == '__main__':
    unittest.main()
