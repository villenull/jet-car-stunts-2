#!/usr/bin/env python3
"""Resolve launcher paths without starting tools or modifying the guest."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, asdict
import json
import os
from pathlib import Path
import shlex
from typing import Mapping


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    layout: str
    sdk: Path
    avd_home: Path
    avd: str
    dist: Path
    controller: Path
    helper_jar: Path
    logdir: Path

    def environment(self) -> dict[str, str]:
        return {
            'ANDROID_SDK_ROOT': str(self.sdk),
            'ANDROID_HOME': str(self.sdk),
            'ANDROID_AVD_HOME': str(self.avd_home),
        }

    def missing(self) -> list[str]:
        """Check local files only; this does not validate a bootable guest."""
        errors = []
        for path in (self.sdk / 'platform-tools/adb', self.sdk / 'emulator/emulator', self.controller):
            if not path.is_file() or not os.access(path, os.X_OK):
                errors.append(f'missing executable: {path}')
        for path in (self.helper_jar, self.dist / 'controller/mapping.json',
                     self.avd_home / f'{self.avd}.ini', self.avd_home / f'{self.avd}.avd/config.ini'):
            if not path.is_file():
                errors.append(f'missing file: {path}')
        return errors


LAYOUT_FILE = 'jcs2-layout.json'


def installed_defaults(root: Path) -> dict[str, str]:
    """Read an installer-written layout marker; absent means a legacy checkout.

    Installed copies must not depend on Steam launch-option environment
    expansion. The marker only selects defaults; explicit JCS2_* values win.
    """
    path = root / LAYOUT_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f'{LAYOUT_FILE} is unreadable: {error}') from error
    if not isinstance(data, dict) or data.get('schema') != 1 or data.get('layout') != 'portable' \
            or set(data) - {'schema', 'layout', 'avd'} or not isinstance(data.get('avd', ''), str):
        raise ValueError(f'{LAYOUT_FILE} must be {{"schema": 1, "layout": "portable", "avd": NAME}}')
    defaults = {'JCS2_LAYOUT': 'portable'}
    if 'avd' in data:
        defaults['JCS2_AVD'] = data['avd']
    return defaults


def resolve_paths(root: Path | str | None = None, env: Mapping[str, str] | None = None) -> RuntimePaths:
    env = os.environ if env is None else env
    root = Path(root if root is not None else Path(__file__).resolve().parents[1]).resolve()
    env = {**installed_defaults(root), **env}
    layout = env.get('JCS2_LAYOUT', 'legacy')
    if layout not in ('legacy', 'portable'):
        raise ValueError('JCS2_LAYOUT must be legacy or portable')

    def path(key: str, default: str) -> Path:
        value = env.get(key, default)
        if not value.strip():
            raise ValueError(f'{key} must not be empty')
        candidate = Path(value).expanduser()
        return (candidate if candidate.is_absolute() else root / candidate).resolve()

    portable = layout == 'portable'
    avd = env.get('JCS2_AVD', 'hardened_api28')
    if not avd or avd in ('.', '..') or any(c in avd for c in '/\\\x00\n\r'):
        raise ValueError('JCS2_AVD must be a single profile name')
    return RuntimePaths(
        root=root, layout=layout,
        sdk=path('JCS2_SDK', 'runtime/sdk' if portable else 'analysis/arm-runtime-20260910T010130Z/runtime/sdk'),
        avd_home=path('JCS2_AVD_HOME', 'state/avd' if portable else 'analysis/hardened-runtime-20260910T150000Z/avdhome'),
        avd=avd,
        dist=path('JCS2_DIST', 'assets' if portable else '.'),
        controller=path('JCS2_CONTROLLER', 'linux-launcher/jcs2-controller-linux'),
        helper_jar=root / 'linux-launcher/jcs2-input-helper.jar',
        logdir=path('JCS2_LOGDIR', 'state/logs' if portable else 'analysis/linux-launcher/logs'),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--shell', action='store_true', help='print safely quoted Bash environment exports')
    mode.add_argument('--check', action='store_true', help='check local files only; never launch tools')
    args = parser.parse_args()
    try:
        paths = resolve_paths()
    except ValueError as error:
        parser.error(str(error))
    if args.shell:
        exports = paths.environment()
        exports.update(JCS2_LOGDIR=str(paths.logdir))
        for name, value in exports.items():
            print(f'export {name}={shlex.quote(value)}')
        print(f'export PATH={shlex.quote(str(paths.sdk / "platform-tools") + ":" + str(paths.sdk / "emulator"))}:"$PATH"')
        return 0
    result = {key: str(value) for key, value in asdict(paths).items()}
    if args.check:
        result['errors'] = paths.missing()
        result['scope'] = 'Filesystem only; AVD backing-image relocation and live operation are not verified.'
    print(json.dumps(result, indent=2))
    return 1 if args.check and result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
