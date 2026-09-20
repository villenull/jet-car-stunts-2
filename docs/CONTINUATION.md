# JCS2 continuation handoff — 2026-09-16 (current, overrides Sep-12 root docs)

**Status date:** 2026-09-16 for §1–§8; **paused-state, wipe-safety and
clean-machine recovery status is 2026-09-20 in §9 — read §9 first when
resuming on a fresh machine or after a wipe.** Overrides stale `HANDOFF.md` / `PROJECT-STATUS.md`
(2026-09-12). Those files and other root `*.md` are preserved historical
record, not current orders. No readiness / fully-fixed claim is made anywhere
in this file. No license choice is made.

Sources read to write this (curated latest only, local working notes — this
file is self-contained so a fresh clone does not need them):
`controls-tiltdrive-user-test-card`, `tiltdrive-integration-review`,
`controls-matrix-complete`, `controls-tiltdrive-implemented`,
`editor-entry-diagnosis`, `editor-save-gate`, `quit-gaming-followup`,
`quit-sept14`, plus on-disk `run-offline-tests.py`, `linux-launcher/`,
`controller/go.mod` + `mapping.json`, `packaging/`, `steam/`,
`game-fixes/*` provenance, `artwork/steam-proposal/` delivery records.

## 1. Operating contract (all workers, no exceptions)

- One live slot at a time, granted by root. Ports 5038/5594/5595 + endpoint
  and AVD lock files serialize cooperating launches; release on teardown.
- User drives ALL live action (launches, session switches, driving, taps,
  quit). Agents: no Steam launch/stop/restart, no emulator boot, no ADB
  input, no session switch, no broad kills (only captured owned pgids), no
  user-process termination, no Paseo/session restart. Never kill to "free"
  the slot.
- Offline green ≠ live proof. Every claim cites evidence paths.
- Personal guest: never uninstall, clear, wipe, or rotate keys. Upgrades are
  same-key `-r` reinstall only, with pre/post cert + versionCode gates.

## 2. Architecture / source layout (from actual files)

```text
run-jcs2 -> linux-launcher/jcs2-launcher.sh -> runner.py (owns all children)
linux-launcher/
  runner.py  joystick_bridge.py  bridge_side_channel.py  deck_pad.py
  qt_settings.py (emulator-wide Qt settings)  hud_layout.py (in-race HUD repair)
  tilt_control/adapter.py estimator.py motion_reader.py hidraw_reader.py
    sensor_transport.py settings.py stick_gate.py (sole LX/LY suppression authority)
  navigation_v2/ overlay/ gamescope_window.py audio_config.py
  runtime_paths.py (portable vs legacy path resolver)
  test-*.py + tilt_control/test_tilt.py (offline fakes only)
controller/ (Go 1.21 module jcs2/controller; mapping.json; helper-src/*.java)
game-fixes/audio|controls|editor-save|maps|personal-deploy (composers + verifiers)
packaging/plan.py + current-plan.json + elf-audit.json (read-only planner)
steam/jcs2-steam-launch.sh + steam_shortcut.py + binary_vdf.py
artwork/steam-proposal/ (grid masters + delivery records)
run-offline-tests.py (17 suites, fakes/tempfiles/sockets only)
```

Runtime flow: Steam wrapper → runner (ports 5038/5594/5595, 1536 MiB,
GPU mode per session — `-gpu host` with a desktop compositor, else
`swiftshader_indirect` — `-net none` offline) → emulator + joystick bridge +
controller
NDJSON stdin + optional tilt native push (`_push_tilt_to_guest` as
accelerometer vectors only; gated sticks never double-drive).

## 3. Dependencies / offline build+test (truthful, fresh-clone limits apply)

- Python 3.14 is this host's interpreter (`python3 --version`); the minimum
  Python version the suites are known-good on is not established. No system
  Python packages are installed by tests (`PYTHONDONTWRITEBYTECODE=1`, stdlib +
  repo code; Tk/Tcl needed only for the progression-choice panel
  (`progression_choice.py`, lazily imported) — the `--control-settings`
  driving-mode panel was removed 2026-09-18).
- `controller/go.mod` declares Go 1.21 as the module minimum; no Go toolchain
  is installed on this host (`go: command not found`), so the Go controller
  is used here as source plus the prebuilt
  `linux-launcher/jcs2-controller-linux` — a fresh Go rebuild is not claimed.
  The input-helper JAR (`controller/helper-src/Jcs2InputHelper.java`) ships
  `classes.dex` inside: rebuilding it from source needs Android SDK
  build-tools (javac against `android.jar`, then `d8` dexing) and no build
  script for that recipe exists in this repo yet.
- `llvm-objdump/llvm-readelf --mcpu=cortex-a9` is the native-byte decode bar
  (used for all patch proofs); a custom decoder alone is never accepted.
- Commands (read-only unless noted; need provisioned local paths per §4):
  `python3 run-offline-tests.py` — full offline matrix;
  `python3 run-offline-tests.py -v <suite>` — one suite;
  `python3 linux-launcher/runtime_paths.py --check` — filesystem preflight
  (never boots anything); `./run-jcs2 --headless --input qa` — local QA only
  with explicit flags (not a fresh-clone default).
