# JCS2 repository contents — publication manifest

This document describes what the `main` branch tracks, what stays local-only,
and why. Tracked: original authored source, patch/compose/verify scripts,
manifests, sanitized provenance, and approved artwork. Not tracked: game
binaries, device backups, saved guests, SDK/toolchain trees, key material,
logs, and scratch output. No license is declared here — none is invented.

Ancestor `AGENTS.md` check (this checkout, its parents, `/home/deck`):
none exists, so no ancestor instructions apply.

## Staging method (explicit allowlist, no blanket add)

- `.gitignore` carries denies only (binaries, payloads, keys, heavy trees,
  logs, build outputs — see it for exact patterns). It contains no `!`
  negations by design: a negation after a deny could re-include a secret or
  binary, so exceptions are allowlisted with `git add -f <path>` instead.
- Files enter the index only via explicit `git add <path>` per category
  below. `git add -A` / `git add .` is not used.
- Root status reports (`HANDOFF.md`, `PROJECT-STATUS.md`, `RELEASE-CHECKLIST.md`,
  `LINUX-STATUS.md`, `LIGHTWEIGHT-OPTIONS.md`, `WINDOWS-HANDOFF.md`,
  `PAID-LEVEL-MAP.md`, `PAID-LEVEL-MAP-evidence.txt`) stay local-only: they are
  superseded historical notes, not tracked unless explicitly curated.
  `README.md`, `docs/CONTINUATION.md`, and `docs/SETUP.md` are the curated
  entry points and ARE tracked.

## INCLUDED — tracked paths

| Category | Tracked paths |
|---|---|
| Repo config + docs | `.gitignore`, `README.md`, `docs/REPOSITORY-CONTENTS.md`, `docs/CONTINUATION.md`, `docs/CONTINUATION-2026-09-18.md`, `docs/SETUP.md`, `docs/RESTORE-2026-09-17.md`, `docs/PAUSED-2026-09-17.md`, `docs/REINSTALL-2026-09-17.md` |
| Offline test entrypoints | `run-offline-tests.py`, `run-jcs2` |
| Controller (Go + Java helper source) | `controller/*.go`, `controller/go.mod`, `controller/mapping.json`, `controller/example.ndjson`, `controller/cmd/jcs2-controller/*.go`, `controller/helper-src/Jcs2InputHelper.java`, `controller/*.md` |
| Windows launcher (source only) | `launcher/launcher.cpp`, `launcher/logic.hpp`, `launcher/build.ps1`, `launcher/prepare-personal-runtime.ps1`, `launcher/launcher.ini.example`, `launcher/README.md`, `launcher/CURRENT-STATUS.md`, `launcher/support/JCS2-Start-and-Collect.cmd`, `launcher/support/Start-and-Collect.ps1`, `launcher/tests/test_launcher.py`, `launcher/tests/logic_test.cpp`, `launcher/tests/windows-mock/run_windows_mock.py`, `launcher/tests/windows-mock/mock_controller.cpp`, `launcher/tests/windows-mock/mock_tool.cpp` |
| Linux launcher (Python source only) | `linux-launcher/*.py` (incl. `runner.py`, `runtime_paths.py`, `joystick_bridge.py`, `gamescope_window.py`, `bridge_side_channel.py`, `progression_choice.py`, `audio_config.py`, `menu_*.py`), `linux-launcher/*.sh`, `linux-launcher/navigation_v2/*.py`, `linux-launcher/overlay/*.py`, `linux-launcher/tilt_control/*.py`, `linux-launcher/tests/*.py`, `linux-launcher/README.md` |
| Steam integration (source only) | `steam/*.py`, `steam/*.sh`, `steam/*.md`, `steam/controller-notes.txt`, `steam/artwork/ASSETS.md`, `steam/artwork/PROVENANCE.md`, `steam/artwork/icon.png` (192×192 unmodified game icon, hash in PROVENANCE.md) |
| Packaging / distribution planning | `packaging/*.py`, `packaging/*.md`, `packaging/*.json`, `packaging/installer/runtime-lock.json`, `packaging/installer/licenses/*.txt` |
| Game-fix patch scripts + manifests | `game-fixes/**/[*.py, *.sh, *.json, *.md]` across `audio/`, `controls/`, `editor-save/`, `maps/`, `personal-deploy/` — patch/compose/verify/test scripts and sanitized JSON/MD manifests only |
| Progression-unlock scripts | `progression-unlock/*.py`, `progression-unlock/assets/candidate.json` (hashes/offsets only) |
| Developer-only Deck display pins | `dev-deck-display/README.md`, `dev-deck-display/gamescope-session-edp1`, `dev-deck-display/override.conf`, `dev-deck-display/monitors-dp1-disable.snippet.lua` (Deck-local config, NOT distributed) |

