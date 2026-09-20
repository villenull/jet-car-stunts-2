# Personal-game deployment / rollback procedure (offline-prepared)

Status: SUPERSEDED IN PART (user scope Sept 14): unlock-all is now the
canonical default (planner `--variant unlock-all` default, sentinel pins,
Hurricane user-test route; `test_deploy_plan.py` green). The normal-install
flow below REMAINS VALID as the tested fallback and documents the Sept 13
personal normal install (receipt verified, save preserved). Unlock-all
requires no selector; legacy choice intents are retired, never consumed.
Final installer approval stays gated. Runner/progression code untouched;
needed code changes go via root.

## 0. What is being deployed (evidence, not claims)

- Canonical variant `normal`:
  `staging/progression-variants/normal/apks-signed` (5 splits, DB86 key,
  versionCode 29). Offline pins verified: per-file hashes == record ==
  `staging/maps-ownership-v1` source (byte-identical publication);
   uniform DB86 cert on all five (openssl-extracted); native bytes present
   (sound v3 true-NOPs + Levels 12B; per-SKU seed hook OMITTED-no-seed per
   maps-ownership-provenance.json — store shows BuyNow, gameplay opens via
   12B; deep review is d853's lane, recorded PASS on bytes present).
- Personal baseline (from handoff evidence, NOT re-probed here): DB86
  progression build installed, "cert match", but the session install
  dropped en/es/xhdpi → expect base+armeabi only, possibly mixed split
  state. Same-key 5-split `install-multiple -r` restores the missing
  splits while the platform preserves app data. Live cert/version gate
  decides; any mismatch aborts with zero device changes.

## 1. Selection: default canonical normal, no implicit unlock-all

- `progression-choice` default reads `normal`; legacy files carry no
  switch intent, so the runner proceeds with the current install unless
  the user explicitly opts in. Unlock-all requires a separate affirmative
  tap and is OUT of scope for this deploy.
- PRIMARY path (no new code): user opens the prelaunch panel →
  Progression (shows normal) → Save & Play (explicit) → next runner
  launch consumes the single intent at the owned prelaunch boundary
  (post-isolate, pre-verify-packages) and performs the same-key
  reinstall, or proceeds unchanged with a recorded warning if ownership
  is unprovable. Abort on uncertain outcome (no game launch).
- FALLBACK path: the exact manual adb sequence emitted by
  `plan_personal_deploy.py` (section 3), executed by root/user at the
  live slot after maps PASS.

## 2. Same-key upgrade requirements (all must hold live, else abort)

1. Slot: maps live work released; no emulator/foreign procs; runner
   ports free; target serial owned (cmdline+env verified).
2. Emulator STOPPED for step 3; booted guest for steps 4–7.
3. Installed package present with per-split pulled cert == DB86 on every
   present split AND versionCode == 29 (else abort — never uninstall).
4. Host backup complete with manifest (step 3).

## 3. Backup (read-only source; emulator stopped)

```
BACKUP=/tmp/pc-personal-backup-<UTC>
cp -a "$AVDHOME" "$BACKUP/avdhome"          # full save image backup
sha256sum "$BACKUP"/avdhome/hardened_api28.ini > "$BACKUP/host-manifest.txt"
```
Rationale: google_apis user image → `adb root` unavailable, so app-data
backup is impossible over adb; the host-side userdata copy IS the full
save backup. The split-APK pulls in step 4 are the rollback material.

## 4. Pre-install evidence (abort on any mismatch; device untouched)

```
adb -P 5038 -s 127.0.0.1:5595 shell pm path com.trueaxis.jetcarstunts2 | tee $BACKUP/pm-path-before.txt
adb -P 5038 -s 127.0.0.1:5595 shell dumpsys package com.trueaxis.jetcarstunts2 | grep -E "versionCode|signatures" | tee $BACKUP/dumpsys-before.txt
# pull each listed split to $BACKUP/installed-<split>; fingerprint each:
# abort unless every present split == DB86 and versionCode == 29.
```

## 5. Install (reinstall-only; preserves medals/records)

```
adb -P 5038 -s 127.0.0.1:5595 install-multiple -r --no-streaming \
  base.apk split_config.armeabi_v7a.apk split_config.en.apk \
  split_config.es.apk split_config.xhdpi.apk   # canonical normal paths
```

## 6. Post-verify (must all hold; else scoped recovery §8)

`pm path` shows all 5 splits → pull each → hashes equal the variant
record → cert DB86 → versionCode 29 → launch → PLAY smoke → audible
check (both user-driven gates).

## 7. Audio-note activation (joint, owner-executed, via root)

**SUPERSEDED 2026-09-18:** the panel that carried the note
(`controls_settings.py:84`, "Sound mode changes take effect after
restarting the game.") was removed with the driving-mode panel, so this
tree owes no reword — nothing in the lane displays that sentence any
more. The step below applies only to a build that still carries the panel.

The build contains sound v3 (taps are safe no-ops, LOW always), which
invalidated that sentence. The reword to fixed-Low-Latency truthful
text is tilt-owner/reviewer-executed IN THE SAME deploy window (staged
wording approved by reviewer), never before: the old sentence is
accurate for unpatched builds. This procedure does not edit UI Python.

## 8. Rollback — explicit scoped failure recovery ONLY

Trigger: post-verify mismatch/unknown after install, or explicit user
direction. Never automatic. Action: reinstall the step-4 pulled splits:

```
adb -P 5038 -s 127.0.0.1:5595 install-multiple -r --no-streaming $BACKUP/installed-*.apk
```
then re-run §6 verification. If rollback also fails to verify: stop,
preserve `$BACKUP` + logs, escalate to root (host-side avdhome restore
is a last resort, user-approved only).

## 9. Left for live verification (explicit)

Slot grant; §2 gates; backup manifest; §4→§6 evidence; PLAY + audible
user gates; §7 owner edit landed; receipt/status reflects the switch.