- Re-verified 2026-09-20 on this workstation: `OK: 546 unittest cases across
  17 suites` (live-controls 30, window-policy 17, runner 86, lifecycle 55,
  tilt 111 +1 skipped, audio-config 10, runtime-paths 7, personal-controls 1,
  bridge-side-channel 20, hud-layout 20, ebadf 16, steam-wrapper 11,
  packaging 32, progression-choice 77, steam-shortcut 28, installer 25;
  bridge-map script checks). Supersedes the 2026-09-16 `449/16` figure and the
  intermediate 462/504/539 numbers quoted in README/SETUP/older notes —
  `controls-settings` is gone (panel removed) and `hud-layout` + `installer`
  are new. Same run repeated on a fresh `git clone` of the pushed commit: §9.7.

## 4. Local-only artifacts (NOT distributed — provisioning prerequisites)

Git tracks source/docs/tooling only. A fresh clone does **not** contain and
must not invent: `backups/` (USB/original APKs, private saves, Steam VDF
backups), `staging/` (scratch AVDs, composed APK sets, run evidence),
`analysis/` (raw logs/screenshots), `runtime/sdk` + system images, AVD
disk images, pulled splits/saves, or the signing private key.
Persistent AVD `.ini`/`config.ini`/`hardware-qemu.ini` carry absolute paths
into this checkout and need staged rewrites on any relocation (see
`packaging/plan.py` report; planner prints only, never copies/repairs).
`runtime_paths.py --check` fails closed until provisioned — that is expected,
 not a bug.
**Retrieval — corrected 2026-09-20 (superseded the pre-wipe text):** the USB
holder this section used to name (`backups/usb-20260910T005449Z/`) and the
DB86 key material were **destroyed in the 2026-09-17 Deck wipe** (see
`docs/RESTORE-2026-09-17.md` §4 and `docs/REINSTALL-2026-09-17.md` §3/§7).
The live install is signed with the replacement Deck-side key `jcs2fresh`
(`~/.jcs2-signing/jcs2-fresh.p12`, cert `47:B6:…:C6:67`), not DB86, so the
"verify the installed splits are DB86" gate in `docs/SETUP.md` §3 and
`game-fixes/personal-deploy/PERSONAL-DEPLOY.md` no longer describes this
guest. Current payload/key holders and the exact restore mapping are in **§9**;
never fabricate, substitute, or rotate either key.
Platform SDK pieces and language toolchains (Android SDK / command-line
tools, emulator, system images, Go, Python, LLVM) may be fetched from their
official upstream sources — the owner game payload, the guest saves and the
signing key are the only private-provision items, and the pristine game APK
also exists outside the Deck (sha256 in §9.4).

## 5. Current truth by subsystem

### 5.1 TiltDrive host gate (implemented, offline-reviewed; UNSELECTABLE since 2026-09-18)
Engine + persisted mode + LX/LY-only gate are in-repo
(`stick_gate.py`, `runner.py` control sections,
`tilt_control/settings.py` + `adapter.py`; quit sections untouched). The
selector panel `controls_settings.py` and its `--control-settings` pre-launch
screen were REMOVED 2026-09-18 and `TILT_DRIVE_SELECTABLE = False` in
`runner.py` closes the choice at the input ingress (a lane that persisted Tilt
migrates once to Gamepad with `stage=tilt-drive-disabled`), because the game's
sensor-landscape window lets the accelerometer the tilt mirror feeds re-derive
the display quarter and the picture flips on every lift.
Gate drops only axis LX/LY when persisted Tilt is armed; LT/RT/RX/RY,
LB/RB/START/BACK/X/Y always pass; missing/corrupt settings default Gamepad;
Tilt+dead sensor persists Tilt with loud error (fail-closed); explicit Gamepad
restores cached sticks. Reversible anytime (panel/router/CLI; deleting the
settings file returns Gamepad). In-game Gamepad toggle stays **ON** by design.
Physical gate (user test card): calibrate neutral in-game → directions →
pedals/buttons → sticks-dead → Gamepad-while-tilted blend → normal quit.
Limits: in-race steering UNVERIFIED (V+/V− diverge but every gas cell ejects
regardless; missing no-tilt locked-spawn cell); brake is one trial (LT held
parked vs 54/22 gas-alone; trigger-conflict alternative open; LT-alone
untested). Console ON moves / OFF freezes is proven; OFF is never used.

### 5.2 Quit lifecycle (old fix executed; new cohide detail; GamingMode pending)
Sept-14 root cause (presentation drain: ~17 s visible Android home after
`stage=game-quit`, ~22 s total) is fixed in code: tri-state
`game_task_state`, cached-process fallback (`game_process=cached-task-finished`),
`hide_owned_emulator_window` (recorded pid+xid re-validated, owned transients
also hidden, foreign pids never touched, never raises), `begin_quit_teardown`
(hide first, then owned-pgid SIGTERM overlapping the bridge wait; generic
`stop_owned` grace unchanged). Headless QA PASS + two real user runs show the
detection shape; hide mechanics PASS on real X11 desktop probe
(owned-unmapped, foreign-untouched). Pending: real GamingMode user check
(hide → Steam UI with no home flash, emulator-exit ≤ ~8 s, ports free,
`stage=game-hide via=recorded-identity` or exact skip reason).

