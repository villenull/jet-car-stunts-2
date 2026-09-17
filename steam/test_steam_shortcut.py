#!/usr/bin/env python3
"""Synthetic-fixture tests for steam_shortcut/binary_vdf. Never touches real Steam."""
import contextlib
import io
import json
import os
import struct
import sys
import tempfile
import unittest
import zlib
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import binary_vdf as vdf  # noqa: E402
import steam_shortcut as ss  # noqa: E402

ACCOUNT = '12345678'
OTHER = '87654321'


def png(width, height, fill=b'\x00'):
    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF))
    raw = b''.join(b'\x00' + fill * (width * 4) for _ in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def entry(index, appid, name, exe, extra=()):
    s = lambda k, v: vdf.Node(vdf.T_STRING, k, v)
    return vdf.Node(vdf.T_MAP, str(index).encode(), [
        vdf.Node(vdf.T_INT32, b'appid', vdf.int32(appid)), s(b'AppName', name), s(b'Exe', exe),
        s(b'StartDir', b'/elsewhere/'), s(b'icon', b''), s(b'LaunchOptions', b'-x "%command%"'),
        vdf.Node(vdf.T_INT32, b'LastPlayTime', vdf.int32(1700000000)),
        vdf.Node(vdf.T_UINT64, b'FutureField', b'\x01\x02\x03\x04\x05\x06\x07\x08'),
        vdf.Node(vdf.T_FLOAT32, b'Scale', struct.pack('<f', 1.5)),
        vdf.Node(vdf.T_MAP, b'tags', [s(b'0', b'Favorite')]),
    ] + list(extra))


def read(path, mode='rb'):
    with open(path, mode) as handle:
        return handle.read()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.steam = os.path.join(base, 'home', '.local', 'share', 'Steam')
        for acc in (ACCOUNT, OTHER):
            os.makedirs(os.path.join(self.steam, 'userdata', acc, 'config'))
        os.makedirs(os.path.join(self.steam, 'config'))
        self.proc = os.path.join(base, 'proc')
        os.makedirs(self.proc)
        self.root = os.path.join(base, 'Games', 'Jet Car Stunts 2 — ü')
        os.makedirs(os.path.join(self.root, 'state'))
        os.makedirs(os.path.join(self.root, 'steam', 'artwork'))
        self.target = os.path.join(self.root, 'jet-car-stunts-2.sh')
        with open(self.target, 'w') as handle:
            handle.write('#!/bin/sh\n')
        os.chmod(self.target, 0o755)
        self.art('icon.png', png(4, 4))
        self.complete()

    def tearDown(self):
        self.tmp.cleanup()

    def art(self, name, data):
        with open(os.path.join(self.root, 'steam', 'artwork', name), 'wb') as handle:
            handle.write(data)

    def complete(self, status='complete'):
        with open(os.path.join(self.root, 'install-state.json'), 'w') as handle:
            json.dump({'status': status}, handle)

    def vdf_path(self, acc=ACCOUNT):
        return os.path.join(self.steam, 'userdata', acc, 'config', 'shortcuts.vdf')

    def grid(self, acc=ACCOUNT):
        return os.path.join(self.steam, 'userdata', acc, 'config', 'grid')

    def write_shortcuts(self, entries, acc=ACCOUNT):
        data = vdf.serialize([vdf.Node(vdf.T_MAP, b'shortcuts', entries)])
        with open(self.vdf_path(acc), 'wb') as handle:
            handle.write(data)
        return data

    def read_entries(self, acc=ACCOUNT):
        with open(self.vdf_path(acc), 'rb') as handle:
            return vdf.parse(handle.read())[0].value

    def register(self, **kw):
        kw.setdefault('steam_closed_confirmed', True)
        return ss.register(self.root, kw.pop('account', ACCOUNT), self.steam, proc_root=self.proc, **kw)

    def unregister(self, acc=ACCOUNT):
        return ss.unregister(self.root, acc, self.steam, True, proc_root=self.proc)

    def fake_process(self, pid, comm):
        os.makedirs(os.path.join(self.proc, str(pid)))
        with open(os.path.join(self.proc, str(pid), 'comm'), 'w') as handle:
            handle.write(comm + '\n')


class BinaryVdfTests(unittest.TestCase):
    def test_round_trip_all_supported_types(self):
        nodes = [vdf.Node(vdf.T_MAP, b'shortcuts', [entry(0, 0x9234abcd, b'X \xff', b'"/a b"')])]
        data = vdf.serialize(nodes)
        self.assertEqual(vdf.serialize(vdf.parse(data)), data)

    def test_rejects_malformed(self):
        good = vdf.serialize([vdf.Node(vdf.T_MAP, b'shortcuts', [entry(0, 1, b'a', b'b')])])
        for bad in (good[:-1], good + b'\x00', good[:10], b'\x05w\x00ab\x00\x08\x08',
                    b'\x00shortcuts\x00\x09k\x00\x08\x08', b''):
            with self.assertRaises(vdf.VdfError):
                vdf.parse(bad)


class RegisterTests(Fixture):
    def test_create_in_account_without_shortcuts(self):
        result = self.register()
        self.assertEqual(result['action'], 'create')
        (e,) = self.read_entries()
        exe = '"%s"' % self.target
        appid = ss.shortcut_appid(exe, 'Jet Car Stunts 2')
        self.assertEqual(result['appid'], appid)
        self.assertTrue(appid & 0x80000000)
        self.assertEqual(vdf.uint32_of(vdf.find(e.value, 'appid')), appid)
        self.assertEqual(vdf.string(e.value, 'AppName'), b'Jet Car Stunts 2')
        self.assertEqual(vdf.string(e.value, 'Exe'), exe.encode())
        self.assertEqual(vdf.string(e.value, 'StartDir'), ('"%s/"' % self.root).encode())
        self.assertEqual(vdf.string(e.value, 'LaunchOptions'), b'')
        icon = os.path.join(self.grid(), '%d_icon.png' % appid)
        self.assertEqual(vdf.string(e.value, 'icon'), icon.encode())
        self.assertTrue(os.path.isfile(icon))
        self.assertEqual(result['artwork']['icon']['status'], 'installed')
        self.assertEqual(result['artwork']['hero']['status'], 'missing-source')
        self.assertEqual(result['shortcut_id64'], str((appid << 32) | 0x02000000))
        self.assertFalse(os.path.exists(os.path.join(self.steam, 'config', 'config.vdf')))
        manifest = json.loads(read(os.path.join(self.root, 'state', 'steam-registration.json'), 'r'))
        self.assertEqual(len(manifest['registrations']), 1)

    def test_preserves_unrelated_entries_byte_identical_and_is_idempotent(self):
        other = entry(0, 0xF9E5EE54, b'Other \xc3\xa9', b'"/opt/other game/run"')
        before = vdf.serialize(other.value)
        self.write_shortcuts([other])
        self.art('cover.png', png(600, 900))
        self.art('hero.png', png(3840 // 64, 1240 // 64))  # wrong size: warning only
        first = self.register()
        entries = self.read_entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual(vdf.serialize(entries[0].value), before)
        self.assertTrue(any('hero is' in w for w in first['warnings']))
        grid_before = sorted(os.listdir(self.grid()))
        backups = os.listdir(os.path.join(self.root, 'state', 'steam-backup'))
        with open(self.vdf_path(), 'rb') as handle:
            snapshot = handle.read()
        second = self.register()
        self.assertEqual(second['action'], 'noop')
        self.assertIsNone(second['backup_dir'])
        with open(self.vdf_path(), 'rb') as handle:
            self.assertEqual(handle.read(), snapshot)
        self.assertEqual(sorted(os.listdir(self.grid())), grid_before)
        self.assertEqual(os.listdir(os.path.join(self.root, 'state', 'steam-backup')), backups)
        self.assertIn('%dp.png' % first['appid'], grid_before)
        self.assertIn('%d_hero.png' % first['appid'], grid_before)

    def test_backup_contains_original(self):
        original = self.write_shortcuts([entry(0, 5, b'a', b'"/b"')])
        result = self.register()
        with open(os.path.join(result['backup_dir'], 'shortcuts.vdf'), 'rb') as handle:
            self.assertEqual(handle.read(), original)

    def test_user_launch_options_name_and_art_are_preserved(self):
        first = self.register()
        appid = first['appid']
        entries = self.read_entries()
        vdf.find(entries[0].value, 'LaunchOptions').value = b'--control-settings'
        vdf.find(entries[0].value, 'AppName').value = b'JCS2 mine'
        vdf.find(entries[0].value, 'LastPlayTime').value = vdf.int32(42)
        self.write_shortcuts(entries)
        user_hero = os.path.join(self.grid(), '%d_hero.jpg' % appid)
        with open(user_hero, 'wb') as handle:
            handle.write(b'user art')
        with open(os.path.join(self.grid(), '%d_icon.png' % appid), 'wb') as handle:
            handle.write(b'user icon')
        self.art('hero.png', png(8, 8))
        self.art('icon.png', png(5, 5))
        result = self.register()
        (e,) = self.read_entries()
        self.assertEqual(vdf.string(e.value, 'LaunchOptions'), b'--control-settings')
        self.assertEqual(vdf.string(e.value, 'AppName'), b'JCS2 mine')
        self.assertEqual(vdf.uint32_of(vdf.find(e.value, 'LastPlayTime')), 42)
        self.assertEqual(vdf.uint32_of(vdf.find(e.value, 'appid')), appid)
        self.assertEqual(result['artwork']['hero']['status'], 'preserved-user')
        self.assertEqual(result['artwork']['icon']['status'], 'preserved-user')
        self.assertFalse(os.path.exists(os.path.join(self.grid(), '%d_hero.png' % appid)))
        self.assertEqual(read(user_hero), b'user art')
        # Unregister keeps the user's files.
        self.unregister()
        self.assertEqual(read(user_hero), b'user art')
        self.assertTrue(os.path.exists(os.path.join(self.grid(), '%d_icon.png' % appid)))

    def test_owned_art_is_updated_when_source_changes(self):
        first = self.register()
        self.art('icon.png', png(6, 6))
        second = self.register()
        self.assertEqual(second['action'], 'update')
        self.assertEqual(second['artwork']['icon']['status'], 'installed')
        path = os.path.join(self.grid(), '%d_icon.png' % first['appid'])
        with open(path, 'rb') as handle:
            self.assertEqual(ss.png_size(path), (6, 6))

    def test_existing_entry_uses_stored_appid(self):
        exe = ('"%s"' % self.target).encode()
        self.write_shortcuts([entry(0, 0x80000011, b'Jet Car Stunts 2', exe)])
        result = self.register()
        self.assertEqual(result['action'], 'update')
        self.assertEqual(result['appid'], 0x80000011)
        self.assertEqual(len(self.read_entries()), 1)
        self.assertTrue(os.path.exists(os.path.join(self.grid(), '%d_icon.png' % 0x80000011)))

    def test_legacy_lowercase_keys_match(self):
        node = vdf.Node(vdf.T_MAP, b'0', [
            vdf.Node(vdf.T_STRING, b'appname', b'Jet Car Stunts 2'),
            vdf.Node(vdf.T_STRING, b'exe', self.target.encode())])
        self.write_shortcuts([node])
        result = self.register()
        (e,) = self.read_entries()
        self.assertEqual(vdf.find(e.value, 'exe').key, b'exe')
        self.assertIsNotNone(result['appid'])
        self.assertTrue(any('legacy' in w for w in result['warnings']))

    def test_steam_running_refuses_without_changes(self):
        original = self.write_shortcuts([entry(0, 5, b'a', b'"/b"')])
        self.fake_process(4242, 'steamwebhelper')
        with self.assertRaises(ss.SteamRunning):
            self.register()
        with open(self.vdf_path(), 'rb') as handle:
            self.assertEqual(handle.read(), original)
        self.assertFalse(os.path.exists(self.grid()))
        self.assertTrue(ss.plan(self.root, ACCOUNT, self.steam, proc_root=self.proc)['would_block'])

    def test_corrupt_shortcuts_refused(self):
        with open(self.vdf_path(), 'wb') as handle:
            handle.write(b'\x00shortcuts\x00\x00\x30\x00garbage')
        with self.assertRaises(ss.MalformedData):
            self.register()
        self.assertFalse(os.path.exists(self.grid()))
        self.assertFalse(os.path.exists(os.path.join(self.root, 'state', 'steam-registration.json')))

    def test_non_sequential_keys_refused(self):
        self.write_shortcuts([entry(3, 5, b'a', b'"/b"')])
        with self.assertRaises(ss.MalformedData):
            self.register()

    def test_duplicate_owned_entries_conflict(self):
        exe = ('"%s"' % self.target).encode()
        self.write_shortcuts([entry(0, 0x80000001, b'Jet Car Stunts 2', exe),
                              entry(1, 0x80000002, b'Jet Car Stunts 2', exe)])
        with self.assertRaises(ss.Conflict):
            self.register()

    def test_stale_manifest_appid_does_not_hijack(self):
        first = self.register()
        entries = self.read_entries()
        vdf.find(entries[0].value, 'Exe').value = b'"/usr/bin/someone-else"'
        self.write_shortcuts(entries)
        snapshot = read(self.vdf_path())
        with self.assertRaises(ss.Conflict):
            self.register()
        self.assertEqual(read(self.vdf_path()), snapshot)
        result = self.unregister()
        self.assertEqual(result['action'], 'absent')
        self.assertEqual(len(self.read_entries()), 1)
        self.assertEqual(first['appid'], vdf.uint32_of(vdf.find(self.read_entries()[0].value, 'appid')))

    def test_computed_appid_collision_conflict(self):
        appid = ss.shortcut_appid('"%s"' % self.target, 'Jet Car Stunts 2')
        self.write_shortcuts([entry(0, appid, b'Other', b'"/other"')])
        with self.assertRaises(ss.Conflict):
            self.register()

    def test_multiple_accounts_isolated(self):
        other = self.write_shortcuts([entry(0, 9, b'b', b'"/c"')], acc=OTHER)
        a = self.register()
        b = self.register(account=OTHER)
        self.assertEqual(a['appid'], b['appid'])
        self.assertEqual(len(self.read_entries(OTHER)), 2)
        self.unregister()
        self.assertFalse(os.path.exists(os.path.join(self.grid(), '%d_icon.png' % a['appid'])))
        self.assertEqual(len(self.read_entries(OTHER)), 2)
        self.unregister(OTHER)
        with open(self.vdf_path(OTHER), 'rb') as handle:
            self.assertEqual(handle.read(), other)

    def test_install_incomplete_refused(self):
        self.complete('staging')
        with self.assertRaises(ss.InstallIncomplete):
            self.register()
        os.unlink(os.path.join(self.root, 'install-state.json'))
        with self.assertRaises(ss.InstallIncomplete):
            self.register()
        self.assertFalse(os.path.exists(self.vdf_path()))

    def test_rollback_on_vdf_replace_failure(self):
        original = self.write_shortcuts([entry(0, 5, b'a', b'"/b"')])
        real_replace = ss._replace

        def failing(src, dst):
            if dst.endswith('shortcuts.vdf'):
                raise OSError(28, 'No space left on device')
            real_replace(src, dst)
        with mock.patch.object(ss, '_replace', failing):
            with self.assertRaises(ss.RegistrationError) as ctx:
                self.register()
        self.assertEqual(ctx.exception.code, 6)
        self.assertIn('rolled back', str(ctx.exception))
        with open(self.vdf_path(), 'rb') as handle:
            self.assertEqual(handle.read(), original)
        self.assertFalse(os.path.exists(self.grid()))
        self.assertFalse(os.path.exists(os.path.join(self.root, 'state', 'steam-registration.json')))
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.vdf_path())) if 'tmp' in n], [])

    def test_rollback_on_manifest_failure_restores_everything(self):
        original = self.write_shortcuts([entry(0, 5, b'a', b'"/b"')])
        with mock.patch.object(ss, '_save_manifest', side_effect=OSError(5, 'EIO')):
            with self.assertRaises(ss.RegistrationError):
                self.register()
        with open(self.vdf_path(), 'rb') as handle:
            self.assertEqual(handle.read(), original)
        self.assertFalse(os.path.exists(self.grid()))

    def test_steam_started_mid_transaction_rolls_back_art(self):
        self.write_shortcuts([])
        calls = {'n': 0}
        real = ss.steam_running

        def flip(root, proc_root):
            calls['n'] += 1
            return [{'pid': 1, 'name': 'steam'}] if calls['n'] > 1 else real(root, proc_root)
        with mock.patch.object(ss, 'steam_running', flip):
            with self.assertRaises(ss.SteamRunning):
                self.register()
        self.assertEqual(self.read_entries(), [])
        self.assertFalse(os.path.exists(self.grid()))

    def test_proton_override_warns_but_never_writes(self):
        appid = ss.shortcut_appid('"%s"' % self.target, 'Jet Car Stunts 2')
        config = os.path.join(self.steam, 'config', 'config.vdf')
        text = ('"InstallConfigStore"\n{\n "Software" { "Valve" { "Steam" {\n'
                ' "CompatToolMapping"\n {\n  "%d"\n  {\n   "name" "proton_9"\n  }\n }\n}}}\n}\n' % appid)
        with open(config, 'w') as handle:
            handle.write(text)
        result = self.register()
        self.assertTrue(any('Proton' in w for w in result['warnings']))
        self.assertEqual(read(config, 'r'), text)

    def test_path_validation(self):
        for bad in ('relative/path', os.path.join(self.tmp.name, 'has"quote')):
            with self.assertRaises(ss.UsageError):
                ss.register(bad, ACCOUNT, self.steam, steam_closed_confirmed=True, proc_root=self.proc)
        with self.assertRaises(ss.UsageError):
            self.register(launch_target='../escape.sh')
        outside = os.path.join(self.tmp.name, 'outside.sh')
        open(outside, 'w').close()
        os.chmod(outside, 0o755)
        os.symlink(outside, os.path.join(self.root, 'link.sh'))
        with self.assertRaises(ss.UsageError):
            self.register(launch_target='link.sh')
        os.chmod(self.target, 0o644)
        with self.assertRaises(ss.UsageError):
            self.register()
        os.chmod(self.target, 0o755)
        with self.assertRaises(ss.UsageError):
            self.register(steam_closed_confirmed=False)
        with self.assertRaises(ss.UsageError):
            self.register(account='../1')

    def test_symlinked_shortcuts_refused(self):
        real = os.path.join(self.tmp.name, 'elsewhere.vdf')
        with open(real, 'wb') as handle:
            handle.write(vdf.serialize([vdf.Node(vdf.T_MAP, b'shortcuts', [])]))
        os.symlink(real, self.vdf_path())
        with self.assertRaises(ss.MalformedData):
            self.register()


