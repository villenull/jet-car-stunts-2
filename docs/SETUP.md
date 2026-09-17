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
saves, or the DB86 signing private key. Paths below are absolute on the
owner's machine (`/home/deck/Projects/JCS2/...`, Steam
`/home/deck/.local/share/Steam/...`, generated images under
`/home/deck/.codex/generated_images/...`) — re-provision them locally;
do not hard-code another machine's paths into tracked files.

Provision the owner game payload, saves, and signing key only from the
owner's local holders: USB backup
(`backups/usb-20260910T005449Z/`: base `41043f68…`, armeabi
`d1ceccba…`, en `9d72176b…`, es `1000baf0…`, xhdpi `59da6a86…`;
pristine lib `bc7fdf9d…`, BuildID `39f1603f…`), local AVD trees, and
the DB86 private key
(`DB:86:A5:6E:99:37:04:51:84:A3:B5:B8:E3:64:E3:0F:53:33:A3:0A:FD:A1:27:39:E1:1E:71:78:B4:AD:33:ED`).
Never fabricate, substitute, or rotate these. Platform SDK pieces and
language toolchains (Android SDK / command-line tools, emulator, system
images, Go, Python, LLVM) may be fetched from their official upstream
sources.

## 2. Truthful command matrix

| Command | Needs provisioning? | What it proves |
|---|---|---|
| `python3 run-offline-tests.py` | No (stdlib + repo) — expect `OK: 449 unittest cases across 16 suites` | offline behavior only; not GamingMode/touch/tilt/audio |
| `python3 linux-launcher/runtime_paths.py --check` | Yes (SDK/AVD/assets) — fails closed when missing | filesystem preflight only; passing ≠ bootable/self-contained |
| `python3 packaging/plan.py` | Yes (source tree) — prints JSON report; exit 0 ≠ blockers resolved | read-only inventory + proposed AVD rewrites; copies/repairs nothing |
| `./run-jcs2` / `./run-jcs2 --headless --input qa` | Yes (full guest + ports 5038/5594/5595 free + locks) | live guest; user-driven only, never automatic |
| Steam shortcut `steam/jcs2-steam-launch.sh` | Yes (Steam UI shortcut + ports free) | user launches from Steam; agents never launch/stop Steam |
| `game-fixes/*/patch_*.py --check`, `verify_*.py`, `check_combined.py` | Yes (lib/APK copies; `/tmp` scratch) | byte/hash/llvm guards; never touch backups in place |

Host prerequisites: Python 3 (3.14 is this host's version; the minimum
version the suites are known-good on is not established), Steam Deck / Linux
with X11 + Gamescope for GamingMode, Android SDK platform-tools + API28
emulator pieces (provisioned locally or fetched from official upstream
sources), optional Tk/Tcl for `--control-settings` panel,
`llvm-objdump/llvm-readelf` for native-byte verification.
`controller/go.mod` declares Go 1.21 as the module minimum, but no Go
toolchain is installed on this host — do not claim a fresh Go rebuild.
Rebuilding `linux-launcher/jcs2-input-helper.jar` from
`controller/helper-src/Jcs2InputHelper.java` additionally needs Android SDK
build-tools (javac against `android.jar`, then `d8` dexing for
`classes.dex`); no build script for that recipe exists in this repo yet.

## 3. First-time setup (owner-provisioned machine)

1. Clone, then provision `backups/`, `runtime/sdk`, AVD home, and signing key:
   owner APK/saves/key only from local holders; platform SDK and language
   toolchains may come from official upstream sources (see §1).
2. `python3 run-offline-tests.py` → expect 449/449.
3. `python3 linux-launcher/runtime_paths.py --check` → resolve missing paths
   before any launch; default layout keeps the existing authorized guest,
   `JCS2_LAYOUT=portable` only after a staged package is prepared.
4. Steam: add `steam/jcs2-steam-launch.sh` as a non-Steam shortcut named
   `Jet Car Stunts 2` (appid `4192595540`), no Proton override; optional
   Launch Options `--control-settings` for the prelaunch TiltDrive panel.
   Steam closed during any VDF/grid edit; one normal user Steam launch picks
   up name/art.
5. In-session TiltDrive switch: pause (Menu/START) → View → Left joystick or
   Tilt → Apply & Return → touch Resume. In-game Gamepad stays ON; game OFF
   is not used. Deleting the settings file returns to Gamepad.
6. Upgrades only: verify installed splits uniform DB86 + versionCode 29,
   back up host AVD dir, `install-multiple -r` the 5 signed splits, re-verify
   5/5 hashes + cert + versionCode. Never uninstall/clear; never reinvent the
   key.

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
- No installer is built here; same-key upgrades run under the existing
  authorization with the §3 gate evidence (personal stable LOW / unlock
  baseline already installed — no duplicate redeploy).