### 5.3 Sound (v3 stable; cycling rejected)
Native stable mode is **v3 fixed LOW** (sites `0x154a7c`, `0x11e318`,
`0x154a1c`; taps safe no-ops; LOW always). Cycling candidates
(`candidate-cycling*`, incl. tap-only/v2) carry `REJECTED.json` — crash risk
(interworking/ASLR/synchrony/boot-context findings in the independent
reviews). Do not rebuild cycling without new root approval.

### 5.4 Unlock baseline vs editor candidate
Personal guest holds the canonical unlock baseline (six-sentinel native patch,
final lib `cb2bd45b…ffb1`, `versionCode 29`, single-APK monolith signed with
the Deck key `jcs2fresh` → cert `47:B6:…:C6:67`; **not DB86** — that key died
in the 2026-09-17 wipe) — intact, never wiped. Editor variant
lib `cb2bd45b85cd6bf161199e57c460870d295b73d4ad2101bb7d97d0d413f0ffb1`
 (45B = 41 controls-line + 4 editor bytes, DB86-signed 5 splits) was composed
 offline into the stage
 `staging/editor-save-20260915T010000Z` (local-only, never in git), used only
 in isolated scratch guests that have since been destroyed. It was **never
 installed personally**. Stage re-composability: the composer scripts are
 tracked (`game-fixes/editor-save/`), but the stage itself and the DB86 signing
 key it needed are both gone — recomposing now yields a `jcs2fresh`-signed
 build, which is a different install identity (§9.1).

### 5.5 Editor SAVE gate (statically proven, functionally unverified)
Two-NOP patch proven by hash + llvm at both call sites (SAVE `0x159a9a`
`48b3→00bf`, Select `0x15a9a8` `b8b3→00bf`; neighbours BLX intact; predicate +
store + Create/main notes stock; disjoint from audio/maps/sentinels/controls
sites). Observed: LOGIN uncheck→PLAY needs no login; Note ×2 dismiss; car-select
reachable; server-listed row tap → detail with NO store prompt → PLAY opens and
drives, with `userLevels/` EMPTY by root pull. NOT proven: any newly-saved
custom track file, list population from it, select-reopen, H1==H2 across
restart. **Corrections to stale claims:** (a) the Select fall-through on
server/bundled content must not be called custom-track reopen; (b) false
server/Scepie provenance "built" claims are withdrawn; (c) CREATE is the
correct route with no entitlement gate on entry and no patched regression —
EDIT is hidden because play-test bit1 at editor `+0x28c` is clear until
`SetPlayModeOn` fires, which needs built nodes (`[editor+0x1d4] ≥ 2`) plus HUD
play-test confirm; (d) the "none identified" gap note is refuted by the
button-exact route: LayTrack (`g_pButtonLayTrack`) → Flatten (`0x100`) →
Checkpoint (`0x200` via `CanPlaceCheckPoint`) → Play confirm
(`Hud::AddMessage` + `SetPlayModeOn` + restart) → pause EXTENDED shows EDIT
(`OnEditClicked` → `SetEditModeOn`) → build → `QuitLevelEditorEv` →
`PlaythroughComplete` → SAVE `tasktest` → root pull H1 → list → select →
open → restart → H2 + reopen. Sole missing evidence: HELP AND OPTIONS content
(strings encrypted at rest) + one live HUD-tap sweep mapping visible slots to
LayTrack/Flatten/Checkpoint on 1280×800. Last editor slot (f028 line) released
clean: guests destroyed, locks/ports free, personal/Steam untouched.

### 5.6 Steam artwork (applied Steam-closed; UI pickup pending)
Non-Steam shortcut renamed `jcs2-steam-launch.sh` → `Jet Car Stunts 2`,
appid `4192595540` preserved (VDF round-trip verified). Grid: portrait
`…p.png` kept; hero background kept; wide v2 master (1832×858, car ~12–15%
smaller, down/right, title top-left) applied over the wide slot; cropped
genuine RGBA logo (`logo-tight.png`, 1187×413 RGBA, tight ~3:1 canvas)
applied over the logo slot; baked-checkerboard logo renders rejected and
never applied. Backups kept (`backups/steam-shortcut-*`,
`backups/steam-grid-*`). Steam was closed for all writes and never
launched/stopped/restarted by agents. Rendered Desktop/GamingMode pickup
needs one normal user Steam launch; if art does not appear, exit Steam fully
and re-verify hashes before re-applying.

## 6. Tested vs unverified (do not relabel)

- Tested offline: full 546 matrix (§3); gate/persist/reconnect/failure semantics;
  quit watcher streak/reset matrix + hide-never-blocks; packaging/plan +
  resolver checks; installer stage/dry-run + checksum-verification suites;
  Steam VDF/PNG/hash on-disk checks; static llvm + hash
  guards for every native byte.
- Live-proven on scratch/runs: console→HAL→sensor→calibration indicator
  (ON moves/OFF frozen); spawn-lock method; brake-parked datapoint;
  server-row Select fall-through; LOGIN-uncheck→menu; headless quit path;
  X11 desktop hide mechanics.