class UnregisterTests(Fixture):
    def test_removes_only_owned_entry_and_renumbers(self):
        a = entry(0, 1, b'A', b'"/a"')
        c = entry(1, 3, b'C', b'"/c"')
        a_bytes, c_bytes = vdf.serialize(a.value), vdf.serialize(c.value)
        self.write_shortcuts([a])
        result = self.register()
        entries = self.read_entries()
        entries.append(vdf.Node(vdf.T_MAP, b'2', c.value))
        self.write_shortcuts(entries)
        out = self.unregister()
        self.assertEqual(out['action'], 'remove')
        self.assertEqual(out['artwork']['icon']['status'], 'removed')
        remaining = self.read_entries()
        self.assertEqual([n.key for n in remaining], [b'0', b'1'])
        self.assertEqual(vdf.serialize(remaining[0].value), a_bytes)
        self.assertEqual(vdf.serialize(remaining[1].value), c_bytes)
        self.assertFalse(os.path.exists(os.path.join(self.grid(), '%d_icon.png' % result['appid'])))
        self.assertEqual(self.unregister()['action'], 'absent')

    def test_without_manifest_removes_nothing(self):
        exe = ('"%s"' % self.target).encode()
        original = self.write_shortcuts([entry(0, 0x80000001, b'Jet Car Stunts 2', exe)])
        self.assertEqual(self.unregister()['action'], 'absent')
        self.assertEqual(read(self.vdf_path()), original)


