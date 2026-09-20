#!/usr/bin/env python3
"""Read-only package inventory. Never boots, copies, repairs, or rewrites a guest."""
import argparse
import json
import os
from pathlib import Path
import struct

SDK = 'analysis/arm-runtime-20260910T010130Z/runtime/sdk'
AVD = 'analysis/hardened-runtime-20260910T150000Z/avdhome'
IMAGE = 'system-images/android-28/google_apis/x86'
# Launcher payload, split by kind so the text-only setup archive can never
# carry a compiled binary. LAUNCHER_FILES is the import closure of runner.py
# plus the shell entry points (run-jcs2 -> jcs2-launcher.sh -> python-select.sh,
# steam/jcs2-steam-launch.sh -> python-select.sh); LAUNCHER_BINARIES are built
# or fetched locally and are therefore excluded from the setup archive.
LAUNCHER_FILES = [
    'linux-launcher/jcs2-launcher.sh',
    'linux-launcher/python-select.sh',
    'linux-launcher/runner.py',
    'linux-launcher/runtime_paths.py',
    'linux-launcher/progression_choice.py',
    'linux-launcher/audio_config.py',
    'linux-launcher/deck_pad.py',
    'linux-launcher/qt_settings.py',
    'linux-launcher/joystick_bridge.py',
    'linux-launcher/bridge_side_channel.py',
    'linux-launcher/gamescope_window.py',
    'linux-launcher/tilt_control/__init__.py',
    'linux-launcher/tilt_control/adapter.py',
    'linux-launcher/tilt_control/estimator.py',
    'linux-launcher/tilt_control/motion_reader.py',
    'linux-launcher/tilt_control/sensor_transport.py',
    'linux-launcher/tilt_control/settings.py',
    'linux-launcher/tilt_control/stick_gate.py',
]
LAUNCHER_BINARIES = [
    'linux-launcher/jcs2-controller-linux',
    'linux-launcher/jcs2-input-helper.jar',
]
STEAM_FILES = ['steam/jcs2-steam-launch.sh', 'steam/steam_shortcut.py', 'steam/binary_vdf.py']
FILES = ['run-jcs2'] + STEAM_FILES + LAUNCHER_FILES + LAUNCHER_BINARIES

# jet-car-stunts-2-setup.tar.gz ships text only: the Android runtime is fetched
# from official sources with the pinned hashes in installer/runtime-lock.json,
# and the game APK set, controller binary, helper JAR and artwork are separate
# release assets the installer stages from a payload directory. This one list is
# shared by the archive builder (prepare_distribution.py) and the installer's
# md5 runtime manifest, so both always describe the same shipped tree.
SETUP_ENTRY = ('packaging/installer/setup-jcs2.sh', 'setup-jcs2.sh')
INSTALLER_FILES = [
    'packaging/installer/install_jcs2.py',
    'packaging/installer/runtime-lock.json',
    'packaging/installer/licenses/android-sdk-license.txt',
    'packaging/installer/licenses/android-sdk-arm-dbt-license.txt',
    'packaging/installer/licenses/cpython-3.12-LICENSE.txt',
    'packaging/plan.py',
    'packaging/bootstrap_linux_guest.py',
    'packaging/distribution.json',
]
SETUP_FILES = ['run-jcs2', 'controller/mapping.json'] + STEAM_FILES + LAUNCHER_FILES + INSTALLER_FILES


def setup_entries():
    """Text files that ship inside jet-car-stunts-2-setup.tar.gz.

    Arcnames mirror the install tree, except for the entry script, which sits at
    the archive root so it is the first thing a user sees after extraction.
    """
    rows = [{'source': SETUP_ENTRY[0], 'destination': SETUP_ENTRY[1], 'kind': 'file'}]
    rows += [{'source': path, 'destination': path, 'kind': 'file'} for path in SETUP_FILES]
    return rows


def manifest():
    rows = [{'source': p, 'destination': p, 'kind': 'file'} for p in FILES]
    rows += [{'source': 'controller/mapping.json', 'destination': 'assets/controller/mapping.json', 'kind': 'file'}]
    rows += [{'source': SDK + '/' + p, 'destination': 'runtime/sdk/' + p, 'kind': 'tree'}
             for p in ('emulator', 'platform-tools', IMAGE)]
    rows += [{'source': AVD + '/hardened_api28' + suffix,
              'destination': 'state/avd/hardened_api28' + suffix, 'kind': kind,
              'personal_state': True} for suffix, kind in (('.ini', 'file'), ('.avd', 'tree'))]
    return rows


