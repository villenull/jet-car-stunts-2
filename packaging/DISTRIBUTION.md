# Single-download distribution plan

Display name everywhere: **Jet Car Stunts 2**.

The user confirmed permission to redistribute the game files and official artwork
on September 12, 2026. That question is settled. The public package should include
an authorized, verified game split set and official artwork, but must not include
the developer's personal saved guest, private backups, accounts, or credentials.

## Recommended user experience

1. Download `jet-car-stunts-2-setup.tar.gz` from the project's GitHub Release.
2. Extract it in Desktop Mode and open **Install Jet Car Stunts 2**.
3. Choose an install folder. First setup downloads the pinned Android runtime
   from official sources with its applicable license acceptance, creates a fresh
   guest, and installs the bundled authorized game and input helper.
4. Add the prepared **Jet Car Stunts 2** shortcut to Steam, then play from Gaming
   Mode. The final installer should configure the approved cover, hero, logo and
   icon after Steam has exited, preserving existing shortcuts and artwork.

This is one user-facing setup download; first setup still needs internet for
runtime dependencies. It is a target workflow, not a working installer today.
The available preservation runner deliberately neither creates fresh guests nor
installs APKs, so that bootstrap must be built and tested separately.

GitHub permits up to 1000 assets in a Release, but each must be **under 2 GiB**.
The current personal runtime/guest is about 11 GiB logical and 5 GiB allocated;
its compressed size has not been measured. A single all-inclusive archive cannot
be promised before measuring the actual distributable payload. Separate runtime
fetches keep the setup asset small without requiring the user to assemble parts.
[GitHub Release limits](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)

The downloaded Android SDK's terms restrict redistribution, except where
applicable third-party licenses provide rights. The local emulator has an Apache
license file and third-party notices, but that does not establish permission for
the whole downloaded Google APIs system-image bundle. Downloading the runtime
from the official source during setup is the current plan; fully bundled offline
runtime requires component-level review first.
[Android SDK terms, section 3](https://developer.android.com/studio/terms)

## Prepared now

- `distribution.json`: canonical title, launch environment, game-payload candidate,
  public exclusions, and artwork filenames/dimensions.
- `prepare_distribution.py`: generates review-only desktop/Steam metadata without
  editing Steam, installing anything, probing devices, or copying guest data.
- `steam/artwork/icon.png`: unmodified 192×192 official game icon from the
  locally extracted APK; provenance and hash beside it.
- `analysis/distribution-20260912-authorized/`: concrete reviewed desktop entry
  and JSON integration plan. It targets portable mode, which is not yet staged;
  it is not a replacement for the current working Steam shortcut.

Run `python3 packaging/prepare_distribution.py` for read-only JSON, or use
`--root /final/install/path --stage /new/review/folder` to stage metadata once
that tree contains its launch script. An existing stage directory is refused.
No `shortcuts.vdf`, Steam settings, or library artwork has been modified.

Steam officially documents adding a non-Steam shortcut through its client UI.
A direct `shortcuts.vdf` writer would depend on an undocumented binary format;
this task does not pretend such an installer is already validated. Implementing
one later requires exact-entry matching, backup, fail-closed parsing, closed-Steam
checks, duplicate prevention, and tests proving unrelated entries remain unchanged.
[Steam's supported shortcut flow](https://help.steampowered.com/en/faqs/view/4B8B-9697-2338-40EC)

## Artwork still required

| Asset | Prepared path | Target |
| --- | --- | --- |
| Icon | `steam/artwork/icon.png` | Original 192×192 PNG, ready |
| Cover | `steam/artwork/cover.png` | 600×900, readable game title |
| Hero/banner | `steam/artwork/hero.png` | 3840×1240, game art without title |
| Logo | `steam/artwork/logo.png` | Transparent PNG within 1280×720 |

These targets follow Valve's library presentation guidelines. The custom
non-Steam artwork files must be associated with the actual installed shortcut;
no guessed AppID filenames are being written.
[Valve library assets](https://partner.steamgames.com/doc/store/assets/libraryassets),
[Valve artwork rules](https://partner.steamgames.com/doc/store/assets/rules)

The project contains gameplay captures and extracted game textures. No finished
hero/cover/game-title logo was found in the shipping tree. The official game
page also links screenshots/artwork. New assets still need preparation and
visual review; an icon alone does not finish the Steam presentation.
[Official game artwork source](https://trueaxis.com/jetcarstunts2.html)

## Remaining build work

1. Select and verify the authorized base/split APK set (candidate under
   `dist/JCS2-Windows-Personal/assets/game/`) and helper. The progression-unlock
   split is a separate candidate and must not be substituted implicitly. Exclude
   `assets/bootstrap/synthetic-state-normalized.tar` and all personal guest state.
2. Build pinned official-runtime download, checksum/license handling, fresh guest
   initialization, helper installation and game installation. Establish required
   clean game state without copying a developer's profile.
3. Complete cover/hero/logo and implement tested Steam shortcut/art installation
   or retain the documented manual Add Non-Steam flow for the first release.
4. Test a fresh SteamOS installation and upgrade/rollback without disturbing
   existing saved progress. Measure the final compressed setup asset and write
   SHA256 checksums, release notes, and component notices.
5. Publish only the reviewed, explicit release allowlist after the package is
   complete. This project has not been published or uploaded by this work.