- Unverified and must stay marked so: in-race tilt authority; brake feel
  (single trial); physical directions/blend/stale; GamingMode quit
  presentation; whole custom save→list→reopen→restart chain; rendered Steam
  art; personal redeploy audible + PLAY smoke.

## 7. Rejected approaches (do not repeat without new evidence + approval)

Predicate-level return-1; global `Store_IsItemPurchased` forcing; seed rev1/2
reuse (SIGILL history); audio-cycling rebuilds; `-qt-hide-window`; patching
Create/main-menu notes (cosmetic); touching shop/progression/ads/medals;
blind PLAY/input sweeps; synthetic tracks/files passed off as saves;
inventing HUD tap coordinates; absolute-path invention for fresh clones;
re-copying wide when only the logo needs work.

## 8. Next concrete tasks (in order, serialized)

1. ~~**User TiltDrive card**~~ — closed 2026-09-18: three live attempts, the
   display flipped every time; Tilt Drive is now unselectable by design (§5.1).
2. **User GamingMode quit check** (§5.2 asserts + latencies + ports free).
3. **Isolated editor retry** (fresh scratch guest, `cb2bd45b`, landscape
   1280×800, full-touch preflight 5/5, HELP AND OPTIONS screenshot first,
   then one HUD-tap sweep with node/flag reads; STOP if EDIT absent and
   record pause variant + flag byte instead of sweeping).
4. **Same-key upgrades as needed** (no duplicate personal redeploy: stable
   LOW + unlock baseline already installed): `jcs2fresh` cert
   `47:B6:…:C6:67` + `versionCode 29` gates (§5.4 — this replaces the superseded
   "DB86 + v29" wording; DB86 material died in the 2026-09-17 wipe), host
   AVD backup, `install-multiple -r` on the approved splits, 5/5 hash + cert +
   sentinel pins, PLAY + audible user gates, scoped rollback on mismatch.
   Covered by the existing same-key upgrade authorization.

Evidence to keep for each: run dir + hashes + screenshots + log excerpts +
slot release note. Personal/Steam/Paseo/session intact throughout.

## 9. Paused state, wipe safety and clean-machine recovery (2026-09-20)

**Status:** the project is PAUSED by the user on 2026-09-20. No Deck mutation
and no erase happened. This section is the authority for "is it safe to wipe"
and "how do I come back"; §1–§8 describe behavior and history and are not
overridden except where they are quoted as superseded.

### 9.1 Source of record

- `https://github.com/villenull/jet-car-stunts-2` — verified **private**
  (`gh repo view … --json visibility,isPrivate` → `PRIVATE`, `isPrivate: true`),
  single branch `main`, no `v2` branch (older notes that say "branch v2" are
  stale).
- Before this session `main` was `410625e` (2026-09-17T20:20Z). The whole
  2026-09-18/19/20 session — Gaming Mode work, the installer, the tilt-panel
  removal, the HUD/audio/deck-pad modules — existed only in the workstation
  working tree. It is now committed and pushed as
  **`9a1b3a0f7546e4c916d459042d1e2a5d1d07beb9`** (fast-forward from `410625e`,
  no divergence; `git ls-remote origin refs/heads/main` agrees). §9.2 lists what
  that commit carries and §9.7 records the fresh-clone proof of it. That SHA is
  the *source-content* commit; docs-only commits may sit on top of it, so use
  `git log --oneline` when you need the branch tip.
- Install identity matters for any redeploy: the live guest is signed with the
  Deck-side key `jcs2fresh` (`~/.jcs2-signing/jcs2-fresh.p12`, cert
  `47:B6:…:C6:67`), **not** the DB86 key that died in the 2026-09-17 wipe. Any
  doc that gates on "splits uniform DB86" describes hardware that no longer
  exists (fixed in §4, §5.4 and §8 above; `docs/SETUP.md` §3 and
  `game-fixes/personal-deploy/PERSONAL-DEPLOY.md` still carry the old wording —
  read them with that substitution).

### 9.2 What this commit carries (source recoverable from GitHub alone)

Linux lane: `runner.py`, `joystick_bridge.py`, `gamescope_window.py`,
`audio_config.py`, new `deck_pad.py` (Deck built-in pad hand-off from the
Omarchy `deck-input-mapper` EVIOCGRAB), new `qt_settings.py`, new
`hud_layout.py` (live in-race HUD repair over `/proc/<pid>/mem`),
`tilt_control/sensor_transport.py`; `controls_settings.py` deleted (Tilt Drive
unselectable). Tests: the `test-*.py` suite updates plus new
`test-hud-layout.py`, `test-bridge-side-channel.py`, and
`packaging/installer/tests/test_install_jcs2.py`. Installer/packaging:
`packaging/installer/{install_jcs2.py,setup-jcs2.sh,runtime-lock.json}`,
`prepare_distribution.py`, `distribution.json`, `plan.py`,
`bootstrap_linux_guest.py`, `current-plan.json`. Windows-launcher source
(`launcher/logic.hpp`, `prepare-personal-runtime.ps1`: `hw.accelerometer = yes`),
`controller/mapping.json` (BRAKE/HANDBRAKE axes), Steam wrapper + README + test,
and the docs in this commit including `docs/CONTINUATION-2026-09-18.md` (the
overnight report that was previously untracked — the single largest
GitHub-only loss if it had not been committed).

