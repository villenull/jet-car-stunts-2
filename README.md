# JetCarStunts2 (JCS2) — Linux launcher + host tooling (continuation, 2026-09-16)

**Date:** 2026-09-16. This file **overrides** the stale root `HANDOFF.md` and
`PROJECT-STATUS.md` (both 2026-09-12) as the current entry point.
Those two files plus `LINUX-STATUS.md`, `WINDOWS-HANDOFF.md`,
`LIGHTWEIGHT-OPTIONS.md`, `RELEASE-CHECKLIST.md`, `PAID-LEVEL-MAP.md` are
**preserved as historical** — do not delete; do not follow their live-test
instructions as current orders.

Full handoff: [`docs/CONTINUATION.md`](docs/CONTINUATION.md).
Provisioning + fresh-clone limits: [`docs/SETUP.md`](docs/SETUP.md).
Restore (2026-09-17, Deck fully wiped — start here): [`docs/RESTORE-2026-09-17.md`](docs/RESTORE-2026-09-17.md).

**Paused state + wipe safety + clean-machine redeploy recipe (2026-09-20):
[`docs/CONTINUATION.md`](docs/CONTINUATION.md) §9 — read §9 first.** Overnight
Deck report: [`docs/CONTINUATION-2026-09-18.md`](docs/CONTINUATION-2026-09-18.md).

## What this repo is

Host-side launcher and tooling that runs the preserved offline Android game
(Jet Car Stunts 2, package `com.trueaxis.jetcarstunts2`, versionCode 29) in a
local API28 guest. No game source is in this repo; game behavior is modified
only via documented native-byte candidates composed offline and verified by
hash. Agents do **not** restart Steam/sessions, kill user processes, or
reinstall/uninstall the personal guest.

## Current truth (2026-09-16, verified from source + latest curated reports)

- **TiltDrive host gate implemented in repo, offline-reviewed — and UNSELECTABLE
  since 2026-09-18.** The driving-mode panel (`linux-launcher/controls_settings.py`,
  including its `--control-settings` pre-launch screen) was removed on
  2026-09-18: Gamepad is the only mode, the runner rejects any `control_mode`
  request at its input ingress, and `TILT_DRIVE_SELECTABLE = False` in
  `linux-launcher/runner.py` closes the selection, because the
  game's window requests `SCREEN_ORIENTATION_SENSOR_LANDSCAPE`
  (`dumpsys window tokens`: `mOrientation=6`), so the framework derives the
  display quarter from the very accelerometer the tilt mirror feeds and the
  picture flips whenever the Deck is lifted; this AVD image has no rotation-hold
  lever (`wm user-rotation` and `cmd window user-rotation` are unknown commands).
  The gate `linux-launcher/tilt_control/stick_gate.py` and the whole engine are
  kept intact, and a lane that persisted Tilt Drive migrates once to Gamepad
  with a `stage=tilt-drive-disabled` row. In-game Gamepad stays **ON**.
- **Physical tilt is still a user gate.** In-game Gamepad ON moves the
  calibration indicator; OFF freezes it. In-race tilt steering authority is
  **UNVERIFIED**; brake-vs-gas is one spawn-locked datapoint (LT held parked
  vs full gas) with the trigger-conflict alternative open.
- **Quit:** the Sept-14 detection + owned-hide/teardown fix is implemented
  (old real-run root cause already executed in code). Offline matrix re-run
  2026-09-20 is **546/546 across 17 suites** (`docs/CONTINUATION.md` §3).
  Real GamingMode presentation
  (hide → Steam UI, no home flash, ≤ ~8 s emulator exit) is **user-check
  pending**.
- **Sound:** stable mode is **v3 fixed LOW** (`game-fixes/audio/`, sites
  `0x154a7c`/`0x11e318`/`0x154a1c`). All cycling variants are
  **REJECTED** (crash risk) — do not retry without new root approval.
