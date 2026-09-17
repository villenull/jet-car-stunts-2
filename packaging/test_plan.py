import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('plan', Path(__file__).with_name('plan.py'))
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


class PlannerTests(unittest.TestCase):
    def test_relative_backing_header_without_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'disk.qcow2'
            p.write_bytes(struct.pack('>4sIQI', b'QFI\xfb', 3, 20, 8) + b'disk.img')
            before = p.read_bytes()
            self.assertEqual(plan.backing_reference(p), 'disk.img')
            self.assertEqual(p.read_bytes(), before)

    def test_invalid_backing_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'disk.qcow2'
            p.write_bytes(struct.pack('>4sIQI', b'QFI\xfb', 3, 20, 5000))
            with self.assertRaises(ValueError):
                plan.backing_reference(p)

    def test_inventory_does_not_follow_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'tree').mkdir()
            (root / 'outside').write_bytes(b'private')
            (root / 'tree/link').symlink_to(root / 'outside')
            result = plan.inventory(root / 'tree')
            self.assertEqual(result['logical_bytes'], 0)
            self.assertEqual(len(result['symlinks']), 1)

    def test_proposed_rewrites_and_missing_sources_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            avd = root / plan.AVD
            (avd / 'hardened_api28.avd').mkdir(parents=True)
            ini = avd / 'hardened_api28.ini'
            ini.write_text('path=' + str(avd / 'hardened_api28.avd') + '\n')
            config = avd / 'hardened_api28.avd/config.ini'
            config.write_text('image.sysdir.1=' + str(root / 'staging/windows-runtime/sdk' / plan.IMAGE) + '/\n')
            before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            result = plan.audit(root)
            self.assertEqual(result['proposed_ini_rewrites'][0]['proposed'], '<PACKAGE>/state/avd/hardened_api28.avd')
            self.assertEqual(result['proposed_ini_rewrites'][1]['proposed'], '<PACKAGE>/runtime/sdk/' + plan.IMAGE + '/')
            self.assertTrue(any('Missing source' in b for b in result['blockers']))
            self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_missing_external_dependencies_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guest = root / plan.AVD / 'hardened_api28.avd'
            guest.mkdir(parents=True)
            (guest / 'config.ini').write_text('image.sysdir.1=/missing-jcs2-image/\n')
            backing = b'../missing-base.img'
            (guest / 'userdata-qemu.img.qcow2').write_bytes(
                struct.pack('>4sIQI', b'QFI\xfb', 3, 20, len(backing)) + backing)
            blockers = plan.audit(root)['blockers']
            for expected in ('Missing absolute AVD dependency', 'Unmapped absolute AVD dependency',
                             'Missing QCOW backing file', 'External QCOW backing dependency'):
                self.assertTrue(any(expected in b for b in blockers), expected)

    def test_allowlist_excludes_backups_and_windows_binary_tree(self):
        for entry in plan.manifest():
            self.assertNotIn('backups/', entry['source'])
            self.assertNotIn('windows-runtime', entry['source'])
        self.assertIn(plan.SDK + '/emulator', [r['source'] for r in plan.manifest()])


if __name__ == '__main__':
    unittest.main()
