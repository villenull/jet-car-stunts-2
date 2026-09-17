import contextlib
import importlib.util
import io
import sys
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    'bootstrap_linux_guest', Path(__file__).with_name('bootstrap_linux_guest.py'))
bootstrap = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bootstrap
spec.loader.exec_module(bootstrap)


SPLITS = list(bootstrap.EXPECTED_SPLITS)


def make_fixture(root: Path) -> dict:
    sdk = root / 'sdk'
    (sdk / 'platform-tools').mkdir(parents=True)
    (sdk / 'emulator').mkdir(parents=True)
    (sdk / 'platform-tools' / 'adb').write_bytes(b'adb')
    (sdk / 'emulator' / 'emulator').write_bytes(b'emu')
    image = sdk / 'system-images/android-28/google_apis/x86'
    image.mkdir(parents=True)
    (image / 'source.properties').write_text('Pkg.Revision=28\n')
    apks = root / 'apks'
    apks.mkdir()
    for i, name in enumerate(SPLITS):
        (apks / name).write_bytes(f'apk-payload-{i}'.encode())
    avd_home = root / 'avd-home'
    avd_home.mkdir()
    return {'sdk': sdk, 'apks': apks, 'avd_home': avd_home, 'root': root}


def base_args(fx: dict) -> list:
    return ['--root', str(fx['root']), '--sdk', str(fx['sdk']),
            '--apks-dir', str(fx['apks']), '--avd-home', str(fx['avd_home'])]


def snapshot(root: Path) -> dict:
    return {str(p): p.read_bytes() for p in sorted(root.rglob('*')) if p.is_file()}


def make_monolith_fixture(root: Path, name: str = 'signed-monolith.apk') -> dict:
    fx = make_fixture(root)
    for split in SPLITS:
        (fx['apks'] / split).unlink()
    (fx['apks'] / name).write_bytes(b'monolith-payload')
    return fx


class ConfigValidationTests(unittest.TestCase):
    def test_monolith_single_apk_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_monolith_fixture(Path(tmp))
            cfg = bootstrap.load_config(bootstrap.parse_args(base_args(fx)))
            self.assertEqual([p.name for p in cfg.split_paths],
                             ['signed-monolith.apk'])
            self.assertEqual(len(cfg.apk_hashes), 1)

    def test_monolith_extra_apk_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_monolith_fixture(Path(tmp))
            (fx['apks'] / 'other.apk').write_bytes(b'extra')
            with self.assertRaises(bootstrap.BootstrapError) as ctx:
                bootstrap.load_config(bootstrap.parse_args(base_args(fx)))
            self.assertEqual(ctx.exception.code, 2)

    def test_five_splits_required_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            cfg = bootstrap.load_config(bootstrap.parse_args(base_args(fx)))
            self.assertEqual([p.name for p in cfg.split_paths], SPLITS)
            # Missing one split is refused with exit code 2.
            (fx['apks'] / SPLITS[0]).unlink()
            with self.assertRaises(bootstrap.BootstrapError) as ctx:
                bootstrap.load_config(bootstrap.parse_args(base_args(fx)))
            self.assertEqual(ctx.exception.code, 2)
            # An extra apk is equally refused.
            (fx['apks'] / SPLITS[0]).write_bytes(b'restored')
            (fx['apks'] / 'split_config.de.apk').write_bytes(b'extra')
            with self.assertRaises(bootstrap.BootstrapError) as ctx:
                bootstrap.load_config(bootstrap.parse_args(base_args(fx)))
            self.assertEqual(ctx.exception.code, 2)

    def test_port_parity_and_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            with self.assertRaises(bootstrap.BootstrapError) as ctx:
                bootstrap.load_config(bootstrap.parse_args(
                    base_args(fx) + ['--console-port', '5595']))  # odd
            self.assertEqual(ctx.exception.code, 2)
            with self.assertRaises(bootstrap.BootstrapError) as ctx:
                bootstrap.load_config(bootstrap.parse_args(
                    base_args(fx) + ['--adb-port', '5594']))  # == console
            self.assertEqual(ctx.exception.code, 2)
            with self.assertRaises(bootstrap.BootstrapError) as ctx:
                bootstrap.load_config(bootstrap.parse_args(
                    base_args(fx) + ['--adb-port', '5595']))  # == console + 1
            self.assertEqual(ctx.exception.code, 2)
            cfg = bootstrap.load_config(bootstrap.parse_args(base_args(fx)))
            self.assertEqual((cfg.adb_port, cfg.console_port,
                              cfg.serial), (5038, 5594, '127.0.0.1:5595'))

    def test_name_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            for bad in ('hardened_api28', 'a/b', 'a\\b', '..', ''):
                with self.assertRaises(bootstrap.BootstrapError) as ctx:
                    bootstrap.load_config(bootstrap.parse_args(
                        base_args(fx) + ['--avd-name', bad]))
                self.assertEqual(ctx.exception.code, 2, bad)

    def test_controller_trio_all_or_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            ctrl = fx['root'] / 'ctrl'; ctrl.write_bytes(b'c')
            mapping = fx['root'] / 'map.json'; mapping.write_text('{}')
            helper = fx['root'] / 'help.jar'; helper.write_bytes(b'h')
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.load_config(bootstrap.parse_args(
                    base_args(fx) + ['--controller', str(ctrl)]))
            cfg = bootstrap.load_config(bootstrap.parse_args(
                base_args(fx) + ['--controller', str(ctrl), '--mapping', str(mapping),
                                 '--helper-jar', str(helper)]))
            self.assertEqual(cfg.helper_hash, bootstrap.sha256_file(helper))

    def test_missing_sdk_tooling_is_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            (fx['sdk'] / 'platform-tools' / 'adb').unlink()
            with self.assertRaises(bootstrap.BootstrapError) as ctx:
                bootstrap.load_config(bootstrap.parse_args(base_args(fx)))
            self.assertEqual(ctx.exception.code, 2)


