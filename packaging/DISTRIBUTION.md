# Single-download distribution plan

Display name everywhere: **Jet Car Stunts 2**.

The user confirmed permission to redistribute the game files and official artwork
on September 12, 2026. That question is settled. The public package should include
an authorized, verified game split set and official artwork, but must not include
the developer's personal saved guest, private backups, accounts, or credentials.

## Recommended user experience

1. Download `jet-car-stunts-2-setup.tar.gz` from the project's GitHub Release.
2. Extract it in Desktop Mode and run `setup-jcs2.sh` (the archive root; it is
   text only and about 150 KB).
3. `./setup-jcs2.sh` is a dry-run: it prints every stage and every missing piece
   without touching anything. `./setup-jcs2.sh --execute --accept-licenses
   --archives-dir <pinned archives> --payload-dir <game payload>` then fetches
   and SHA-256-verifies the pinned Android runtime from official sources, claims
   a fresh owned guest, stages the authorized game and input helper, bootstraps
   the guest, runs one lane smoke pass and writes the install markers.
4. Add the prepared **Jet Car Stunts 2** shortcut to Steam (the installer prints
   the exact `steam/steam_shortcut.py register` command), then play from Gaming
   Mode. Registering after Steam has exited also installs the approved cover,
   hero, logo and icon while preserving existing shortcuts and artwork.

The archive carries no binaries by design: the pinned runtime is fetched (or
supplied offline), and the game APK set, controller binary, helper JAR and
artwork are separate release assets staged through `--payload-dir` and
`--artwork-dir`. The preservation runner deliberately neither creates fresh
guests nor installs APKs, so first setup runs the separate fresh-guest bootstrap
(`packaging/bootstrap_linux_guest.py`) through the installer.

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

- `distribution.json`: canonical title, launch environment, installer contract,
  game-payload candidate, public exclusions, and artwork filenames/dimensions.
- `prepare_distribution.py`: generates review-only desktop/Steam metadata and
  assembles the reproducible text-only setup archive without editing Steam,
  installing anything, probing devices, or copying guest data.
- `packaging/installer/install_jcs2.py` + `packaging/installer/setup-jcs2.sh`:
  the single-file setup entry point (dry-run by default; `--execute` mutates).
  It reads `packaging/installer/runtime-lock.json`, verifies size + SHA-256,
  extracts the four pinned components, claims the guest through
  `bootstrap_linux_guest.claim_avd`, writes `jcs2-layout.json`,
  `install-state.json` and the md5 runtime manifests, bootstraps the guest,
  runs one lane smoke pass and hands the shortcut to `steam/steam_shortcut.py`.
- `steam/artwork/icon.png`: unmodified 192×192 official game icon from the
  locally extracted APK; provenance and hash beside it.
- `analysis/distribution-20260912-authorized/`: concrete reviewed desktop entry
  and JSON integration plan. It targets portable mode, which is not yet staged;
  it is not a replacement for the current working Steam shortcut.

Run `python3 packaging/prepare_distribution.py` for read-only JSON, or use
`--root /final/install/path --stage /new/review/folder` to stage metadata and the
setup archive once that tree contains its launch script. An existing stage
directory is refused, and a binary file in the shipped file list is refused.
No `shortcuts.vdf`, Steam settings, or library artwork has been modified.

Steam officially documents adding a non-Steam shortcut through its client UI.
A direct `shortcuts.vdf` writer would depend on an undocumented binary format;
this task does not pretend such an installer is already validated. Implementing
one later requires exact-entry matching, backup, fail-closed parsing, closed-Steam
checks, duplicate prevention, and tests proving unrelated entries remain unchanged.
[Steam's supported shortcut flow](https://help.steampowered.com/en/faqs/view/4B8B-9697-2338-40EC)

## Artwork status

| Asset | Prepared path | Target | Measured |
| --- | --- | --- | --- |
| Icon | `steam/artwork/icon.png` | Original 192×192 PNG | 192×192, ready |
| Cover | `steam/artwork/cover.png` | 600×900, readable game title | 600×900 |
| Landscape | `steam/artwork/landscape.png` | 920×430 | 1832×858 |
| Hero/banner | `steam/artwork/hero.png` | 3840×1240, game art without title | 1920×620 |
| Logo | `steam/artwork/logo.png` | Transparent PNG within 1280×720 | 1187×413 |

These targets follow Valve's library presentation guidelines. `steam_shortcut.py`
installs each present PNG and warns about a size that differs from the
recommendation, so the hero and landscape files still need a visual pass. The
custom non-Steam artwork files must be associated with the actual installed
shortcut; no guessed AppID filenames are being written.
[Valve library assets](https://partner.steamgames.com/doc/store/assets/libraryassets),
[Valve artwork rules](https://partner.steamgames.com/doc/store/assets/rules)

The project contains gameplay captures and extracted game textures. Candidate
hero/cover/logo files now exist in `steam/artwork/` (measured above), but visual
correctness in Desktop Steam and Gaming Mode is unverified, and no transparency
check has been run on the logo.
[Official game artwork source](https://trueaxis.com/jetcarstunts2.html)

## Remaining build work

1. Select and verify the authorized base/split APK set (candidate under
   `dist/JCS2-Windows-Personal/assets/game/`) and helper, then publish it as the
   payload asset (`--payload-dir`). The progression-unlock split is a separate
   candidate and must not be substituted implicitly. Exclude
   `assets/bootstrap/synthetic-state-normalized.tar` and all personal guest state.
2. Done in the installer: pinned official-runtime download with checksum/license
   handling, fresh guest claim, helper and game installation through
   `packaging/bootstrap_linux_guest.py`. Still open: one end-to-end
   `--execute` run on a fresh SteamOS host with the real payload, which is what
   establishes the required clean game state.
3. Done in the installer: Steam shortcut/art installation through the tested
   `steam/steam_shortcut.py` (closed-Steam check, backup, rollback). Still open:
   the final cover/hero/logo at the recommended sizes (see the table above).
4. Test a fresh SteamOS installation and upgrade/rollback without disturbing
   existing saved progress. The setup archive is assembled deterministically with
   a published SHA-256 (`.sha256` sidecar + `SHA256SUMS` inside); release notes
   and component notices still need writing before publishing.
5. Publish only the reviewed, explicit release allowlist after the package is
   complete. This project has not been published or uploaded by this work.
