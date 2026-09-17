"""Read emulator hardware audio flags; never alter guest or host audio state."""
from pathlib import Path


def audio_config_status(path: Path) -> dict[str, object]:
    """Report AVD audio hardware flags, using emulator defaults for absent keys."""
    values = {'hw.audioInput': 'yes', 'hw.audioOutput': 'yes'}
    for line in path.read_text(encoding='utf-8').splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(('#', ';')) or '=' not in stripped:
            continue
        key, value = stripped.split('=', 1)
        if key.strip() in values:
            values[key.strip()] = value.strip().lower()
    return {
        'config': str(path),
        'input': values['hw.audioInput'],
        'output': values['hw.audioOutput'],
        'output_disabled': values['hw.audioOutput'] in ('no', 'false', '0'),
    }
