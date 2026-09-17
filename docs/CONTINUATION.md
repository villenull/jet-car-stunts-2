# JCS2 continuation handoff — 2026-09-16 (current, overrides Sep-12 root docs)

**Status date:** 2026-09-16. Overrides stale `HANDOFF.md` / `PROJECT-STATUS.md`
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
  runner.py  joystick_bridge.py  bridge_side_channel.py
  controls_settings.py (Gamepad/TiltDrive selector, prelaunch + live)
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
run-offline-tests.py (16 suites, fakes/tempfiles/sockets only)
```

Runtime flow: Steam wrapper → runner (ports 5038/5594/5595, 1536 MiB,
host GPU, `-net none` offline) → emulator + joystick bridge + controller
NDJSON stdin + optional tilt native push (`_push_tilt_to_guest` as
accelerometer vectors only; gated sticks never double-drive).

## 3. Dependencies / offline build+test (truthful, fresh-clone limits apply)

- Python 3.14 is this host's interpreter (`python3 --version`); the minimum
  Python version the suites are known-good on is not established. No system
  Python packages are installed by tests (`PYTHONDONTWRITEBYTECODE=1`, stdlib +
  repo code; Tk/Tcl needed only for the optional `--control-settings` panel).
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
- Verified 2026-09-16: `OK: 449 unittest cases across 16 suites`
  (live-controls 32, window-policy 18, runner 54, lifecycle 44, tilt 110,
  controls-settings 19, audio-config 4, runtime-paths 7, personal-controls 1,
  bridge side-channel 16, ebadf 16, steam-wrapper 10, packaging 10,
  progression-choice 80, steam-shortcut 28; bridge-map script checks).
  Transient note: a 446/449 mid-edit collision during a concurrent tilt-lane
  edit is documented in the quit follow-up §5c; settled disk is 449/449.

## 4. Local-only artifacts (NOT distributed — provisioning prerequisites)

Git tracks source/docs/tooling only. A fresh clone does **not** contain and
must not invent: `backups/` (USB/original APKs, private saves, Steam VDF
backups), `staging/` (scratch AVDs, composed APK sets, run evidence),
`analysis/` (raw logs/screenshots), `runtime/sdk` + system images, AVD
disk images, pulled splits/saves, or the **DB86 signing private key**.
Persistent AVD `.ini`/`config.ini`/`hardware-qemu.ini` carry absolute paths
into this checkout and need staged rewrites on any relocation (see
`packaging/plan.py` report; planner prints only, never copies/repairs).
`runtime_paths.py --check` fails closed until provisioned — that is expected,
 not a bug. Retrieval: the owner APK set, pulled saves, and the DB86 signing
 private key come only from the owner's local holders — USB backup
 (`backups/usb-20260910T005449Z/`, base `41043f68…`, lib `bc7fdf9d…`) and
 local AVD/private-key holders; never fabricate, substitute, or rotate those.
 Platform SDK pieces and language toolchains (Android SDK / command-line
 tools, emulator, system images, Go, Python, LLVM) may be fetched from their
 official upstream sources — only the owner game payload, saves, and signing
 key are private-provision. Same project key
`DB:86:A5:6E:99:37:04:51:84:A3:B5:B8:E3:64:E3:0F:53:33:A3:0A:FD:A1:27:39:E1:1E:71:78:B4:AD:33:ED`
is required for any upgrade; `install-multiple -r` preserves userdata.

## 5. Current truth by subsystem

### 5.1 TiltDrive host gate (implemented, offline-reviewed, physically gated)
Selector + persisted mode + LX/LY-only gate + honest panel text are in-repo
(`stick_gate.py`, `runner.py` control sections, `controls_settings.py`,
`tilt_control/settings.py` + `adapter.py`; quit sections untouched).
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
Personal guest holds the old canonical unlock baseline (DB86, versionCode 29;
receipt-verified 5/5 sets, save intact) — intact, never wiped. Editor variant
lib `cb2bd45b85cd6bf161199e57c460870d295b73d4ad2101bb7d97d0d413f0ffb1`
 (45B = 41 controls-line + 4 editor bytes, DB86-signed 5 splits) was composed
 offline into the still-existing local stage
 `staging/editor-save-20260915T010000Z` (local-only, not in git), used only in
 isolated scratch guests that have since been destroyed. It was **never
 installed personally**.

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

- Tested offline: full 449 matrix; gate/persist/reconnect/failure semantics;
  quit watcher streak/reset matrix + hide-never-blocks; packaging/plan +
  resolver checks; Steam VDF/PNG/hash on-disk checks; static llvm + hash
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

1. **User TiltDrive card** (no install gated): Tilt Drive → in-game ON →
   calibrate → directions → pedals/buttons → sticks-dead → Gamepad blend →
   normal quit. Report six one-liners.
2. **User GamingMode quit check** (§5.2 asserts + latencies + ports free).
3. **Isolated editor retry** (fresh scratch guest, `cb2bd45b`, landscape
   1280×800, full-touch preflight 5/5, HELP AND OPTIONS screenshot first,
   then one HUD-tap sweep with node/flag reads; STOP if EDIT absent and
   record pause variant + flag byte instead of sweeping).
4. **Same-key upgrades as needed** (no duplicate personal redeploy: stable
   LOW + unlock baseline already installed): DB86 + v29 gates, host
   AVD backup, `install-multiple -r` 5 splits, 5/5 hash + cert + sentinel
   pins, PLAY + audible user gates, scoped rollback on mismatch. Covered by
   the existing same-key upgrade authorization.

Evidence to keep for each: run dir + hashes + screenshots + log excerpts +
slot release note. Personal/Steam/Paseo/session intact throughout.
