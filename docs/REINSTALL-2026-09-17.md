# JCS2 reinstall handoff — 2026-09-17 (fresh Omarchy → playable variant)

**Status date:** 2026-09-17 ~19:00 UTC. Deck reprovisioned from zero and the
patched game is INSTALLED + RESUMED in a fresh guest. This file is the entry
point for the next chat; `docs/RESTORE-2026-09-17.md` (wipe survey) and
`docs/CONTINUATION.md` (pre-wipe world) are historical.

## 1. Branch / repo pointers (all pushed)

- Local repo: `/home/villenull/Projects/jet-car-stunts-2`, branch `v2`
  tracking `origin/v2` @ `e3e5a61`.
- `origin/v2` since the wipe survey (`b97cf79`):
  - `f032254` — stage 4 approved Steam grid PNGs into `steam/artwork/`
    (cover/landscape/hero/logo; hashes in §5).
  - `e7c4c3f` — `game-fixes/monolith/compose_monolith_candidate.py` (new):
    45-byte variant (audio v3 + maps 12B + 6 unlock sentinels + controls
    NOP + editor save/reopen NOPs) composed onto a monolithic APK.
  - `d1d1209` — `packaging/bootstrap_linux_guest.py`: accept exactly-1
    monolith APK alongside legacy 5 splits; single `adb install` path.
  - `e3e5a61` — fresh-claim marker-gate fix (`guest_freshly_claimed`) +
    regression tests (17 bootstrap tests).
- Offline suite: **465/465 OK across 16 suites**
  (`python3 run-offline-tests.py`).
- Workflow (user order): all dev/push HERE; Deck is deploy-via-rsync +
  test over SSH only. Deck git is rsync-seeded (no `gh auth` there).
  Untracked local-only: `WATCHDOG.yml` (pre-existing, not ours).

## 2. Payload (the GitHub question, resolved)

- `*.apk` can NEVER live in git: root `.gitignore` denies them (×3) AND
  GitHub hard-rejects files >100 MB (this APK is 156 MB).
- User chose: **private repo Release asset**. Draft release
  `jcs2-pristine-v1.0.23` exists but the 156 MB upload stalls
  (`gh release upload` HTTP 500 after ~8 min; asset list still empty).
  Retry later; NOT blocking play.
- Wipe-proof copies NOW: `/home/villenull/Downloads/com.trueaxis.jetcarstunts2.apk`
  + `deck:~/Projects/JCS2/staging/pristine-v1.0.23.apk`, both
  `sha256 779f2103…7c30fe`.
- Source APK: `versionCode 29 / versionName 1.0.23`, stock True Axis cert
  `97:D4:…:EA:36`, `libtrueaxis.so bc7fdf9d…` (pristine, all 13 patch
  ranges verified original+disjoint).
- Lucky Patcher: NOT needed. Unlock = 6-sentinel native patch
  (`c06b→0020`, in-repo), no LP APK involved.

## 3. Installed variant (on Deck, proven)

- Compose (on Deck): `staging/pristine-v1.0.23.apk` →
  `staging/monolith-20260917T182312Z/unsigned-monolith.apk` (1362 entries,
  META-INF stripped) → signed with NEW Deck key `jcs2fresh`
  (`~/.jcs2-signing/jcs2-fresh.p12`, keytool RSA-4096, NOT DB86 — DB86
  material died in the wipe): `staging/install-monolith/signed-monolith.apk`.
- Gates: final lib `cb2bd45b…ffb1` (45 changed bytes / 13 sites, 45-diff
  verified), manifest `versionCode 29`, `jarsigner -verify` → `jar verified.`,
  new cert `47:B6:…:C6:67`, source manifest byte-identical.
- Install: `packaging/bootstrap_linux_guest.py --execute --headless` on
  `jcs2-fresh` → `JCS2 bootstrapped on 127.0.0.1:5595`.
- Device truth: `pm path` → single `base.apk`; `versionCode=29 /
  versionName=1.0.23`; pulled-APK cert == `47:B6:…:C6:67`; game pid live;
  `mResumedActivity … Jetcarstunts2Activity`; helper JAR pushed
  (`/data/local/tmp/jcs2-input-helper.jar`); markers
  (`.jcs2-install-state`, `.jcs2-bootstrap-state`, `.jcs2-owned`) written.
- Variant content: stable LOW v3 + maps ownership 12B + unlock-all
  sentinels + controls button-gate NOP + editor SAVE/reopen NOPs.

## 4. Deck state (deck@192.168.100.25, fresh Omarchy)

- Clone `~/Projects/JCS2` @ `e3e5a61`, clean; `runtime/` (python 3.12,
  platform-tools 37.0.1, emulator 37.1.11, API28 x86 img r12, sha-verified),
  Go 1.27.1 (`~/go`), Temurin JDK 21 (`~/jdk-21`, jarsigner + build-tools
  apksigner present), controller binary built (3.4 MB, `--help` OK),
  helper JAR rebuilt (classes.dex, 6212 B).
- AVD `jcs2-fresh` (swiftshader_indirect, 1536 MiB, landscape) claimed +
  markers; portable preflight
  (`JCS2_LAYOUT=portable JCS2_AVD=jcs2-fresh … --check`) → `errors: []`.
- Display pins BOTH applied (GamingMode wrapper + drop-in + Hyprland DP-1
  disable); journal shows pre-pin boots already selecting eDP-1.
- **Guest LEFT RUNNING** (emulator + game resumed) for the feedback session.
  Owned adb was on port 5038; a stray `adb -L tcp:5038` fork-server from
  manual probes was killed once (it blocked a bootstrap retry).
- Disk 31G used / 444G avail.

## 5. Steam wireup (ready, NOT yet run)

- Art staged + synced: `steam/artwork/` cover `ec637ea0…`, landscape
  `2203cfe3…`, hero `052881f7…`, logo `8dff6d01…`, icon `8b25a8c9…`.
- Fresh-Deck facts: old appid `4192595540` is UNREPRODUCIBLE (path-derived);
  register will mint a NEW id — read it from plan/register JSON, never the
  old one. Account `userdata/1486631847/` exists; no `shortcuts.vdf` yet.
- After PLAY smoke, user exits Steam fully, then on Deck:
  `python3 steam/steam_shortcut.py plan/register --install-root $PWD
  --account 1486631847 --launch-target steam/jcs2-steam-launch.sh`
  (+ `--confirm-steam-closed` for register), hash re-check, one normal
  user Steam launch for pickup. Needs `install-state.json {complete}` +
  `state/` (installer lane; register exits 7 without it).

## 6. Next tasks (in order)

1. **User PLAY smoke** (user drives): visible game, drive a lap, sound LOW
   audible check, quit path. Report six one-liners.
2. Retry the stalled Release upload (pristine, then signed variant).
3. Steam shortcut + art register per §5.
4. TiltDrive card, GamingMode quit check, editor BUILD→SAVE→H1/H2 retry —
   all live-unverified on the new guest.
5. `install-state.json`/packaging `distribution.json` status fields still
   say installer-not-built; update when the portable package is real.

## 7. Standing notes

- No publishing authorized. No DB86 continuity claims (new key `47:B6…`).
- Stray-probe lesson: manual `adb -P 5038` commands fork a server that
  collides with the bootstrap/runner port probe — kill it or use `-P` on
  a scratch port for reads.
- Bootstrap gotchas fixed this session: monolith single-APK mode,
  fresh-claim marker gate (failed first run left config+owned marker but
  no pending marker → second run refused as "restore").