Deliberately NOT tracked (present locally, excluded by .gitignore):
`.exe`, `jcs2-controller-linux`, `*.jar`, `*.apk`, `*.so*`, `*.patched`,
`*.zip`, `__pycache__`, test-output trees, the superseded wide v1 master,
both baked-checkerboard REJECTED logo renders.

## EXCLUDED — stays local, never staged

| Exclusion class | Local paths (left in place, not modified by repo prep) |
|---|---|
| Full device/PC backups + images | `backups/` (incl. `private-direct-*`, `private-jcs2-*`, `20260909T211807Z`, `usb-*`, `steam-grid-*`, `steam-shortcut-*`), `backup.ab`, `androguard.db` |
| Runtime SDK / toolchains / guests | `runtime/` (bundled CPython 3.12 tree), `tools/` (go tarball+tree, llvm-mingw, platform-tools), `dist/` (Windows personal tree + SDK/emulator + APK sets) |
| Analysis + staging scratch | `analysis/` (emulator logs, AVDs, APK copies, owneronly key dir, third-party APKs), `staging/` (per-attempt APK sets, `.so` candidates, videos, mem dumps, incl. the former `staging/editor-save-20260915T010000Z` compose stage, gone since the 2026-09-17 wipe) |
| Decompile/binary drops | `luna-inspection-20260909T212750Z/`, `opus-review-20260909T213500Z/` |
| Proprietary payloads inside staged trees | `game-fixes/audio/candidate*/libtrueaxis.so.patched` + `provenance.json`/`REJECTED.json` beside them; `progression-unlock/assets/*.apk`, `*.png`, `*.log` (evidence captures) |
| Built Windows/Linux binaries | `launcher/*.exe`, `linux-launcher/jcs2-controller-linux`, `linux-launcher/jcs2-input-helper.jar` (build output; source `.java` IS tracked) |
| Windows-mock test output | `launcher/tests/windows-mock/analysis/` (incl. `prefix-Fresh/` Wine symlinks + `drive_c` DLLs, per-case `assets/bootstrap/*.apk`, `synthetic-data.tar`) |
| Derived artwork bundles / non-final masters | `artwork/steam-proposal/*.zip`, `artwork/steam-proposal/revised/*.zip`, `artwork/steam-proposal/originals/*.{jpg,jpeg,png,webp}`, superseded `revised-20260915/wide-generated-master-1833x858.png`, both `revised-20260915/logo-generated-REJECTED-*.png` |
| Key material / private analysis | `analysis/offline-apk-hardening-*/owneronly/` (contains the local-developer `.p12`, repacked APKs), `analysis/*/signer-cert.pem`, `community-signer.pem`, any `private-*` dirs |
| Logs, caches, build outputs | `**/__pycache__/`, `*.pyc`, `*.log`, `**/logs/`, `**/run-*/` |
| Superseded root reports (local-only) | `HANDOFF.md`, `PROJECT-STATUS.md`, `RELEASE-CHECKLIST.md`, `LINUX-STATUS.md`, `LIGHTWEIGHT-OPTIONS.md`, `WINDOWS-HANDOFF.md`, `PAID-LEVEL-MAP.md`, `PAID-LEVEL-MAP-evidence.txt` |

Local asset preservation: everything above stays on the owner's machine under
`/home/deck/Projects/JCS2/` and is not modified by repo preparation. To
re-derive excluded artifacts: Windows binaries rebuild from `launcher/*.cpp` +
`controller/` via `build.ps1` with a Go toolchain; `jcs2-controller-linux`
rebuilds with `go build ./...` from `controller/` (`go.mod` declares the Go
1.21 module minimum); the helper JAR has no in-repo build script — rebuilding
it from `controller/helper-src/Jcs2InputHelper.java` needs Android SDK
build-tools (javac against `android.jar`, then `d8` dexing for `classes.dex`);
mock-test output regenerates via
`launcher/tests/windows-mock/run_windows_mock.py`; `.so.patched` candidates
regenerate via `game-fixes/*/compose_*.py` + `patch_*.py` given the owner's
local authorized APK (not in repo); artwork zips regenerate via
`artwork/steam-proposal/build_proposal.py`. Platform SDK pieces and language
toolchains may be fetched from official upstream sources; only the owner game
payload, saves, and signing key are private-provision.