class IdentityTests(unittest.TestCase):
    def test_identity_stable_and_helper_sensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            paths = bootstrap.collect_split_paths(fx['apks'])
            first, hashes, none_hash = bootstrap.compute_identity(
                bootstrap.PACKAGE, paths)
            second, _, _ = bootstrap.compute_identity(bootstrap.PACKAGE, paths)
            self.assertEqual(first, second)
            self.assertEqual(len(hashes), 5)
            self.assertIsNone(none_hash)
            self.assertTrue(first.startswith(bootstrap.PACKAGE + '\n'))
            helper = fx['root'] / 'h.jar'
            helper.write_bytes(b'helper-bytes')
            with_helper, _, helper_hash = bootstrap.compute_identity(
                bootstrap.PACKAGE, paths, helper)
            self.assertNotEqual(first, with_helper)
            self.assertIn(helper_hash, with_helper)


class ArgvTests(unittest.TestCase):
    def test_argv_builders_match_runner_forms(self):
        adb = Path('/sdk/platform-tools/adb')
        emu = Path('/sdk/emulator/emulator')
        self.assertEqual(bootstrap.adb_server_argv(adb, 5038),
                         [str(adb), '-P', '5038', 'nodaemon', 'server'])
        argv = bootstrap.emulator_argv(emu, 'jcs2-fresh', 5594, adb)
        self.assertEqual(argv[:9],
                         [str(emu), '-avd', 'jcs2-fresh', '-port', '5594',
                          '-no-snapshot', '-no-boot-anim', '-adb-path', str(adb)])
        self.assertIn('-gpu', argv)
        self.assertEqual(argv[argv.index('-gpu') + 1], 'swiftshader_indirect')
        self.assertIn('-fixed-scale', argv)
        headless = bootstrap.emulator_argv(emu, 'jcs2-fresh', 5594, adb,
                                           headless=True)
        self.assertIn('-no-window', headless)
        self.assertNotIn('-fixed-scale', headless)
        splits = [Path(f'/apks/{name}') for name in SPLITS]
        install = bootstrap.install_argv(adb, 5038, '127.0.0.1:5595', splits)
        middle = install[install.index('-s') + 2:]
        self.assertEqual(middle[:3], ['install-multiple', '-r', '--no-streaming'])
        self.assertEqual([Path(t).name for t in middle[3:]], SPLITS)

    def test_config_template_uses_swiftshader_gpu(self):
        text = bootstrap.avd_config_text(Path('/sdk/image'), 'jcs2-fresh')
        self.assertIn('hw.gpu.mode = swiftshader_indirect', text)
        self.assertNotIn('hw.gpu.mode = host', text)
        self.assertIn('hw.ramSize = 1536', text)
        self.assertIn('vm.heapSize = 256', text)
        self.assertIn('disk.dataPartition.size = 6442450944', text)
        self.assertIn('image.sysdir.1 = /sdk/image', text)


