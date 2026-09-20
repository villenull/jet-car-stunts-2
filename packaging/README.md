# Linux portable package readiness — September 12, 2026

This is an offline plan, not an installer or a completed portable guest. Nothing
here launches the emulator, changes saved state, installs packages, or copies
runtime trees. The user performs all future live tests.

Run `python3 packaging/plan.py` from any working directory to print the current
read-only inventory and proposed AVD path changes. `--root PATH` inspects an
alternate source tree. The command returns a JSON report; a zero exit status means
the report completed, **not** that its `blockers` are resolved. The manifest is
an explicit selection, never a request to recursively package the project root.
`current-plan.json` records this machine's source paths and metadata; it contains
no extracted game save, keys, or backup payloads. Refresh it after source changes.

## Layout agreed with the launcher

```text
run-jcs2
steam/jcs2-steam-launch.sh
linux-launcher/           # explicit source/binary allowlist in plan.py
assets/controller/mapping.json
runtime/sdk/emulator/     # entire existing Linux tree, all libraries/plugins/assets
runtime/sdk/platform-tools/
runtime/sdk/system-images/android-28/google_apis/x86/  # whole image + sidecars
state/avd/hardened_api28.ini
state/avd/hardened_api28.avd/  # personal writable guest; all disk pairs together
state/logs/              # created at runtime, not a source log archive
```

`JCS2_LAYOUT=portable` explicitly selects these runtime/assets/state paths.
Legacy/default launch remains on the existing authorized AVD. `JCS2_SDK`,
`JCS2_AVD_HOME`, `JCS2_AVD`, `JCS2_DIST`, `JCS2_CONTROLLER`, and `JCS2_LOGDIR`
remain overrides. Relative paths resolve against the project root. A relocatable
launcher does not itself relocate the guest or install the game into a fresh AVD.
Personal tilt settings live in the selected log directory as `tilt-settings.json`.
They are absent on the audited source and omitted from the base manifest; preserve
that separate personal settings file explicitly if it is created before staging.
Default steering and pitch both use the left stick; there is no prelaunch touch
panel any more.

Source and runtime binaries are separate from the **stateful synthetic AVD**.
The latter holds the tested personal guest and saved progress and must not be
advertised as a pristine or generally redistributable Android image. Original
private backups, unrelated analysis trees, archived Windows binaries, APK archives,
test fixtures, historical menu navigation and overlay code are excluded. The saved
guest is deliberately a separate personal-state unit; no backup keys were read.

## Concrete relocation blockers

- `hardened_api28.ini` has an absolute `path` into this checkout. On a future
  staged copy, rewrite it to the final absolute `state/avd/hardened_api28.avd`
  directory; validate `path.rel` against the selected Android home rather than
  assuming the old `avd/...` value is authoritative.
- Persistent `config.ini` has `image.sysdir.1` pointing into
  `staging/windows-runtime/sdk/system-images/android-28/google_apis/x86/`.
  Rewrite the staged copy to the packaged Linux SDK's image directory. That
  existing image tree is already included in the manifest; retain every sidecar.
- Generated `hardware-qemu.ini` also contains absolute image, AVD, and SDK paths.
  These are recorded separately as `generated: true` in the report. The emulator
  may regenerate them at boot; do not mistake edits to this generated file alone
  for fixing the persistent profile. Other generated launch/lock files are not
  an authoritative relocation recipe. The planner inventories them but never
  deletes them or claims a ready-to-copy snapshot.
- The three inspected QCOW2 headers refer to relative sibling `cache.img`,
  `userdata-qemu.img`, and `encryptionkey.img`, all present. Preserve each overlay
  **and** base together, including the guest's encryption disk as state. No QCOW
  rebase/flatten is currently indicated by these relative names. No disk payload
  or encryption-key content was inspected. The planner flags external or missing
  backing references if the source changes; it does not recursively open them.
- Copying a stopped, consistent AVD with sparse-file preservation is a future
  operation. The planner neither verifies process state nor snapshots live disks.

## Dependencies inspected, and what is still unproven

`elf-audit.json` records `readelf` metadata from 128 ELF objects in the allowed
Linux emulator tree, ADB, and controller; none of those binaries was executed.
The highest referenced GLIBC symbol version in that set is **GLIBC_2.34**.
The controller requests `/lib64/ld-linux-x86-64.so.2` and `libc.so.6` /
`libresolv.so.2`. Emulator/QEMU also reference X11, PulseAudio, bundled Qt6,
Android emulator libraries, C++ support, and graphics/plugin dependencies. Keep
the whole emulator tree; DT_NEEDED metadata alone cannot prove runtime `dlopen`
closure, correct library search paths, or compatibility with a fresh SteamOS.

Host prerequisites to validate on the target are Bash, Python 3 (the active
launcher uses the standard library), x86-64 Linux/glibc, readable/writable KVM,
working host GPU/OpenGL/Vulkan drivers as required by the emulator, X11/XWayland
inside Gamescope, and controller device permissions. The window helper loads
X11 through ctypes. Driving reads Linux joystick devices; optional tilt reads
motion input devices. Guest helper execution uses the packaged JAR through ADB,
so a host Java installation is not part of the observed launcher contract.
No system packages, permissions, services, or host configuration were changed.

## Storage measurements

| Selected tree | Logical bytes | Allocated bytes reported by filesystem |
| --- | ---: | ---: |
| Linux emulator, complete | 859,352,592 | 860,213,248 |
| Existing platform-tools | 10,642,368 | 10,645,504 |
| API28 Google APIs x86 image, complete | 2,801,135,886 | 2,801,164,288 |
| Existing writable AVD | 8,151,767,174 | 1,750,507,520 |

The selected runtime plus personal AVD is about **11.01 GiB logical / 5.05 GiB
allocated**, before small launcher files. These are inventory estimates, not ZIP
size or unique Btrfs extents. Shared extents can make summed allocation misleading.
A naive nonsparse copy can consume the logical size, including the 6 GiB userdata
base. Therefore the historical 10 GiB guidance is not a guarantee for this
state-preserving Linux package. Budget separately for destination allocation,
archive/extraction overlap, future save growth, and OS headroom. No archive was
created and no second guest was allocated.

## Next steps after the user is available

1. User verifies Gaming Mode window/touch/controls and clean exit/relaunch.
2. Confirm the guest is stopped, choose a destination with enough space, and
   make a progress-preserving sparse staged copy from the allowlist. Preserve
   the entire Linux dependency tree and all guest backing pairs.
3. Rewrite persistent AVD paths on that staged copy to its final location,
   inspect generated-path regeneration and relative backing resolution, and
   run the launcher's portable offline preflight.
4. User tests the relocated personal guest, then separately tests a fresh
   SteamOS host. Verify visible gameplay, touch, controller permissions, exit,
   relaunch, and saved progress before calling the package finished.

A **fresh Android guest bootstrap** is a different unresolved deliverable: the
current runner intentionally does not create an AVD or install/restore the game.
This manifest preserves the existing personal guest; it does not silently add
private backup inputs or claim a first-install bootstrap exists.

Validation: `python3 -m unittest discover -s packaging -p 'test_*.py'` — six
synthetic tests covering allowlisting, path proposals, missing inputs, symlink
non-traversal, bounded QCOW header parsing, and unchanged source bytes.