- **Personal guest:** canonical unlock baseline (six-sentinel native patch,
  final lib `cb2bd45b…ffb1`), `versionCode 29`, single-APK monolith signed with
  the Deck key `jcs2fresh` (cert `47:B6:…:C6:67`) — **not DB86**, which died in
  the 2026-09-17 wipe. Installed, intact; personal stable LOW already
  installed — no duplicate redeploy is queued. Editor candidate `cb2bd45b` (45 changed bytes vs pristine)
  was composed offline into the (now gone) local stage
  `staging/editor-save-20260915T010000Z` (local-only, never in git), used only in
  isolated scratch guests that have since been destroyed — never installed
  personally.
- **Editor save:** SAVE (`0x159a9a 48b3→00bf`) + Select (`0x15a9a8 b8b3→00bf`)
  NOPs are **statically proven only**. No actual custom save/reopen, no H1/H2.
  Server-listed-row fall-through was observed with `userLevels/` EMPTY, so it
   is **not** custom-save proof; earlier false server/Scepie provenance claims
   are **withdrawn**. Exact gap: CREATE-drive HUD LayTrack → Flatten →
   Checkpoint until nodes ≥ 2 → AddMessage play-test confirm → EDIT →
   build → checkpoint completion → PlaythroughComplete → SAVE. A fresh editor
   quit without checkpoint completion discards to car select — there is no
   quit-to-SAVE. All test guests cleaned; personal untouched.
- **Artwork (Steam closed):** shortcut renamed to `Jet Car Stunts 2`,
  appid `4192595540` preserved; wide v2 (car smaller, down/right) +
  cropped genuine RGBA logo applied; baked-checker logo renders rejected,
  never applied. Rendered Steam UI pickup needs one normal user Steam launch.

## Tested vs unverified

| Area | Offline-tested | Still live-unverified |
|---|---|---|
| TiltDrive gate/router | 546/546 whole offline matrix (`docs/CONTINUATION.md` §3) | unselectable by design 2026-09-18 (sensor-landscape display flip); in-race authority never verified |
| Quit hide/teardown | lifecycle/runner/window suites green | GamingMode presentation |
| Sound v3 | file/byte guards + prior live PASS on scratch | personal redeploy audible check |
| Editor SAVE/Select | static llvm + hash guards | whole custom save→list→reopen→restart chain |
| Steam name/art | on-disk VDF/PNG/hash verified | rendered UI pickup |

Nothing here is labeled ready / fully fixed.

## Layout (tracked source only)

```text
run-jcs2  linux-launcher/  controller/  game-fixes/  packaging/
steam/  artwork/steam-proposal/  run-offline-tests.py
docs/ (this handoff; historical root *.md stay local-only, not in git)
dev-deck-display/ (developer-only: Deck-local display pins, not distributed)
```

NOT distributed and NOT in git: `backups/`, `staging/`, `analysis/`,
`runtime/sdk`, AVD images, pulled APKs/saves, the signing private key (the
live key is `jcs2fresh` — DB86 died in the 2026-09-17 wipe).
Same-key (`DB:86:A5:6E:…:33:ED`) `-r` reinstall preserves userdata —
never uninstall, clear, reinvent, or rotate the key. See `docs/SETUP.md`.

## Next concrete tasks (serialized, one live slot, user drives live)

1. ~~User TiltDrive test card~~ — closed 2026-09-18: three live attempts, display flipped every time; Tilt Drive is unselectable now (see Current truth).
2. User GamingMode quit-presentation check.
3. Single isolated editor BUILD→SAVE→H1/H2 retry only after slots free.
4. Same-key upgrades as needed under the existing authorization (DB86 gates,
   host AVD backup, `install-multiple -r`, hash/cert/versionCode pins, scoped
   rollback on mismatch) — no duplicate personal redeploy of the already
   installed stable LOW / unlock baseline.

Rejected loops to avoid: predicate-level return-1, global
`Store_IsItemPurchased` forcing, seed rev1/2 reuse, audio cycling rebuilds,
`-qt-hide-window`, blind PLAY sweeps, synthetic-track/file tricks.
Full rules: `docs/CONTINUATION.md`.

## Operating rules (maintainers keep developing under these)

- No Steam launch/stop/restart, session switch, emulator boot, ADB touch,
  broad kills, or user-process termination. Live work is serialized through
  root with ports 5038/5594/5595 + lock files.
- Offline proof ≠ live proof. Report evidence paths with every claim.

No license choice is made here.