class DiscoveryAndCliTests(Fixture):
    def test_accounts_and_roots(self):
        with open(os.path.join(self.steam, 'config', 'loginusers.vdf'), 'w') as handle:
            handle.write('"users"\n{\n "%d"\n {\n  "PersonaName" "Tester"\n  "MostRecent" "1"\n }\n}\n'
                         % (int(ACCOUNT) + ss.STEAMID64_BASE))
        accounts = {a['account_id']: a for a in ss.list_accounts(self.steam)}
        self.assertEqual(set(accounts), {ACCOUNT, OTHER})
        self.assertTrue(accounts[ACCOUNT]['most_recent'])
        self.assertEqual(accounts[ACCOUNT]['persona_name'], 'Tester')
        self.assertFalse(accounts[OTHER]['most_recent'])
        home = os.path.join(self.tmp.name, 'home')
        os.makedirs(os.path.join(home, '.steam'))
        os.symlink(self.steam, os.path.join(home, '.steam', 'steam'))
        roots = ss.find_steam_roots(home)
        self.assertEqual(roots, [{'path': os.path.realpath(self.steam), 'kind': 'native'}])

    def test_steam_process_detection(self):
        self.assertEqual(ss.steam_running(self.steam, self.proc), [])
        self.fake_process(7, 'bash')
        self.assertEqual(ss.steam_running(self.steam, self.proc), [])
        self.fake_process(8, 'steam')
        self.assertEqual(ss.steam_running(self.steam, self.proc), [{'pid': 8, 'name': 'steam'}])

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = ss.main(list(args))
        return code, (json.loads(out.getvalue()) if out.getvalue().strip() else None)

    def test_cli_contract(self):
        common = ['--install-root', self.root, '--account', ACCOUNT, '--steam-root', self.steam,
                  '--proc-root', self.proc]
        code, out = self.cli('plan', *common, '--launch-target', 'jet-car-stunts-2.sh',
                             '--launch-options', '')
        self.assertEqual((code, out['action'], out['dry_run']), (0, 'create', True))
        self.assertFalse(os.path.exists(self.vdf_path()))
        code, out = self.cli('register', *common)
        self.assertEqual((code, out['ok'], out['error']['code']), (2, False, 2))
        code, out = self.cli('register', *common, '--launch-options', '', '--confirm-steam-closed')
        self.assertEqual((code, out['action']), (0, 'create'))
        for key in ('appid', 'shortcut_id64', 'account_id', 'shortcuts_vdf', 'backup_dir',
                    'manifest', 'artwork', 'warnings'):
            self.assertIn(key, out)
        self.fake_process(99, 'steam')
        code, out = self.cli('register', *common, '--confirm-steam-closed')
        self.assertEqual((code, out['error']['type']), (3, 'SteamRunning'))
        self.complete('failed')
        code, out = self.cli('unregister', *common, '--confirm-steam-closed')
        self.assertEqual(code, 3)
        code, _ = self.cli('bogus')
        self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()
