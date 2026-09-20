#!/usr/bin/env python3
"""Stage reviewable launcher metadata and the text-only setup archive.

Never edits Steam: the metadata is a review artifact, and the setup archive is
verified to contain text files only (the pinned Android runtime is fetched from
official sources, and the game APK set, controller binary, helper JAR and
artwork ship as separate release assets). The archive is built byte-for-byte
reproducibly so its SHA-256 can be published and checked.
"""
import argparse
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile

METADATA = Path(__file__).with_name('distribution.json')
SETUP_PREFIX = 'jet-car-stunts-2'
PAYLOAD_COMMIT = 'payload-commit.txt'
SHA256SUMS = 'SHA256SUMS'


def _load_plan_module():
    """Reuse packaging/plan.py as the single source of the shipped file list."""
    spec = importlib.util.spec_from_file_location(
        "jcs2_distribution_plan", Path(__file__).with_name('plan.py'))
    if spec is None or spec.loader is None:
        raise ValueError('cannot load packaging/plan.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_plan = _load_plan_module()


def desktop_string(value):
    if '\x00' in value or '\n' in value or '\r' in value:
        raise ValueError('desktop paths cannot contain NUL or newlines')
    return value.replace('\\', '\\\\').replace('\t', '\\t')


def exec_argument(value):
    # Exec quoting is parsed after desktop string escapes; percent field codes
    # must also be escaped. No shell is involved in this launch command.
    escaped = value.replace('%', '%%')
    for char in ('\\', '"', '`', '$'):
        escaped = escaped.replace(char, '\\' + char)
    return desktop_string('"' + escaped + '"')


def is_text_file(path):
    """True when the file is UTF-8 text without NUL bytes (no binary payload)."""
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    if b'\x00' in raw:
        return False
    try:
        raw.decode('utf-8')
    except UnicodeDecodeError:
        return False
    return True


def git_commit(root):
    """Recorded shipped commit; 'unknown' outside a git checkout."""
    try:
        result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown'
    return result.stdout.strip() if result.returncode == 0 else 'unknown'


def git_dirty(root):
    """True when the shipped tree differs from the recorded commit."""
    try:
        result = subprocess.run(['git', '-C', str(root), 'status', '--porcelain'],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return bool(result.stdout.strip()) if result.returncode == 0 else None


def _tar_info(name, size, mode=0o644):
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = 0
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = ''
    info.gname = ''
    return info


def setup_members(root):
    """The archive members as (arcname, bytes, mode); refuses anything non-text."""
    root = Path(root)
    members = []
    for entry in _plan.setup_entries():
        source = root / entry['source']
        if not source.is_file():
            raise ValueError(f'setup archive source is missing: {entry["source"]}')
        if not is_text_file(source):
            raise ValueError(
                f'setup archive is text-only; refusing binary content: {entry["source"]}')
        mode = 0o755 if os.access(source, os.X_OK) else 0o644
        members.append((f'{SETUP_PREFIX}/{entry["destination"]}', source.read_bytes(), mode))
    members.sort(key=lambda item: item[0])
    return members


def write_setup_archive(root, path, commit='unknown'):
    """Write the deterministic text-only setup archive; returns its report."""
    path = Path(path)
    members = setup_members(root)
    payload_commit = (commit or 'unknown').strip() or 'unknown'
    generated = [
        (f'{SETUP_PREFIX}/{PAYLOAD_COMMIT}', (payload_commit + '\n').encode('utf-8'), 0o644),
        (f'{SETUP_PREFIX}/{SHA256SUMS}',
         ''.join(f'{hashlib.sha256(data).hexdigest()}  {name[len(SETUP_PREFIX) + 1:]}\n'
                 for name, data, _ in members).encode('utf-8'), 0o644),
    ]
    rows = sorted(generated + members, key=lambda item: item[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'wb') as handle:
        with gzip.GzipFile(filename='', mode='wb', fileobj=handle, mtime=0, compresslevel=9) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive:
                for name, data, mode in rows:
                    archive.addfile(_tar_info(name, len(data), mode), io.BytesIO(data))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sidecar = path.with_name(path.name + '.sha256')
    sidecar.write_text(f'{digest}  {path.name}\n')
    return {
        'archive': str(path),
        'asset': path.name,
        'sha256': digest,
        'sha256_file': str(sidecar),
        'bytes': path.stat().st_size,
        'prefix': SETUP_PREFIX,
        'entry': f'{SETUP_PREFIX}/{_plan.SETUP_ENTRY[1]}',
        'payload_commit': payload_commit,
        'files': len(rows),
        'binary_content': False,
        'binary_entries': 0,
    }


def prepare(root, metadata=None, archive_dir=None, commit=None):
    metadata = json.loads(METADATA.read_text()) if metadata is None else metadata
    root = root.resolve()
    target = root / metadata['launch_target']
    if not target.is_file():
        raise ValueError(f'missing launch target: {target}')
    desktop = ('[Desktop Entry]\nType=Application\nVersion=1.0\n'
               f'Name={metadata["title"]}\n'
               'Comment=Launch the configured personal Android game\n'
               f'Exec=/usr/bin/env JCS2_LAYOUT=portable {exec_argument(str(target))}\n'
               f'Path={desktop_string(str(root))}\n'
               'Terminal=false\nCategories=Game;\n')
    icon = root / metadata['artwork']['icon']['path']
    if icon.is_file():
        desktop += f'Icon={desktop_string(str(icon))}\n'
    report = {
        'title': metadata['title'], 'package_root': str(root),
        'steam_shortcut': {'name': metadata['title'], 'target': str(target),
                           'start_in': str(root), 'launch_options': 'JCS2_LAYOUT=portable %command%',
                           'compatibility': 'native Linux; no Proton override'},
        'artwork': {key: dict(spec, exists=(root / spec['path']).is_file())
                    for key, spec in metadata['artwork'].items()},
        'status': 'metadata + text-only setup archive; installation runs through the '
                  'archive entry point (packaging/installer/install_jcs2.py)',
        'steam_modified': False,
    }
    if archive_dir is not None:
        archive_dir = Path(archive_dir)
        asset = metadata['release_asset']
        recorded = commit if commit is not None else git_commit(root)
        report['installer'] = write_setup_archive(root, archive_dir / asset, recorded)
        report['installer']['release_status'] = metadata.get('release_status')
        # A tree that differs from the recorded commit still gets an artifact,
        # but the commit is then context only: the archive's own SHA256SUMS is
        # what identifies its contents.
        report['installer']['tree_dirty'] = git_dirty(root)
    return desktop, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--stage', type=Path, help='write metadata into a NEW review directory')
    parser.add_argument('--no-archive', action='store_true',
                        help='stage metadata only; skip the setup archive')
    args = parser.parse_args()
    if args.stage:
        args.stage.mkdir(parents=True, exist_ok=False)
    stage = None if (args.no_archive or not args.stage) else args.stage
    desktop, report = prepare(args.root, archive_dir=stage)
    if args.stage:
        (args.stage / 'jet-car-stunts-2.desktop').write_text(desktop)
        (args.stage / 'steam-integration.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
