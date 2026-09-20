"""Audio configuration helpers: AVD flag audit and guest media-volume seeding.

`audio_config_status` only reads.  `seed_media_volume` writes ONE guest setting
(the STREAM_MUSIC index, via the emulator's `media` tool) so the race mix is not
silenced by a stale low media volume; it never touches guest data, the AVD
profile, or host audio state.
"""
from pathlib import Path

MEDIA_STREAM_MUSIC = 3
MEDIA_VOLUME_MAX = 15
MEDIA_VOLUME_TARGET = MEDIA_VOLUME_MAX


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


def media_volume_command(target: int = MEDIA_VOLUME_TARGET) -> tuple[str, ...]:
    """ADB arguments that raise the guest STREAM_MUSIC index through its `media` tool."""
    if not 0 <= target <= MEDIA_VOLUME_MAX:
        raise ValueError(f'media volume target out of range: {target}')
    return ('shell', 'media', 'volume', '--stream', str(MEDIA_STREAM_MUSIC), '--set', str(target))


def media_volume_readback_command() -> tuple[str, ...]:
    """ADB arguments that read the guest's speaker media-volume index."""
    return ('shell', 'settings', 'get', 'system', 'volume_music_speaker')


def parse_volume_index(text: str) -> int | None:
    """Guest index from `settings get system volume_music_speaker`.

    Returns None for an unset ('null'), empty, or out-of-range value; a guest that
    has never had its media volume written reports `null`, not 0.
    """
    token = text.strip()
    if not token.isascii() or not token.isdigit():
        return None
    index = int(token)
    return index if index <= MEDIA_VOLUME_MAX else None


def seed_media_volume(adb, target: int = MEDIA_VOLUME_TARGET) -> dict[str, object]:
    """Raise the guest's STREAM_MUSIC index to `target`, then report its readback.

    `adb(*args)` is the runner's ADB callable.  Idempotent, so it is safe to call
    on every launch once the guest is up; a guest that refuses the write is
    reported as `seeded` False instead of silently claiming success.
    """
    adb(*media_volume_command(target))
    index = parse_volume_index(adb(*media_volume_readback_command()).stdout)
    return {'target': target, 'index': index, 'seeded': index == target}