def inventory(path):
    """Metadata only; do not follow symlinks or inspect guest contents."""
    result = {'logical_bytes': 0, 'allocated_bytes': 0, 'files': 0, 'symlinks': []}
    if path.is_symlink():
        result['symlinks'].append(str(path))
        return result
    entries = [path]
    if path.is_dir():
        entries = []
        for directory, dirs, files in os.walk(path, followlinks=False):
            for name in dirs[:]:
                p = Path(directory) / name
                if p.is_symlink():
                    result['symlinks'].append(str(p))
                    dirs.remove(name)
            entries.extend(Path(directory) / name for name in files)
    for p in entries:
        if p.is_symlink():
            result['symlinks'].append(str(p))
        elif p.is_file():
            st = p.stat()
            result['files'] += 1
            result['logical_bytes'] += st.st_size
            result['allocated_bytes'] += st.st_blocks * 512
    return result


def backing_reference(path):
    """Read only QCOW header and bounded backing filename, never disk payload."""
    with path.open('rb') as stream:
        header = stream.read(20)
        if len(header) != 20 or header[:4] != b'QFI\xfb':
            raise ValueError('not a QCOW header')
        _, version, offset, size = struct.unpack('>4sIQI', header)
        if version not in (2, 3):
            raise ValueError('unsupported QCOW version')
        if size == 0:
            return None
        if offset < 20 or size > 4096 or offset + size > path.stat().st_size:
            raise ValueError('invalid backing filename bounds')
        stream.seek(offset)
        return stream.read(size).decode('utf-8')


def audit(root):
    root = root.resolve()
    rows = manifest()
    blockers = []
    for row in rows:
        source = root / row['source']
        if not source.exists():
            blockers.append('Missing source: ' + row['source'])
            continue
        row['inventory'] = inventory(source)
        if row['inventory']['symlinks']:
            blockers.append('Review symlink targets before copying: ' + row['source'])
    rewrites = []
    avd = root / AVD
    for relative in ('hardened_api28.ini', 'hardened_api28.avd/config.ini',
                     'hardened_api28.avd/hardware-qemu.ini'):
        path = avd / relative
        if not path.is_file() or path.is_symlink():
            continue
        for line in path.read_text().splitlines():
            key, sep, value = line.partition('=')
            value = value.strip()
            if sep and value.startswith('/'):
                destination = '<PACKAGE>/runtime/sdk' if value == str(root / SDK) else None
                if value == str(root / AVD):
                    destination = '<PACKAGE>/state/avd'
                for row in rows:
                    src = str(root / row['source'])
                    if value == src or value.startswith(src + '/'):
                        destination = '<PACKAGE>/' + row['destination'] + value[len(src):]
                        break
                staging_image = str(root / 'staging/windows-runtime/sdk' / IMAGE)
                if value.startswith(staging_image + '/') or value == staging_image:
                    destination = '<PACKAGE>/runtime/sdk/' + IMAGE + value[len(staging_image):]
                rewrites.append({'file': relative, 'key': key.strip(), 'current': value,
                                 'proposed': destination or 'UNRESOLVED',
                                 'exists': Path(value).exists(),
                                 'generated': relative.endswith('hardware-qemu.ini')})
                if not Path(value).exists():
                    blockers.append('Missing absolute AVD dependency: ' + value)
                if destination is None:
                    blockers.append('Unmapped absolute AVD dependency: ' + value)
    backing = []
    for name in ('cache.img.qcow2', 'userdata-qemu.img.qcow2', 'encryptionkey.img.qcow2'):
        path = avd / 'hardened_api28.avd' / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            reference = backing_reference(path)
            backing.append({'file': name, 'backing': reference,
                            'exists': bool(reference and (path.parent / reference).exists())})
            if reference and not (path.parent / reference).exists():
                blockers.append('Missing QCOW backing file: ' + reference)
            if reference and (Path(reference).is_absolute() or '..' in Path(reference).parts):
                blockers.append('External QCOW backing dependency requires explicit mapping: ' + reference)
        except (ValueError, UnicodeError, OSError) as exc:
            blockers.append(f'{name}: {exc}')
    blockers += ['Saved guest must be stopped and copied consistently; this tool does not establish that it is stopped.',
                 'Rewrite persistent AVD paths on a future staged copy; preserve relative QCOW backing pairs and review any external backing references.',
                 'Fresh SteamOS host dependencies and Gaming Mode/touch/save persistence remain unverified.']
    return {'mode': 'plan-only', 'manifest': rows, 'proposed_ini_rewrites': rewrites,
            'qcow_backing_references': backing, 'blockers': blockers,
            'excluded': ['backups/', 'unrelated analysis/', 'original/private backup contents',
                         'Windows runtime binaries', 'APK/bootstrap archives', 'development tests and menu overlays'],
            'note': 'AVD is a personal stateful synthetic guest, not a redistributable pristine image. No files changed.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--setup-manifest', action='store_true',
                        help='print the text-only setup archive file list and exit')
    args = parser.parse_args()
    if args.setup_manifest:
        print(json.dumps({'entry': SETUP_ENTRY[1], 'files': setup_entries()}, indent=2))
    else:
        print(json.dumps(audit(args.root), indent=2))