class MarkerTests(unittest.TestCase):
    def test_compatible_and_completed_semantics(self):
        ident = 'pkg\nabc\n'
        self.assertTrue(bootstrap.bootstrap_marker_completed(ident, ident))
        self.assertFalse(bootstrap.bootstrap_marker_completed('pending\n' + ident,
                                                              ident))
        self.assertTrue(bootstrap.bootstrap_marker_compatible(ident, ident, False))
        self.assertTrue(bootstrap.bootstrap_marker_compatible('pending\n' + ident,
                                                              ident, False))
        self.assertTrue(bootstrap.bootstrap_marker_compatible('', ident, True))
        self.assertFalse(bootstrap.bootstrap_marker_compatible('', ident, False))
        self.assertFalse(bootstrap.bootstrap_marker_compatible('other', ident, True))

    def test_exact_boot_completed(self):
        self.assertTrue(bootstrap.exact_boot_completed('1\n'))
        self.assertTrue(bootstrap.exact_boot_completed('noise\n1\r\n'))
        self.assertFalse(bootstrap.exact_boot_completed('10\n'))
        self.assertFalse(bootstrap.exact_boot_completed(''))

    def test_exact_uid(self):
        self.assertTrue(bootstrap.exact_uid('uid=2000(shell) gid=2000', 2000))
        self.assertFalse(bootstrap.exact_uid('uid=0(root) gid=0', 2000))
    def _adopt_cfg(self, home: Path):
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            args = base_args(fx) + ['--avd-home', str(home)]
            cfg = bootstrap.load_config(bootstrap.parse_args(args))
            return cfg

    def test_claim_adopts_unbooted_guest_without_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'avd'
            home.mkdir()
            guest = home / 'jcs2-fresh.avd'
            guest.mkdir()
            (guest / 'config.ini').write_text('hw.gpu.mode = swiftshader_indirect\n')
            (home / 'jcs2-fresh.ini').write_text('path=' + str(guest) + '\n')
            cfg = self._adopt_cfg(home)
            _, _, fresh = bootstrap.claim_avd(cfg)
            self.assertTrue(fresh)
            self.assertEqual((guest / '.jcs2-owned').read_text(), 'jcs2-fresh\n')
            self.assertFalse(bootstrap.check_bootstrap_marker(cfg, fresh))

    def test_marker_missing_on_freshly_claimed_guest_proceeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'avd'
            home.mkdir()
            guest = home / 'jcs2-fresh.avd'
            guest.mkdir()
            (guest / 'config.ini').write_text('hw.gpu.mode = swiftshader_indirect\n')
            (guest / '.jcs2-owned').write_text('jcs2-fresh\n')
            (home / 'jcs2-fresh.ini').write_text('path=' + str(guest) + '\n')
            cfg = self._adopt_cfg(home)
            self.assertTrue(bootstrap.guest_freshly_claimed(cfg))
            self.assertFalse(bootstrap.check_bootstrap_marker(cfg, False))

    def test_claim_refuses_booted_guest_without_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'avd'
            home.mkdir()
            guest = home / 'jcs2-fresh.avd'
            guest.mkdir()
            (guest / 'config.ini').write_text('hw.gpu.mode = swiftshader_indirect\n')
            (home / 'jcs2-fresh.ini').write_text('path=' + str(guest) + '\n')
            (guest / 'userdata-qemu.img.qcow2').write_bytes(b'QFI-fb')
            with self.assertRaises(bootstrap.BootstrapError) as ctx:
                bootstrap.claim_avd(self._adopt_cfg(home))
            self.assertEqual(ctx.exception.code, 4)


class DryRunTests(unittest.TestCase):
    def test_dry_run_exits_zero_and_touches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            before = snapshot(Path(tmp))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = bootstrap.main(base_args(fx))
            self.assertEqual(code, 0)
            self.assertIn('dry-run; nothing was touched', out.getvalue())
            self.assertIn('install-multiple', out.getvalue())
            self.assertEqual(before, snapshot(Path(tmp)),
                             'dry-run must leave the tree byte-identical')


if __name__ == '__main__':
    unittest.main()
