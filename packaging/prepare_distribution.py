#!/usr/bin/env python3
"""Stage reviewable launcher metadata only; never edit Steam or package game data."""
import argparse
import json
from pathlib import Path

METADATA = Path(__file__).with_name('distribution.json')


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


def prepare(root, metadata=None):
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
        'status': 'metadata-only; runtime preparation and first-run importer are not implemented',
        'steam_modified': False,
    }
    return desktop, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--stage', type=Path, help='write metadata into a NEW review directory')
    args = parser.parse_args()
    desktop, report = prepare(args.root)
    if args.stage:
        args.stage.mkdir(parents=True, exist_ok=False)
        (args.stage / 'jet-car-stunts-2.desktop').write_text(desktop)
        (args.stage / 'steam-integration.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
