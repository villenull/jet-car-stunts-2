# Current status

The prior AVD-manager dependency is fixed: the launcher now writes
`ANDROID_AVD_HOME\JCS2-personal.avd\config.ini` and its sibling
`JCS2-personal.ini` directly from the staged API28 Google APIs x86 image. It
requires a fresh empty state directory before claiming it, records a
`.jcs2-owned` marker, and preserves progress after ownership is established.

The launcher also owns a dedicated `adb nodaemon server` process in its Windows
job, performs root-authorized network isolation with transport reconnect and
post-unroot uid 2000 verification, and uses bounded Ctrl+C-aware cleanup.

The optional UHID controller integration is now in the launcher: all three
controller paths are resolved to absolute paths, the controller is job-owned
and starts after network isolation/non-root verification but before game launch,
and a unique per-session ready JSON marker is required within 30 seconds.
Invalid transport/serial, missing marker, startup failure, and controller exit
during play fail closed; normal cleanup removes only the unique marker while
preserving state. Physical Windows/WHPX and Android UHID behavior remain the
controller worker's validation scope.
