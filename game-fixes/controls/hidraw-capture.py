#!/usr/bin/env python3
"""Capture / analyze the Steam-active Deck IMU hidraw stream (tilt owner tool).

While the Steam client runs, the IMU evdev node is gone (proven Sept-14:
hid-steam tears down event8+js1) but ``/dev/hidraw2`` (HID function
``0003:28DE:1205.0004``, same physical input2 function) streams 64-byte
reports at ~100 Hz. No decode is claimed here: this tool captures frames
for physical-rotation validation and analyzes which byte offsets vary.

Usage (operator-managed user capture — user only opens Steam + moves):
  hidraw-capture.py wait-capture [--wait-minutes 10] [--seconds 16]
      # waits for the identity-resolved IMU hidraw node (28DE:1205,
      # PHYS .../input2, hidraw child — never a hardcoded /dev number),
      # readiness-checks valid framed reports (ver=1/type=9/len=64 +
      # advancing seq, read-only, no grab, no writes), captures the
      # STILL/ROLL/PITCH/YAW phase timeline to an absolute workspace
      # NDJSON path, then analyzes inline. Operator relays phase cues.
      # Exit 0 ok / 2 node timeout / 3 stream not ready. Never hangs.

Direct (developer) use:
  hidraw-capture.py capture --seconds 12 --out motion.ndjson
      # Deck STILL 0-3 s, then the 3 motions below (one per ~3 s window):
      #   1. roll right ~30 deg and back to level (steering axis)
      #   2. pitch nose-down ~30 deg and back to level (pitch axis)
      #   3. yaw left-right twice, Deck kept level (gyro-only check)
  hidraw-capture.py analyze --in motion.ndjson
      # header_ok + seq_monotonic must be true; gyro_max_abs_rest flags
      # rest bias; rotation windows must move gyro fast and accel on a
      # 1 g norm (16384 LSB/g). Only then is a decode shippable.

Exit 0 on success; 2 when the hidraw node is missing (Steam idle uses
evdev instead — see tilt_control.motion_reader).
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "linux-launcher"))
from tilt_control.hidraw_reader import (
    HidrawFrameReader, capture_to_ndjson, analyze_variance,
    parse_deck_report, seq_is_monotonic, resolve_imu_hidraw,
    check_stream_ready,
    HIDRAW_DEFAULT_PATH, REPORT_LEN, DECK_REPORT_VERSION, DECK_REPORT_TYPE,
    DECK_REPORT_LEN,
)
from tilt_control.motion_reader import MotionReader

DEFAULT_OUT_DIR = (Path(__file__).resolve().parents[2] / "analysis" /
                   "linux-launcher" / "logs")

# Operator-relayed motion timeline inside one capture (user has no terminal;
# the operator reads these cues aloud / on screen and tells the user when).
# Cues are GUIDANCE for the no-delay case; with user delay, correlate via
# the wall-clock anchors (capture header row + completion JSON) against
# operator-noted motion times — never invalidate merely on cue delay.
PHASES = [
    (0, 3, "STILL — hold the Deck steady, level, hands off sticks"),
    (3, 6, "ROLL — tilt right ~30 deg, hold 1 s, return to level"),
    (6, 9, "PITCH — nose down ~30 deg, hold 1 s, return to level"),
    (9, 12, "YAW — rotate left-right twice, Deck kept level"),
]
MARK_EVERY_S = 10.0  # elapsed-time marks for delay-tolerant correlation


def cmd_capture(args) -> int:
    reader = HidrawFrameReader(args.device)
    if not reader.open():
        print(f"ERROR: {reader.last_error}", file=sys.stderr)
        print("HINT: the hidraw IMU node exists only while Steam runs; "
              "with Steam idle use the evdev path.", file=sys.stderr)
        return 2
    try:
        with open(args.out, "w", encoding="utf-8") as stream:
            count = capture_to_ndjson(reader, args.seconds, stream)
    finally:
        reader.close()
    print(json.dumps({"frames": count, "out": args.out,
                      "short_frames": reader.short_frames}))
    return 0


def cmd_analyze(args) -> int:
    rows = []
    wall_start = None
    with open(args.infile, encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "hex" not in obj:
                wall_start = wall_start or obj.get("wall_start")
                continue
            rows.append(obj["hex"])
    if not rows:
        print("ERROR: no frames in capture", file=sys.stderr)
        return 2
    variance = analyze_variance(rows)
    parsed, bad = [], 0
    for h in rows:
        try:
            parsed.append(parse_deck_report(bytes.fromhex(h)))
        except ValueError:
            bad += 1
    gyro_rest = [abs(v) for p in parsed for v in p["gyro_raw"]]
    accel_norms = [sum(v * v for v in p["accel_raw"]) ** 0.5 for p in parsed]
    print(json.dumps({
        "frames": len(rows),
        "wall_start": wall_start,
        "header_ok": bad == 0,
        "header_bad": bad,
        "identity": {"version": DECK_REPORT_VERSION, "type": DECK_REPORT_TYPE,
                     "len": DECK_REPORT_LEN},
        "report_len_ok": all(len(h) == REPORT_LEN * 2 for h in rows),
        "seq_monotonic": seq_is_monotonic(parsed) if parsed else False,
        "seq_first": parsed[0]["seq"] if parsed else None,
        "seq_last": parsed[-1]["seq"] if parsed else None,
        # Plausibility flags only — NOT motion validation (see module docs).
        "gyro_max_abs_rest": max(gyro_rest) if gyro_rest else None,
        "accel_norm_mean_rest": (sum(accel_norms) / len(accel_norms)
                                 if accel_norms else None),
        "varying_offsets": sorted(offset for offset, distinct in variance.items()
                                  if distinct > max(2, len(rows) // 10)),
        "variance": variance}, indent=2))
    return 0


def cmd_evdev_capture(args) -> int:
    """Operator-start explicit evdev IMU capture (THE chosen stream).

    Captures ONLY the evdev motion node (tier1 identity). Never opens
    hidraw: opening a hidraw node invokes the driver's ll_open path and
    tears down the evdev node mid-capture (proven Sept-14, reversible on
    close but destructive to run concurrently). No waiting, no Steam
    teardown gating, no boot, no writes, no grabs.
    """
    out_path = Path(args.out)
    if not out_path.is_absolute():
        print("ERROR: --out must be an absolute workspace path", file=sys.stderr)
        return 2
    reader = MotionReader()
    if not reader.open():
        print(json.dumps({"state": "not-ready",
                          "detail": reader.last_error}), flush=True)
        return 3
    print(json.dumps({"state": "ready", "device": reader.device_path,
                      "tier": reader.match_tier}), flush=True)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        wall_start = datetime.now(timezone.utc).isoformat()
        count = 0
        acc_sum = [0, 0, 0]
        with open(out_path, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"v": 1, "wall_start": wall_start,
                                     "source": "evdev",
                                     "device": reader.device_path,
                                     "tier": reader.match_tier}) + "\n")
            start = time.monotonic()
            announced, next_mark = set(), MARK_EVERY_S
            while time.monotonic() - start < args.seconds:
                elapsed = time.monotonic() - start
                for index, (phase_start, phase_end, cue) in enumerate(PHASES):
                    if phase_start <= elapsed < phase_end and index not in announced:
                        announced.add(index)
                        print(json.dumps({"state": "phase",
                                          "t": round(elapsed, 1),
                                          "cue": cue}), flush=True)
                if elapsed >= next_mark:
                    print(json.dumps({"state": "mark",
                                      "t": round(elapsed, 1),
                                      "frames": count}), flush=True)
                    next_mark += MARK_EVERY_S
                for sample in reader.read_samples():
                    stream.write(json.dumps(
                        {"t_mono": sample.timestamp,
                         "acc": [sample.acc_x, sample.acc_y, sample.acc_z],
                         "gyro": [sample.gyro_x, sample.gyro_y,
                                  sample.gyro_z]}) + "\n")
                    count += 1
                    acc_sum[0] += sample.acc_x
                    acc_sum[1] += sample.acc_y
                    acc_sum[2] += sample.acc_z
                if not reader.is_open:
                    print(json.dumps(
                        {"state": "evdev-lost",
                         "detail": reader.last_error}), flush=True)
                    break
                time.sleep(0.005)
    finally:
        reader.close()
    wall_end = datetime.now(timezone.utc).isoformat()
    mean = [round(a / count, 1) if count else None for a in acc_sum]
    print(json.dumps({"state": "captured", "frames": count,
                      "out": str(out_path),
                      "accel_mean_rest": mean,
                      "wall_start": wall_start,
                      "wall_end": wall_end}), flush=True)
    return 0


def cmd_dual_capture(args) -> int:
    """SUPERSEDED by evdev-capture (kept for reference, do not use live).

    Concurrent evdev+hidraw opening is proven harmful: opening the
    hidraw node triggers the driver's ll_open path and tears down the
    evdev node mid-capture (reversible on close, but destructive while
    running). Use evdev-capture (chosen stream) instead.
    """
    out_base = Path(args.out_prefix)
    if not out_base.is_absolute():
        print("ERROR: --out-prefix must be absolute", file=sys.stderr)
        return 2
    hnode, hdetail = resolve_imu_hidraw()
    reader = MotionReader()
    evdev_ok = reader.open()
    evdev_err = reader.last_error
    hidraw_n, evdev_n = 0, 0
    hreader = HidrawFrameReader(hnode) if hnode else None
    hready_detail = "no hidraw node: " + hdetail if not hnode else ""
    try:
        if hreader and hreader.open():
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and hidraw_n < 10:
                for _ts, frame in hreader.read_frames():
                    try:
                        parse_deck_report(frame)
                        hidraw_n += 1
                    except ValueError:
                        hready_detail = "framing rejected"
                        break
                time.sleep(0.01)
            hready_detail = (f"{hidraw_n} valid frames @ {hnode}"
                             if hidraw_n else (hready_detail or
                                               "no frames in 5 s"))
        elif hnode:
            hready_detail = hreader.last_error or "hidraw open failed"
        if evdev_ok:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and evdev_n < 10:
                evdev_n += len(reader.read_samples())
                time.sleep(0.01)
    finally:
        if hreader:
            hreader.close()
        reader.close()
    ready = {"state": "READY" if (evdev_n or hidraw_n) else "NOT-READY",
             "evdev": (f"{evdev_n} samples in 5 s (opened)"
                       if evdev_ok else f"unopened: {evdev_err}"),
             "hidraw": hready_detail,
             "hidraw_node": hnode or "-"}
    print(json.dumps(ready), flush=True)
    if args.check_only or not (evdev_n or hidraw_n):
        return 0 if (evdev_n or hidraw_n) else 3
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evdev_path = out_base.parent / f"{out_base.name}-evdev-{stamp}.ndjson"
    hidraw_path = out_base.parent / f"{out_base.name}-hidraw-{stamp}.ndjson"
    evdev_path.parent.mkdir(parents=True, exist_ok=True)
    reader2 = MotionReader()
    hreader2 = HidrawFrameReader(hnode) if hnode else None
    if not reader2.open():
        print(json.dumps({"state": "evdev-lost",
                          "detail": reader2.last_error}), flush=True)
    if hreader2 and not hreader2.open():
        print(json.dumps({"state": "hidraw-lost",
                          "detail": hreader2.last_error}), flush=True)
        hreader2 = None
    try:
        wall_start = datetime.now(timezone.utc).isoformat()
        ev_count = hid_count = 0
        with open(evdev_path, "w", encoding="utf-8") as ev_stream, \
                open(hidraw_path, "w", encoding="utf-8") as hid_stream:
            ev_stream.write(json.dumps({"v": 1, "wall_start": wall_start,
                                        "source": "evdev"}) + "\n")
            hid_stream.write(json.dumps({"v": 1, "wall_start": wall_start,
                                         "source": "hidraw",
                                         "device": hnode}) + "\n")
            start = time.monotonic()
            announced, next_mark = set(), MARK_EVERY_S
            while time.monotonic() - start < args.seconds:
                elapsed = time.monotonic() - start
                for index, (ps, pe, cue) in enumerate(PHASES):
                    if ps <= elapsed < pe and index not in announced:
                        announced.add(index)
                        print(json.dumps({"state": "phase",
                                          "t": round(elapsed, 1),
                                          "cue": cue}), flush=True)
                if elapsed >= next_mark:
                    print(json.dumps({"state": "mark",
                                      "t": round(elapsed, 1),
                                      "evdev": ev_count,
                                      "hidraw": hid_count}), flush=True)
                    next_mark += MARK_EVERY_S
                for sample in reader2.read_samples():
                    ev_stream.write(json.dumps(
                        {"t_mono": sample.timestamp,
                         "acc": [sample.acc_x, sample.acc_y, sample.acc_z],
                         "gyro": [sample.gyro_x, sample.gyro_y,
                                  sample.gyro_z]}) + "\n")
                    ev_count += 1
                if hreader2:
                    for mono_ts, frame in hreader2.read_frames():
                        hid_stream.write(json.dumps(
                            {"t_mono": mono_ts, "hex": frame.hex()}) + "\n")
                        hid_count += 1
                time.sleep(0.005)
    finally:
        reader2.close()
        if hreader2:
            hreader2.close()
    wall_end = datetime.now(timezone.utc).isoformat()
    print(json.dumps({"state": "captured", "evdev_frames": ev_count,
                      "hidraw_frames": hid_count,
                      "evdev_out": str(evdev_path),
                      "hidraw_out": str(hidraw_path),
                      "wall_start": wall_start, "wall_end": wall_end}),
          flush=True)
    args.infile = str(hidraw_path)
    return cmd_analyze(args)


def cmd_wait_capture(args) -> int:
    """Operator-managed passive capture (user only opens Steam + moves).

    1. WAIT (up to --wait-minutes) for the identity-resolved IMU hidraw
       node to appear (user opens Steam; hid-steam keeps .0004/hidraw).
    2. READINESS: open O_RDONLY|O_NONBLOCK (no grab, no writes) and
       require valid framed reports with advancing seq, else abort.
    3. CAPTURE fixed --seconds to an absolute workspace NDJSON path,
       printing operator phase cues (STILL/ROLL/PITCH/YAW) for relay.
    4. ANALYZE inline and print the summary JSON.
    Exit: 0 captured+validated-framing; 2 node never appeared (timeout);
    3 stream not ready/invalid framing. Never waits forever.
    """
    out_path = Path(args.out)
    if not out_path.is_absolute():
        print("ERROR: --out must be an absolute workspace path", file=sys.stderr)
        return 2
    deadline = time.monotonic() + max(60.0, args.wait_minutes * 60.0)
    print(json.dumps({"state": "waiting",
                      "detail": "waiting for Steam-active IMU hidraw "
                                "(user opens Steam; no action taken here)"}),
          flush=True)
    from tilt_control.motion_reader import (
        _find_motion_event_node as _evdev_probe)
    node, detail, evdev = None, "", None
    last_notice = 0.0
    while True:
        node, detail = resolve_imu_hidraw()
        evdev = _evdev_probe()
        steam_active = node is not None and evdev is None
        if steam_active:
            break
        if time.monotonic() >= deadline:
            node = None
            break
        if time.monotonic() - last_notice >= 30.0:
            last_notice = time.monotonic()
            print(json.dumps(
                {"state": "waiting",
                 "hidraw": node or "-",
                 "evdev": ("present (Steam idle — still waiting)"
                           if evdev else "absent"),
                 "detail": detail}), flush=True)
        time.sleep(2.0)
    if node is None or evdev is not None:
        print(json.dumps({"state": "timeout", "detail": detail,
                          "evdev": evdev or "-"}), flush=True)
        return 2
    print(json.dumps({"state": "found", "device": node, "detail": detail}),
          flush=True)
    ok, ready_detail = check_stream_ready(node, timeout_s=args.ready_timeout)
    if not ok:
        print(json.dumps({"state": "not-ready", "device": node,
                          "detail": ready_detail}), flush=True)
        return 3
    print(json.dumps({"state": "ready", "device": node,
                      "detail": ready_detail}), flush=True)
    reader = HidrawFrameReader(node)
    if not reader.open():
        print(json.dumps({"state": "open-failed",
                          "detail": reader.last_error}), flush=True)
        return 3
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        wall_start = datetime.now(timezone.utc).isoformat()
        with open(out_path, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"v": 1, "wall_start": wall_start,
                                     "device": node}) + "\n")
            announced = set()
            next_mark = MARK_EVERY_S
            count = 0
            while time.monotonic() - start < args.seconds:
                elapsed = time.monotonic() - start
                for index, (phase_start, phase_end, cue) in enumerate(PHASES):
                    if phase_start <= elapsed < phase_end and index not in announced:
                        announced.add(index)
                        print(json.dumps(
                            {"state": "phase", "t": round(elapsed, 1),
                             "cue": cue}), flush=True)
                if elapsed >= next_mark:
                    print(json.dumps({"state": "mark", "t": round(elapsed, 1),
                                      "frames": count}), flush=True)
                    next_mark += MARK_EVERY_S
                for mono_ts, frame in reader.read_frames():
                    stream.write(json.dumps({"t_mono": mono_ts,
                                             "hex": frame.hex()}) + "\n")
                    count += 1
                time.sleep(0.005)
    finally:
        reader.close()
    wall_end = datetime.now(timezone.utc).isoformat()
    print(json.dumps({"state": "captured", "frames": count,
                      "short_frames": reader.short_frames,
                      "out": str(out_path), "wall_start": wall_start,
                      "wall_end": wall_end}), flush=True)
    args.infile = str(out_path)
    return cmd_analyze(args)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture", help="record hidraw frames to NDJSON")
    capture.add_argument("--seconds", type=float, default=10.0)
    capture.add_argument("--out", required=True)
    capture.add_argument("--device", default=HIDRAW_DEFAULT_PATH)
    analyze = sub.add_parser("analyze", help="variance report over a capture")
    analyze.add_argument("--in", dest="infile", required=True)
    wait = sub.add_parser("wait-capture",
                          help="wait for Steam hidraw, readiness-check, "
                               "capture phases, analyze (operator-managed)")
    wait.add_argument("--wait-minutes", type=float, default=15.0)
    wait.add_argument("--ready-timeout", type=float, default=30.0)
    wait.add_argument("--seconds", type=float, default=90.0)
    wait.add_argument("--out", default=None)
    dual = sub.add_parser("dual-capture",
                          help="SUPERSEDED by evdev-capture (concurrent "
                               "hidraw open tears down evdev; reference only)")
    dual.add_argument("--seconds", type=float, default=90.0)
    dual.add_argument("--out-prefix", required=True)
    dual.add_argument("--check-only", action="store_true",
                      help="validate both streams (~8 s), no timed window")
    evdev = sub.add_parser("evdev-capture",
                           help="explicit-start evdev IMU capture "
                                "(chosen stream; never touches hidraw)")
    evdev.add_argument("--seconds", type=float, default=90.0)
    evdev.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.command == "capture":
        return cmd_capture(args)
    if args.command == "evdev-capture":
        return cmd_evdev_capture(args)
    if args.command == "dual-capture":
        return cmd_dual_capture(args)
    if args.command == "wait-capture":
        if args.out is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            args.out = str(DEFAULT_OUT_DIR /
                           f"hidraw-user-capture-{stamp}.ndjson")
        return cmd_wait_capture(args)
    return cmd_analyze(args)


if __name__ == "__main__":
    raise SystemExit(main())
