#!/usr/bin/env python3
"""Shared emulator-wide Qt settings helpers for JCS2 launch paths."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

QT_CONFIG_FILENAME = "Emulator.conf"
QT_CONFIG_SUBDIR = "Android Open Source Project"


class QtSettingsError(Exception):
    """The emulator-wide Qt settings file could not be updated safely."""


def emulator_qt_config_path() -> Path:
    """Return the emulator-wide Qt settings file without touching AVD data."""
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", "") or
                        (Path.home() / ".config")).expanduser()
    return config_root / QT_CONFIG_SUBDIR / QT_CONFIG_FILENAME


def seed_compatibility_warning_suppression(
        avd: str, config_path: Path | None = None) -> tuple[Path, str, bool]:
    """Seed the exact per-AVD QSettings key before the emulator can map.

    ``displayCheckWarnings`` reads ``showCompatibilityWarning_<avd>`` from
    process-wide QSettings at the INI root. The older unqualified key under
    the ``[set]`` group does not control that lookup. Only this root key is
    added or updated; all unrelated groups and preferences remain unchanged.
    """
    path = config_path or emulator_qt_config_path()
    key = f"showCompatibilityWarning_{avd}"
    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = ""
    except OSError as error:
        raise QtSettingsError(
            f"cannot read emulator Qt settings {path}: {error}") from error

    lines = original.splitlines()
    sections = [(index, line.strip()[1:-1])
                for index, line in enumerate(lines)
                if line.lstrip().startswith("[") and line.rstrip().endswith("]")]
    first_section = sections[0][0] if sections else len(lines)
    general = next((index for index, name in sections if name == "General"), None)
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    search_ranges = [(0, first_section)]
    if general is not None:
        general_end = next((index for index, _ in sections if index > general),
                           len(lines))
        search_ranges.append((general + 1, general_end))

    changed = False
    found = False
    for start, end in search_ranges:
        for index in range(start, end):
            if key_pattern.match(lines[index]):
                found = True
                if lines[index].strip() != f"{key}=false":
                    lines[index] = f"{key}=false"
                    changed = True
                break
        if found:
            break
    if not found:
        # QSettings serializes root keys in [General] once it has written the
        # file. Seed that section when present; otherwise use the INI root so
        # the first emulator process can read it before any rewrite.
        insert_at = general + 1 if general is not None else first_section
        lines.insert(insert_at, f"{key}=false")
        changed = True

    if changed:
        content = "\n".join(lines) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            if path.exists():
                temporary.chmod(stat.S_IMODE(path.stat().st_mode))
            os.replace(temporary, path)
        except OSError as error:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise QtSettingsError(
                f"cannot seed emulator Qt settings {path}: {error}") from error
    return path, key, changed
