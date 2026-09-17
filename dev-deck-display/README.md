# Deck-only display (developer-only)

Deck-specific local config: ignore the external monitor, always render on the
internal panel (eDP-1), in both Hyprland desktop and GamingMode. NOT part of
the public package; tracked here so it can be re-applied after updates.

## Why

Upstream `-O '*,eDP-1'` ranks every unlisted connector at priority 0 and the
panel at 1 (`parse_connector_priorities` / `get_connector_priority` /
`setup_best_connector` in gamescope `src/Backends/DRMBackend.cpp`; strict
minimum wins), so a connected external (DP-1) always beats eDP-1. Journal
proof, two boots: `Connectors: DP-1 (connected), eDP-1 (connected)` then
`selecting connector DP-1`.

## Files

- `gamescope-session-edp1`: GamingMode wrapper. Runtime-patches the
  pacman-owned `/usr/lib/steamos/gamescope-session` at session start
  (`-O '*,eDP-1'` → `-O eDP-1`), so upstream flag additions flow through.
  Warns (journal) instead of failing if the `-O` line ever vanishes.
- `override.conf`: user systemd drop-in content; install as
  `~/.config/systemd/user/gamescope-session.service.d/override.conf`.
  (The vendor unit's absolute `ExecStart` means a `~/.local/bin` PATH shim
  can never intercept — the drop-in names the wrapper path directly.)
- `monitors-dp1-disable.snippet.lua`: Hyprland desktop pin; append to
  `~/.config/hypr/monitors.lua`, then `hyprctl reload`.

`GAMESCOPE_DISPLAY_FORCE_INTERNAL` is deliberately NOT used: undocumented,
precedence unknown, and the adjacent `Disable internal displays` knob does
the opposite.

## Apply (on the Deck, as `deck`)

```sh
cp gamescope-session-edp1 ~/.local/bin/gamescope-session-edp1
chmod +x ~/.local/bin/gamescope-session-edp1
mkdir -p ~/.config/systemd/user/gamescope-session.service.d
cp override.conf ~/.config/systemd/user/gamescope-session.service.d/override.conf
systemctl --user daemon-reload
cat monitors-dp1-disable.snippet.lua >> ~/.config/hypr/monitors.lua
# in Hyprland: hyprctl reload
```

Takes effect on the next GamingMode entry. Fail-open: panel absent →
external still used; no-output only if nothing is connected.

## Verify

```sh
journalctl --user -u gamescope-session.service | grep -E 'selecting connector|Connectors:|DP-1 \(|eDP-1 \('
# want: selecting connector eDP-1
hyprctl monitors all -j  # want: eDP-1 only, DP-1 disabled=true
```

## Revert

```sh
rm ~/.config/systemd/user/gamescope-session.service.d/override.conf
systemctl --user daemon-reload
# drop the appended lines from ~/.config/hypr/monitors.lua; hyprctl reload
```

Post-`pacman -S gamescope` duty: diff vendor script lines ~250-268 against
the wrapper's sed expectation; the `$HOME` drop-in itself survives updates.
