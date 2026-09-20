# JCS2 setup — provisioning, commands, fresh-clone limits

Date: 2026-09-16. Companion to `README.md` (current truth) and
`docs/CONTINUATION.md` (full handoff). No license choice is made.

## 1. What a fresh clone gives you vs what must be provisioned

A fresh clone contains source, docs, and tooling only:

```text
run-jcs2  run-offline-tests.py  linux-launcher/  controller/
game-fixes/  packaging/  steam/  artwork/  docs/
```

(Historical root `*.md` stay local-only on the owner's machine; they are not
tracked and are not part of a fresh clone.)

It does **not** contain and must not invent: `backups/` (USB original APKs,
private saves, Steam VDF/grid backups), `staging/` (scratch AVDs, composed
APK sets, run evidence), `analysis/` (raw logs/screenshots), `runtime/sdk`
(emulator, platform-tools, system images), AVD disk images, pulled splits,
saves, or the signing private key. Paths below are absolute on the
owner's machine (`/home/deck/Projects/JCS2/...`, Steam
`/home/deck/.local/share/Steam/...`, generated images under
`/home/deck/.codex/generated_images/...`) — re-provision them locally;
do not hard-code another machine's paths into tracked files.

Provision the owner game payload, saves, and signing key only from the
owner's local holders. **Corrected 2026-09-20:** the USB holder this section
used to name (`backups/usb-20260910T005449Z/`) and the DB86 key material were
destroyed in the 2026-09-17 Deck wipe. The current holders are the verified
off-Deck backup at `/home/villenull/Projects/jet-car-stunts-2/.state/jcs2-work-20260920/deck-backup-20260920/` (game payload,
guest saves, `jcs2fresh` signing key, both emulator SDK trees, runtime and
lane logs — manifest + mapping in that directory; exact inventory, hashes and
the full redeploy recipe are in `docs/CONTINUATION.md` §9.4–§9.6) and the
pristine APK copy in `~/Downloads/com.trueaxis.jetcarstunts2.apk`
(`sha256 779f2103…7c30fe`). The live install is signed with `jcs2fresh`
(cert `47:B6:…:C6:67`), **not** DB86. Never fabricate, substitute, or rotate
either key. Platform SDK pieces and
language toolchains (Android SDK / command-line tools, emulator, system
images, Go, Python, LLVM) may be fetched from their official upstream
sources.

## 2. Truthful command matrix

| Command | Needs provisioning? | What it proves |
|---|---|---|
| `python3 run-offline-tests.py` | No (stdlib + repo) — expect `OK: 547 unittest cases across 17 suites` | offline behavior only; not GamingMode/touch/tilt/audio |
| `python3 linux-launcher/runtime_paths.py --check` | Yes (SDK/AVD/assets) — fails closed when missing | filesystem preflight only; passing ≠ bootable/self-contained |
| `python3 packaging/plan.py` | Yes (source tree) — prints JSON report; exit 0 ≠ blockers resolved | read-only inventory + proposed AVD rewrites; copies/repairs nothing |
| `./packaging/installer/setup-jcs2.sh` (dry-run) | No (stdlib + extracted setup tree) — exit 1 lists the missing pieces | what the installer WOULD do; touches nothing |
| `python3 packaging/prepare_distribution.py --stage DIR` | No — writes a review dir | desktop/Steam metadata + the text-only `jet-car-stunts-2-setup.tar.gz` with its SHA-256 |
| `./run-jcs2` / `./run-jcs2 --headless --input qa` | Yes (full guest + ports 5038/5594/5595 free + locks) | live guest; user-driven only, never automatic |
| Steam shortcut `steam/jcs2-steam-launch.sh` | Yes (Steam UI shortcut + ports free) | user launches from Steam; agents never launch/stop Steam |
| `game-fixes/*/patch_*.py --check`, `verify_*.py`, `check_combined.py` | Yes (lib/APK copies; `/tmp` scratch) | byte/hash/llvm guards; never touch backups in place |

Host prerequisites: Python 3 (3.14 is this host's version; the minimum
version the suites are known-good on is not established), Steam Deck / Linux
with X11 + Gamescope for GamingMode, Android SDK platform-tools + API28
emulator pieces (provisioned locally or fetched from official upstream
sources), `llvm-objdump/llvm-readelf` for native-byte verification.
`controller/go.mod` declares Go 1.21 as the module minimum, but no Go
toolchain is installed on this host — do not claim a fresh Go rebuild.
Rebuilding `linux-launcher/jcs2-input-helper.jar` from
`controller/helper-src/Jcs2InputHelper.java` additionally needs Android SDK
build-tools (javac against `android.jar`, then `d8` dexing for
`classes.dex`); no build script for that recipe exists in this repo yet.

