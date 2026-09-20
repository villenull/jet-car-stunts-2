import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock
import prepare_distribution as pd
from prepare_distribution import prepare, exec_argument, desktop_string, METADATA

ROOT = Path(__file__).resolve().parents[1]


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
        self.assertEqual(spec['installer']['layout_marker'], 'jcs2-layout.json')
        self.assertEqual(spec['installer']['install_state'], 'install-state.json')


class SetupArchiveTests(unittest.TestCase):
    def archive_names(self, archive):
        with tarfile.open(archive, 'r:gz') as bundle:
            return {member.name: member for member in bundle.getmembers()}

    def test_setup_archive_is_text_only_complete_and_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / 'first'
            second = Path(tmp) / 'second'
            _, report = prepare(ROOT, archive_dir=first, commit='test-commit')
            _, again = prepare(ROOT, archive_dir=second, commit='test-commit')
            self.assertFalse(report['installer']['binary_content'])
            self.assertIn('tree_dirty', report['installer'])
            self.assertEqual(report['installer']['sha256'], again['installer']['sha256'])
            archive = Path(report['installer']['archive'])
            self.assertEqual(archive.name, 'jet-car-stunts-2-setup.tar.gz')
            members = self.archive_names(archive)
            self.assertIn('jet-car-stunts-2/setup-jcs2.sh', members)
            self.assertEqual(members['jet-car-stunts-2/setup-jcs2.sh'].mode, 0o755)
            self.assertEqual(members['jet-car-stunts-2/run-jcs2'].mode, 0o755)
            self.assertEqual(members['jet-car-stunts-2/payload-commit.txt'].mtime, 0)
            self.assertIn('jet-car-stunts-2/packaging/installer/install_jcs2.py', members)
            self.assertIn('jet-car-stunts-2/packaging/installer/runtime-lock.json', members)
            self.assertIn('jet-car-stunts-2/steam/steam_shortcut.py', members)
            self.assertIn('jet-car-stunts-2/linux-launcher/python-select.sh', members)
            self.assertNotIn('jet-car-stunts-2/linux-launcher/jcs2-controller-linux', members)
            self.assertNotIn('jet-car-stunts-2/steam/artwork/cover.png', members)
            for name in members:
                self.assertFalse(name.startswith('/'))
                self.assertNotIn('..', Path(name).parts)
            with tarfile.open(archive, 'r:gz') as bundle:
                for member in bundle.getmembers():
                    if member.isdir():
                        continue
                    payload = bundle.extractfile(member).read()
                    self.assertNotIn(b'\x00', payload, member.name)
                    payload.decode('utf-8')
                commit = bundle.extractfile('jet-car-stunts-2/payload-commit.txt').read()
            self.assertEqual(commit, b'test-commit\n')
            sidecar = Path(str(archive) + '.sha256')
            self.assertTrue(sidecar.read_text().startswith(report['installer']['sha256']))

    def test_shipped_file_list_matches_the_installer_manifest_contract(self):
        destinations = [entry['destination'] for entry in pd._plan.setup_entries()]
        self.assertEqual(pd._plan.SETUP_ENTRY[1], 'setup-jcs2.sh')
        self.assertIn('packaging/installer/install_jcs2.py', destinations)
        self.assertIn('assets/controller/mapping.json', [
            row['destination'] for row in pd._plan.manifest()])
        self.assertNotIn('steam/artwork/cover.png', destinations)

    def test_setup_archive_refuses_binary_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'blob.bin').write_bytes(b'\x00\x01\x02PNG\x00')
            self.assertFalse(pd.is_text_file(root / 'blob.bin'))
            entries = [{'source': 'blob.bin', 'destination': 'blob.bin', 'kind': 'file'}]
            with mock.patch.object(pd._plan, 'setup_entries', lambda: entries):
                with self.assertRaises(ValueError):
                    pd.write_setup_archive(root, root / 'setup.tar.gz', commit='test')
            self.assertFalse((root / 'setup.tar.gz').exists())

    def test_setup_archive_reports_missing_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [{'source': 'absent.py', 'destination': 'absent.py', 'kind': 'file'}]
            with mock.patch.object(pd._plan, 'setup_entries', lambda: entries):
                with self.assertRaises(ValueError):
                    pd.setup_members(root)


if __name__ == '__main__':
    unittest.main()