### 9.3 What is NOT in git (and can never come from GitHub)

`.gitignore` is a deny-list and the index is built by explicit allowlist, so a
fresh clone legitimately does **not** contain: game APKs (`*.apk`), the Go
controller binary `linux-launcher/jcs2-controller-linux` and
`linux-launcher/jcs2-input-helper.jar` (`*.jar`), key material
(`*.p12`/`*.keystore`/`*.jks`), AVD images (`*.qcow2`), `runtime/`, `state/`,
`staging/`, `analysis/`, `assets/`, logs.

### 9.4 Durable off-Deck backup (owner: the Deck operator, not this repo)

- Location: `/home/villenull/jcs2-deck-backup-20260920/` on this workstation —
  outside the repo, not `/tmp`. Written and owned by the Deck operator, whose
  written mapping is `/home/villenull/jcs2-deck-backup-20260920/RESTORE-MAPPING.md`.
- **Status 2026-09-20: complete and checksum-verified.** A first pass was
  interrupted when the Deck left the network (part of `avd/` only); a resume
  pass finished it, and a second pass then added the host/build items found
  during cross-checking. Final `MANIFEST.sha256`: **7 047 entries /
  12 660 115 501 bytes**; this workstation re-ran `sha256sum -c MANIFEST.sha256`
  from the backup root independently after each pass → **7 047 OK, 0
  mismatches**. The owner separately re-ran `rsync --dry-run --checksum`
  against each live Deck path (checksums read on both sides) with every tree
  reporting IDENTICAL, and hash-verified the secret files equal on both sides
  with modes intact (`secrets/jcs2-signing` 0700, `jcs2-fresh.p12` and
  `.storepass` 0600). Per-subtree: `avd` 26 files / 3.35 GB, `secrets` 5 / 7 kB,
  `payloads` 10 / 654 MB, `sdk` 708 / 1.55 GB, `archives` 2 / 601 MB,
  `runtime` 4 032 / 5.15 GB, `logs` 2 207 / 905 MB, `configs` 8, `helper` 1,
  `source` 5, `steam-config` 6, `local-mirrors` 3, `bin` 1 / 3.5 MB,
  `host-config` 22 / 386 kB. Copy + manifest scripts and per-phase logs are
  under `scripts/`; `host-config/NOTES.md` records the one item that could not
  be captured.
- **Deck build + host items are captured** (the first pass missed them; a
  cross-check of `RESTORE-MAPPING.md` against the manifest found the gap and
  the operator closed it): the built Go controller is in `bin/`
  (`jcs2-controller-linux`, 3 502 343 B, `sha256 b5cebbf1…4f990`, identical on
  both sides — so **no Go toolchain is needed to restore it**); the custom host
  scripts are in `host-config/` (`/usr/local/sbin/deck-lizard-mode`
  `sha256 d729d2fe…41f21`, `/usr/local/bin/deck-input-mapper`
  `sha256 d0b0cde9…a8b86`, which `deck_pad.py` drives), together with the
  10 `jcs2-ops/` helper scripts and the Deck image-provisioning files the lane
  touches (`deck-session-select`, `deck-steam-desktop`, their systemd units and
  `/etc/udev/rules.d/zz-deck-power-button.rules`). `/dev/input/js*` and
  `/dev/dri/renderD128` access is **stock**: device ACLs plus mode 0666, with no
  custom `input`/`video`/`render` group additions to reproduce.
- **Two known, accepted gaps — record them, do not rediscover them.**
  (1) The passwordless sudoers rule is not a byte capture: `/etc/sudoers.d` is
  `0750 root:root` and unreadable to the operator session. It is a
  **documented, functionally equivalent reconstruction** — the only grant
  `deck_pad.py` needs is passwordless right to run the captured tool with `on`
  or `off`. Restore it as a `0440 root:root` file, e.g.:

  ```sudoers
  # /etc/sudoers.d/jcs2-deck-lizard-mode
  deck ALL=(root) NOPASSWD: /usr/local/sbin/deck-lizard-mode on, \
                            /usr/local/sbin/deck-lizard-mode off
  ```

  Create it only through `visudo -f /etc/sudoers.d/jcs2-deck-lizard-mode` (or
  `install -m 0440 -o root -g root` then `visudo -c`); a syntax error in a
  `sudoers.d` file can lock out sudo entirely. Restore
  `host-config/usr-local-sbin/deck-lizard-mode` to
  `/usr/local/sbin/deck-lizard-mode` mode `0755` first, and verify with
  `sudo -n /usr/local/sbin/deck-lizard-mode` returning without a password
  prompt. `host-config/NOTES.md` carries the same recipe.
  (2) `/home/deck/jcs2-ops/archive/` (≈37 GB, 53 505 captured PNG frames plus
  large NDJSON) is an **excluded historical capture archive**, deliberately not
  copied: it is diagnostic frame evidence, not a restore prerequisite and not
  save/progress data. Nothing needs it to rebuild or play. It can still be
  copied before any wipe on request.
