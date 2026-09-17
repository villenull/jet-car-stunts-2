#!/usr/bin/env python3
"""Fidelity diff: stock split APK vs candidate split APK (reviewer tool).

Compares entry set, per-entry compression methods, STORED 4-byte
alignment, AndroidManifest.xml equality, libtrueaxis.so hash, and v1
certificate fingerprint. Exit 0 only if the candidate is faithful
(apart from the documented lib replacement + resign).

Usage:
    python3 fidelity_diff.py <stock.apk> <candidate.apk> [--expect-lib HASH]
"""
import hashlib
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

LIB = "lib/armeabi-v7a/libtrueaxis.so"


def entries(apk):
    with zipfile.ZipFile(apk) as z:
        return {i.filename: i for i in z.infolist() if not i.is_dir()}


def data_offsets(apk):
    raw = Path(apk).read_bytes()
    out = {}
    with zipfile.ZipFile(apk) as z:
        for i in z.infolist():
            if i.is_dir():
                continue
            name_len, extra_len = struct.unpack_from(
                "<HH", raw, i.header_offset + 26)
            out[i.filename] = i.header_offset + 30 + name_len + extra_len
    return out


def cert_fp(apk):
    with zipfile.ZipFile(apk) as z:
        rsa = next(n for n in z.namelist()
                   if n.startswith("META-INF/") and n.endswith(".RSA"))
        der = z.read(rsa)
    r = subprocess.run(["openssl", "pkcs7", "-inform", "DER", "-print_certs"],
                       input=der, capture_output=True, timeout=30)
    r.check_returncode()
    r2 = subprocess.run(["openssl", "x509", "-noout", "-sha256",
                         "-fingerprint"], input=r.stdout,
                        capture_output=True, timeout=30)
    r2.check_returncode()
    for line in r2.stdout.decode().splitlines():
        if "Fingerprint" in line:
            return line.split("=", 1)[1].strip()
    raise ValueError("no cert fingerprint")


def main():
    stock, cand = Path(sys.argv[1]), Path(sys.argv[2])
    expect_lib = sys.argv[4] if len(sys.argv) > 4 and sys.argv[3] == "--expect-lib" else None
    failures = []
    es, ec = entries(stock), entries(cand)
    only_stock = set(es) - set(ec) - {n for n in es if n.startswith("META-INF/")}
    only_cand = set(ec) - set(es) - {n for n in ec if n.startswith("META-INF/")}
    if only_stock:
        failures.append(f"entries missing from candidate: {sorted(only_stock)}")
    if only_cand:
        failures.append(f"extra entries in candidate: {sorted(only_cand)}")
    offs = data_offsets(cand)
    for name in sorted(set(es) & set(ec)):
        if name.startswith("META-INF/"):
            continue
        ms, mc = es[name].compress_type, ec[name].compress_type
        if name != LIB and ms != mc:
            failures.append(f"method change {name}: "
                            f"{'STORED' if ms == 0 else 'DEFLATED'} -> "
                            f"{'STORED' if mc == 0 else 'DEFLATED'}")
        if mc == zipfile.ZIP_STORED and offs[name] % 4:
            failures.append(f"STORED misaligned {name} @ {offs[name]}")
    with zipfile.ZipFile(stock) as z:
        manifest_stock, lib_stock = z.read("AndroidManifest.xml"), None
        try:
            lib_stock = z.read(LIB)
        except KeyError:
            pass
    with zipfile.ZipFile(cand) as z:
        manifest_cand, lib_cand = z.read("AndroidManifest.xml"), None
        try:
            lib_cand = z.read(LIB)
        except KeyError:
            pass
    if manifest_stock != manifest_cand:
        failures.append("AndroidManifest.xml differs")
    if lib_stock is not None and lib_cand is not None:
        if expect_lib and hashlib.sha256(lib_cand).hexdigest() != expect_lib:
            failures.append("candidate lib hash != expected")
        if lib_stock != lib_cand:
            print(f"lib differs (expected for patched splits): "
                  f"{hashlib.sha256(lib_stock).hexdigest()[:16]} -> "
                  f"{hashlib.sha256(lib_cand).hexdigest()[:16]}")
    print(f"stock cert:     {cert_fp(stock)}")
    print(f"candidate cert: {cert_fp(cand)}")
    if failures:
        print(f"FAIL ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: candidate faithful (methods/alignment/manifest; lib+cert differ as documented)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
