# Steam library artwork slots

`steam/steam_shortcut.py register` copies these optional PNG files into the
selected account's `userdata/<account>/config/grid/`, named after the AppID
**stored in the Jet Car Stunts 2 shortcut entry** (never a guessed ID). Drop in
replacements with the same filenames; a missing file is skipped with a warning
and does not block registration.

| Slot | Source file | Grid filename | Recommended |
| --- | --- | --- | --- |
| Portrait cover | `cover.png` | `<appid>p.png` | 600×900 |
| Landscape / recent | `landscape.png` | `<appid>.png` | 920×430 |
| Hero | `hero.png` | `<appid>_hero.png` | 3840×1240, no title text |
| Logo | `logo.png` | `<appid>_logo.png` | transparent, within 1280×720 |
| Icon | `icon.png` | `<appid>_icon.png` + shortcut `icon` field | square (current: 192×192 original) |

PNG only; other sizes install with a warning. Existing grid files for the
same slot (any of .png/.jpg/.jpeg/.webp) that the installer did not write are
kept, as is a user-chosen shortcut icon. On re-install, files the installer
previously wrote (SHA-256 recorded in `state/steam-registration.json`) are
refreshed from updated sources. Unregister removes only unmodified installer
files. Visual correctness in Desktop Steam and Gaming Mode is still unverified.