- A note on `deck-runtime-jcs2-live`: there is **no such systemd user unit on
  the Deck**. The name in `docs/CONTINUATION-2026-09-18.md` is an omp hub
  process spec (now exited), i.e. workstation/session state, not a Deck file —
  nothing to restore. The captured `gamescope-session.service.d/override.conf`
  (`sha256 45ad7eb6…95e9`) is the real Deck-side unit fragment.
- The pristine game APK does **not** depend on the Deck: the same file exists
  at `/home/villenull/Downloads/com.trueaxis.jetcarstunts2.apk`
  (`sha256 779f2103c761012733364320dac5ca1ce0cc03be044ca1243cea7f885d7c30fe`,
  163 497 572 bytes) and both copies hash identically; the backup keeps its own
  copy under `local-mirrors/` with `SHA256SUMS.txt`.
- The pinned emulator archive also does not depend on the Deck:
  `/tmp/jcs2-emulator-ab/emulator-linux_x64-10696886.zip`
  (`sha256 284954ab33259e0125757623132e7e36982d968fb6c22495145bb1b37861e6ef`,
  270 191 252 bytes) matches `runtime-lock.json` and is now durable in two
  places: the backup's `local-mirrors/` and this session's evidence dir (§9.7).
- Deliberately **not** copied, and not required to restore: `/home/deck/go` and
  `/home/deck/jdk-21` (redownloadable toolchains), Steam shader caches /
  `compatdata` and non-JCS2 Steam data (regenerated).
- No secret *value* is recorded in this repo or in any handoff message; only
  paths, modes and public artifact hashes. The backup's secret hashes are in
  `MANIFEST.sha256` inside the backup, deliberately not copied into the repo.

### 9.5 Clean-machine prerequisite inventory (audited from installer + launcher code)

| # | Prereq | Exact requirement | From GitHub alone? |
|---|---|---|---|
| 1 | Host Python | `python3 ≥ 3.9` (installer preflight) | Yes (host) |
| 2 | Bundled Python | cpython 3.12.14+20260901 install_only tar, size 111 368 545, sha256 `936c246d…affa22` → `runtime/python` | Yes — public astral-sh release, hash-pinned in `packaging/installer/runtime-lock.json` |
| 3 | platform-tools | `platform-tools_r37.0.1-linux.zip`, size 9 054 187, sha256 `d230f138…9313f1` → `runtime/sdk/platform-tools` (needs executable `adb`) | Yes — public dl.google.com, hash-pinned, license acceptance |
| 4 | Emulator (primary) | **32.1.15 build 10696886**, `emulator-linux_x64-10696886.zip`, size 270 191 252, sha256 `284954ab…e6ef` → `runtime/sdk/emulator` (needs `emulator`, `qemu-img`) | Yes — public dl.google.com, hash-pinned |
| 5 | Emulator (alternate) | 37.1.11 build 15917651, `role=alternate`, never fetched unless `--include-alternates` | Optional |
| 6 | System image | `system-images;android-28;google_apis;x86` r12 (`x86-28_r12.zip`), size 994 434 707, sha256 `5c5473ca…dad3b` → `runtime/sdk/system-images/android-28/google_apis/x86` | Yes — public dl.google.com, hash-pinned, ARM-DBT license acceptance |
| 7 | Game payload | one signed monolith `*.apk` **or** the five splits, **plus** `jcs2-controller-linux` and `jcs2-input-helper.jar` and `controller/mapping.json`, via `--payload-dir`/`--payload-archive` | **No** — mirrors in §9.4 |
| 8 | Signing key | needed for any patched/variant rebuild | **No** — Deck `~/.jcs2-signing/`, mirrored in §9.4 |
| 9 | Guest saves | `userdata-qemu.img.qcow2` under `state/avd/<profile>.avd` | **No** — Deck `state/avd/`, mirrored in §9.4 |
| 10 | Layout + markers | `jcs2-layout.json {"schema":1,"layout":"portable","avd":NAME}`, `install-state.json {"status":"complete"}`, `state/runtime-manifest.json/.md5` | Generated by the installer's layout/finalize stages |
| 11 | Steam account | `--steam-account` for shortcut registration | No — user value |

Portable layout the wrapper/runner resolve (`runtime_paths.py`):
`JCS2_SDK=<root>/runtime/sdk`, `JCS2_AVD_HOME=<root>/state/avd`,
`JCS2_DIST=<root>/assets`, `JCS2_LOGDIR=<root>/state/logs`, chosen by
`jcs2-layout.json`. The dev-checkout default in `steam/jcs2-steam-launch.sh`
(`state/emulator-ab-10696886`) applies **only** when no layout marker exists, so
an installer-produced root is self-consistent; a hand-provisioned dev checkout
needs that SDK tree instead.

**Source-only rebuild vs complete deployment.** A fresh clone plus host
`python3` gives the offline suite and an installer *dry run* (`setup-jcs2.sh`
with no flags exits 1 listing exactly what is missing). A *playable* install
additionally needs rows 7–9 plus a successful `--execute` and Steam
registration. Runtime rows 2–6 are fetchable from the pinned public URLs by the
installer itself (`--allow-network`), so they are not a backup requirement.