## Exclusion audit (tracked content only)

- Secrets: pattern sweep for `PRIVATE KEY`, `ghp_`, `github_pat_`, `xox*`,
  `AIza*`, `BEGIN CERTIFICATE` over tracked trees. Known hits are all
  non-secret: certificate-block regex literals inside
  `game-fixes/maps/compose_maps_candidate.py` and
  `linux-launcher/progression_choice.py` (parsers, no key material), and this
  manifest's own description of the sweep. No `.jks`/`.keystore`/`.p12`/
  `.pfx`/`.pem`/`.key` files in any tracked tree. The
  `analysis/.../owneronly/*.p12` and `*.pem` files are outside tracked trees
  and excluded. Tracked DB86 references are certificate fingerprints / hashes
  (verifiers), never private key material.
- Large files: no tracked file exceeds 10 MB. Largest are artwork PNGs (wide
  v2 master 2.1 MB, `revised/library-cover.png` 1.9 MB). All `*.apk` /
  `*.so.patched` / `*.exe` / controller binary / `*.zip` are excluded
  (checked with `git check-ignore`).
- Symlinks: the only symlinks under staged trees' directories are the 8
  Wine-prefix fixtures under the excluded
  `launcher/tests/windows-mock/analysis/.../prefix-Fresh/dosdevices/`.
  No symlinks are tracked (checked with `git ls-files -s`).

## Fresh-clone test readiness — missing deps / local fixtures

`python3 run-offline-tests.py` is offline-only (fakes, temp files, pipes, temp
sockets; no emulator/ADB/X11/motion/Steam/session). On a fresh clone:

- Required: `python3` (stdlib only; the minimum version the suites are
  known-good on is not established — 3.14 is this host's version;
  `tkinter`/Tcl 9 needed ONLY for the progression-choice panel
  (`progression_choice.py`, imported lazily), not for tests or the lane), Go ≥ 1.21 per `controller/go.mod` for `go test ./...`,
  room for ~10 MB of tracked artwork + sources.
- NOT required: Android SDK, emulator, system images, AVD guest, APKs, Steam,
  ADB devices, `/dev/uhid`, joystick/motion hardware.
- Rebuild-before-test (excluded binaries): `go build` produces the controller
  binary; the helper JAR needs the Android SDK `d8` recipe above (no in-repo
  script). `test-runtime-paths.py` uses temp dirs (it references legacy
  `analysis/...` paths only as expected default strings, not as live inputs).
- Local-only fixtures staying on the owner's machine (needed for live and
  packaging work, NOT for offline tests): authorized game APK split set
  (off-Deck backup `~/.local/share/jcs2-archive-20260920/deck-backup-20260920/`, `~/Downloads/com.trueaxis.jetcarstunts2.apk`), preserved
  `hardened_api28` guest + Linux SDK/system-image trees (under `analysis/`),
  `runtime/ui-python`, portable `runtime/sdk` + `state/avd` layout (prepared
  later per `packaging/README.md`), Steam grid backups, private `.ab` payloads.
- The fresh-guest bootstrap **now exists**: `packaging/bootstrap_linux_guest.py`
  plus the setup entry point `packaging/installer/{setup-jcs2.sh,install_jcs2.py}`
  and `packaging/installer/runtime-lock.json`. Two suites cover them offline
  (`packaging/test_bootstrap_linux_guest.py`, `packaging/installer/tests/`).
  What is still open is not the code but the target: one end-to-end
  `--execute` on a real SteamOS host with the real payload
  (`docs/CONTINUATION.md` §9.7/§9.8). Gaming Mode
  window/touch/tilt/audio remain user-run live checks.

## Reviewing the index

1. `git status --short` — expect `A` entries only from the INCLUDED table;
   superseded root reports and local-only trees stay untracked or ignored.
2. `git diff --cached --stat` — counts and path categories in the stage report.
3. `git ls-files -s | grep '^120000'` — expect no output (no symlinks tracked).