## 3. First-time setup (owner-provisioned machine)

1. Clone, then provision `backups/`, the emulator SDK (the dev checkout the Steam
   wrapper targets uses `state/emulator-ab-10696886`; an installed copy stages
   `runtime/sdk`), AVD home (`state/avd`, profile `jcs2-fresh`) and signing key:
   owner APK/saves/key only from local holders; platform SDK and language
   toolchains may come from official upstream sources (see §1).
2. `python3 run-offline-tests.py` → expect 547/547.
3. `python3 linux-launcher/runtime_paths.py --check` → resolve missing paths
   before any launch. The Steam wrapper and the portable lane default to
   `JCS2_LAYOUT=portable` against `state/avd`, `assets` and `state/logs` with the
   prepared legacy32 SDK in `state/emulator-ab-10696886`;
   `JCS2_LAYOUT=legacy` keeps the existing authorized guest on the older SDK.
4. Steam: add `steam/jcs2-steam-launch.sh` as a non-Steam shortcut named
   `Jet Car Stunts 2`, no Proton override, **Launch Options empty**. The appid is
   derived from the absolute target path plus the shortcut name — `plan` prints
   it (3832889443 for `/home/deck/Projects/JCS2/steam/jcs2-steam-launch.sh`) and
   the artwork grid filenames follow it. The wrapper supplies the Gaming Mode
   lane itself — portable layout, `jcs2-fresh` profile, the prepared legacy32 SDK
   in `state/emulator-ab-10696886`, `QT_XCB_NO_XI2=1`, and `--input joystick` —
   so no launch option is needed. Steam Input stays off for the shortcut (the
   non-Steam default); nothing here changes a Steam setting. Artwork comes from
   `steam/artwork/` (`cover.png`, `landscape.png`, `hero.png`, `logo.png`,
   `icon.png`) and is installed into the account's grid by
   `steam/steam_shortcut.py register`; that command needs `install-state.json`
   holding `{"status": "complete"}` at the install root (written by the
   installer's finalize stage) and otherwise refuses with exit 7, while `plan`
   stays a read-only dry run. Steam closed during any VDF/grid edit; one normal
   user Steam launch picks up name/art.
5. Driving path: there is no SELECT-button controls panel and no
   `--control-settings` pre-launch screen — the lane starts directly on the
   physical joystick path. In-game Gamepad stays ON; game OFF is not used, and
   Tilt Drive stays unavailable: the game's sensor-landscape window lets tilt
   re-derive the display and the picture flips on every lift, and this AVD
   image exposes no rotation-hold command.
6. Upgrades only: verify the installed package's cert matches the live key
   `jcs2fresh` (`47:B6:…:C6:67` — **not** DB86, which died in the 2026-09-17
   wipe) plus `versionCode 29`, back up the host AVD dir, `install-multiple -r`
   the approved splits, re-verify hash + cert + versionCode. Never
   uninstall/clear; never reinvent or rotate the key.

## 4. Fresh-clone limitations (do not overclaim)

- Offline suites pass without provisioning, but prove no physical behavior.
- `--check`/planner success does not prove the AVD boots, images are
  self-contained, GLIBC 2.34-era binaries run on fresh SteamOS, or saves
  migrate — those are user-operated live checks.
- AVD `.ini`/`config.ini` absolute paths and QCOW overlay/base pairs must be
  restaged per `packaging/` guidance; sparse-aware copies matter (≈11 GiB
  logical / ≈5 GiB allocated for runtime+guest before small files).
- Release builds deny `run-as`; isolated-guest file evidence uses `adb root`
  pull (or `adb backup` fallback) — never the personal backup route.
- The single-file setup entry point is `packaging/installer/setup-jcs2.sh` +
  `packaging/installer/install_jcs2.py` (stdlib only; dry-run by default,
  `--execute` mutates). It is verified as a dry-run against a freshly extracted
  `jet-car-stunts-2-setup.tar.gz`; a full `--execute` on a fresh SteamOS host has
  not been run yet. Same-key upgrades still run under the existing authorization
  with the §3 gate evidence (personal stable LOW / unlock baseline already
  installed — no duplicate redeploy).