### 9.6 Restore recipe (no secrets in this file)

1. Clone the private repo at the verified commit; run
   `python3 run-offline-tests.py` (expect `OK: 546 … 17 suites`).
2. Restore the off-Deck backup sub-trees to the paths in
   `RESTORE-MAPPING.md`, preserving modes (`secrets/` must be restored 0600 in
   a 0700 dir; never into the repo working tree). That mapping also covers the
   Deck build and host items: `bin/jcs2-controller-linux` →
   `linux-launcher/jcs2-controller-linux` (0755), `helper/jcs2-input-helper.jar`
   → `linux-launcher/`, and `host-config/` → `/usr/local/sbin/`,
   `/usr/local/bin/`, `/etc/systemd/system/`, `/etc/udev/rules.d/` and the
   gamescope session fragment. Then recreate the one non-captured file, the
   sudoers grant, with `visudo` exactly as in §9.4 and confirm it with
   `sudo -n /usr/local/sbin/deck-lizard-mode` (no password prompt).
3. Let the installer provision the runtime: `--execute --accept-licenses`
   `--allow-network` (or `--archives-dir` with the pinned archives staged).
4. Supply the payload: `--payload-dir` with the APK set, the controller binary
   and the helper JAR; the archive itself is text-only by design.
5. Restore the guest AVD/profile if saved progress matters, then verify with
   `linux-launcher/runtime_paths.py --check`, one `--execute` run and a
   user-driven PLAY — offline green is not live proof.
6. Rebuild the release asset with
   `python3 packaging/prepare_distribution.py --stage DIR` and record its
   SHA-256; the setup archive is the only distributable artifact and it never
   contains binaries.

### 9.7 Rehearsal evidence (2026-09-20, isolated, no Deck contact)

Evidence dir `/home/villenull/jcs2-recovery-20260920/` (durable, outside the
repo): `logs/installer-rehearsal.log`, `rehearse.sh`, `archives/`. Nothing under
`/home/deck` was contacted and no live guest was reset; the whole run happened
inside the evidence dir.

Measured, in order:

1. **Release asset builds, text-only.** `prepare_distribution.py --stage`
   produced `jet-car-stunts-2-setup.tar.gz`, 151 112 bytes, 34 members,
   `binary_entries: 0`, sha256 `6fae4e23…9d99c0`. (Built from the pre-commit
   dirty tree, so its `payload_commit` is `410625e` with `tree_dirty: true`;
   the released artifact must be rebuilt from the clean commit — the recipe is
   in §9.6 step 6.)
2. **Dry run touches nothing.** `setup-jcs2.sh` extracted on a bare dir exits 1
   listing every missing piece, and the tree digest over the extracted root was
   byte-identical before and after (`ea75f6ed…`).
3. **`--execute` really provisions the runtime from the pinned public URLs.**
   With `--archives-dir` holding only the emulator zip, the installer
   downloaded `python` (astral-sh GitHub release), `platform-tools` and
   `system-image` (dl.google.com), and extracted all four. 1 385 048 691 bytes
   planned, 25 s wall, and every fetched archive re-verified offline against
   `runtime-lock.json` — size **and** SHA-256 match on all four
   (`python` 111 368 545, `platform-tools` 9 054 187, `emulator` 270 191 252,
   `system-image` 994 434 707).
4. **Resulting trees.** `runtime/python/bin/python3`, `.../platform-tools/adb`,
   `.../emulator/emulator`, `.../emulator/qemu-img` and the 2.6 GB system image
   are present, and the extracted emulator reports
   `Android emulator version 32.1.15.0 (build_id 10696886)` — the pinned build.
5. **It then claimed a fresh guest and wrote the layout marker**
   (`state/avd/jcs2-fresh.avd`, `jcs2-layout.json`
   `{avd: jcs2-fresh, layout: portable, schema: 1}`, driving mode `gamepad`).
6. **It stops at the payload, exactly as documented.** Exit **6** with four
   blocking items — game APKs, `jcs2-controller-linux`, `jcs2-input-helper.jar`
   (and non-blocking artwork). No `install-state.json` /
   `state/runtime-manifest.json` was written, so nothing can mistake this for a
   completed install.

What this proves and what it does not: the runtime layer is reproducible on a
clean machine from public pinned URLs, the installer's checksum gate is real
(all four hashes confirmed independently), and the payload/key/saves are the
only true private prerequisites. It does **not** prove a bootable guest, a
Gaming Mode lane, touch, audio or the Steam hand-off — those need the real
payload plus a Deck target, which is why §9.8 and §9.9 stay as they are.

