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
FILES = ['run-jcs2', 'steam/jcs2-steam-launch.sh'] + [
    'linux-launcher/' + name for name in (
        'runner.py', 'runtime_paths.py', 'controls_settings.py', 'progression_choice.py', 'audio_config.py', 'jcs2-launcher.sh', 'joystick_bridge.py',
        'bridge_side_channel.py', 'gamescope_window.py', 'jcs2-controller-linux',
        'jcs2-input-helper.jar', 'tilt_control/__init__.py', 'tilt_control/adapter.py',
        'tilt_control/estimator.py', 'tilt_control/motion_reader.py', 'tilt_control/sensor_transport.py', 'tilt_control/settings.py')]


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
    args = parser.parse_args()
    print(json.dumps(audit(args.root), indent=2))