**Post-push verification of the exact commit (same day, log
`logs/fresh-clone-verify.log`).** `git clone` of the private remote at
`9a1b3a0f7546e4c916d459042d1e2a5d1d07beb9` (the SHA `ls-remote` reports for
`main`, fast-forward from `410625e`, no divergence) gives a tree that contains
every session file listed in §9.2 — `deck_pad.py`, `hud_layout.py`,
`qt_settings.py`, `test-hud-layout.py`, `install_jcs2.py`, `setup-jcs2.sh`,
`runtime-lock.json`, `tests/test_install_jcs2.py`,
`docs/CONTINUATION-2026-09-18.md` — with `controls_settings.py` correctly
absent, the emulator pin resolving to 32.1.15 build 10696886 with 37.1.11 as
`role=alternate`, and no binary or secret-shaped content tracked (the single
grep hit for secret patterns is this repo's own manifest listing the patterns
it sweeps for). `python3 run-offline-tests.py` on that clone:
**`OK: 546 unittest cases across 17 suites`** in 2.3 s. Rebuilding the release
asset from the **clean** commit gives
`jet-car-stunts-2-setup.tar.gz` 151 114 bytes / 34 members,
`payload_commit: 9a1b3a0…`, `tree_dirty: false`,
`sha256 76a9f8fb53268276deca1476f2c52d4a01febc4285b039e0d1614264c8fb2dc9`
(durable copy in the evidence dir under `release-stage/`). That hash, not the
earlier dirty-tree one, is the reference for this paused state.

### 9.8 Known limits — keep saying these out loud

- The `--execute` path has still never run end-to-end on a **Deck/SteamOS**
  target with the real payload: `packaging/distribution.json` says
  `installer-built-dry-run-verified-pending-deck-run`, and a rehearsal in an
  isolated directory is not a fresh-install validation.
- Gaming Mode on the Deck currently runs a **software workaround at ~29 race
  FPS**; the hardware-GPU path (`-gpu host`) still logs `ColorBuffer` errors
  and is unresolved. Do not present the lane as GPU-correct.
- The **physical Gaming Mode input bridge end-to-end is unverified**; desktop
  gameplay is not proof.
- No emulator **version probe** exists anywhere: the 32.1.15 pin is enforced
  only by archive size/SHA-256 at fetch time, and a pre-existing
  `runtime/sdk/emulator` tree is accepted on file presence alone.
- In-race tilt authority, GamingMode quit presentation, the whole custom
  save→list→reopen→restart chain, and rendered Steam art remain live-unverified
  (§6). Tilt Drive is unselectable by design, not fixed.
- HUD repair is **not durable across an app restart** (the game's options blob
  still holds the drifted layout) — `docs/CONTINUATION-2026-09-18.md`.
- Audio "audible" was measured digitally; no acoustic capture is possible on
  this Deck.

### 9.9 Wipe-safety verdict (2026-09-20)

**Wipe-safe.** Every JCS2 artifact the audit could name is off the Deck and
checksum-verified (§9.4), the source and progress are on the private GitHub
commit (§9.2), and the runtime prerequisites are either fetchable from pinned
public URLs or mirrored. Two items are not byte-captured, and the honest
phrasing for each is: the sudoers rule is **reconstructable from a documented
recipe** rather than restored from a copy, and the 37 GB capture archive is
**excluded on purpose** because it is history, not state. Neither blocks a
rebuild, and neither is unrecovered progress. This verdict is about
*recoverability*, not about how good the build is — §9.8 stands unchanged.

| Asset | Off-Deck copy | Wipe-safe? |
|---|---|---|
| All source + docs (§9.2) | private GitHub, verified commit | Yes |
| Offline suite on a fresh clone | this workstation | Yes |
| Runtime archives (python/platform-tools/emulator-32.1.15/system image) | public pinned URLs + backup `local-mirrors/` + evidence dir | Yes |
| Pristine game APK | `~/Downloads`, backup `local-mirrors/` and `payloads/`, hashes match | Yes |
| Signing key `jcs2fresh` + password | backup `secrets/` 0600, hash-equal both sides, in the manifest | Yes |
| Signed/patched monolith APK | backup `payloads/` (signed + unsigned + descriptor) | Yes |
| Helper JAR | backup `helper/` | Yes |
| Native controller binary `jcs2-controller-linux` | backup `bin/`, hash-identical both sides (no Go toolchain needed) | Yes |
| Custom host scripts + units (`deck-lizard-mode`, `deck-input-mapper`, `jcs2-ops/`, session/provisioning units, `zz-deck-power-button.rules`) | backup `host-config/` | Yes |
| Guest saves (`userdata-qemu.img.qcow2`) | backup `avd/`, both-side checksum IDENTICAL | Yes |
| Deck state/logs/configs (`runtime-manifest`, `steam-registration`, `shortcuts.vdf` + grid art, `Emulator.conf`, lane logs incl. `run-20260919T210456Z`) | backup `logs/`, `configs/`, `steam-config/`, `runtime/`, `sdk/` | Yes |
| `/etc/sudoers.d` NOPASSWD rule | not byte-captured (unreadable); documented in `host-config/NOTES.md` + §9.4, recreated via `visudo` | Yes — reconstructable, not copied |
| `/home/deck/jcs2-ops/archive/` (≈37 GB frame evidence) | deliberately **not** copied | Accepted gap — historical capture archive, not needed to restore or play |

Read this table rather than any one-line summary. Re-read the top of §9.4 for
the exact evidence behind every "Yes".
