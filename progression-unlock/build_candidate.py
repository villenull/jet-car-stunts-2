#!/usr/bin/env python3
"""Build a guarded optional native-code variant; never changes guest or originals."""
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'dist/JCS2-Windows-Personal/assets/game/split_config.armeabi_v7a.apk'
EXPECTED_APK = 'dec90e8171fcf2cec465a582bd30e16cda3a5b739ec215f5d66c474de2fb23b8'
EXPECTED_LIB = 'bc7fdf9d62b7a2dc33ddd97d67e9f74830aea58aa059104dd97d9cbff408a34d'
LIB = 'lib/armeabi-v7a/libtrueaxis.so'
OFFSETS = (0x133046, 0x13D846, 0x13D944, 0x14BFBC, 0x14C10A, 0x14C11C)
JDK = ROOT / 'analysis/controller/20260910T140000Z/tools/jdk-17.0.20.1+1'
KEY_DIR = ROOT / 'analysis/offline-apk-hardening-20260910T140000Z/owneronly'
OUT = ROOT / 'progression-unlock/assets'

def sha(data):
    return hashlib.sha256(data).hexdigest()

def patched_library(data):
    if sha(data) != EXPECTED_LIB:
        raise ValueError('Refusing unexpected native library hash')
    out = bytearray(data)
    for pos in OFFSETS:
        if data[pos:pos+4] != bytes.fromhex('c06b0130'):
            raise ValueError(f'Unexpected instruction context at {pos:x}')
        out[pos:pos+2] = bytes.fromhex('0020')
    expected_changes = {pos+i for pos in OFFSETS for i in (0,1)}
    actual_changes = {i for i,(a,b) in enumerate(zip(data,out)) if a != b}
    assert actual_changes == expected_changes
    return bytes(out)

def main():
    raw = SOURCE.read_bytes()
    if sha(raw) != EXPECTED_APK:
        raise SystemExit('Refusing unexpected source APK hash')
    OUT.mkdir(parents=True, exist_ok=True)
    unsigned = OUT / 'progression-unsigned.apk'
    signed = OUT / 'split_config.armeabi_v7a-unlocked.apk'
    if signed.exists():
        raise SystemExit('Candidate already exists; refusing to overwrite evidence')
    with zipfile.ZipFile(SOURCE) as z:
        original = z.read(LIB)
        changed = patched_library(original)
        with zipfile.ZipFile(unsigned, 'w', compression=zipfile.ZIP_DEFLATED) as target:
            for entry in z.infolist():
                if entry.filename.startswith('META-INF/'):
                    continue
                target.writestr(entry.filename, changed if entry.filename == LIB else z.read(entry))
    # Existing final certificate; password remains in its protected file.
    command = [str(JDK/'bin/jarsigner'), '-keystore', str(KEY_DIR/'offline-local-developer.p12'),
               '-storetype','PKCS12','-storepass:file',str(KEY_DIR/'.storepass'),
               '-digestalg','SHA-256','-sigalg','SHA256withRSA','-sigfile','OFFLINE',
               '-signedjar',str(signed),str(unsigned),'offline']
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    (OUT/'signing.log').write_text(result.stdout+result.stderr)
    result.check_returncode()
    result = subprocess.run([str(JDK/'bin/jarsigner'),'-verify','-verbose','-certs',str(signed)],capture_output=True,text=True,timeout=30)
    (OUT/'verification.log').write_text(result.stdout+result.stderr)
    result.check_returncode()
    if 'jar verified.' not in result.stdout:
        raise SystemExit('JAR signature verification did not confirm success')
    with zipfile.ZipFile(signed) as z, zipfile.ZipFile(SOURCE) as source:
        assert z.testzip() is None
        assert z.read(LIB) == changed
        assert z.read('AndroidManifest.xml') == source.read('AndroidManifest.xml')
        assert all(i.compress_type == zipfile.ZIP_DEFLATED for i in z.infolist() if not i.is_dir()), 'Stored entries need alignment validation'
    report = dict(source_apk_sha256=EXPECTED_APK,source_lib_sha256=EXPECTED_LIB,
                  candidate_apk_sha256=sha(signed.read_bytes()),candidate_lib_sha256=sha(changed),
                  offsets=list(OFFSETS),changed_bytes=12,signature='JDK v1 verified; Android installation validation pending',
                  certificate_sha256='db86a56e9937045184a3b5b8e364e30f5333a30afda12739e11e7178b4ad33ed',
                  guest_modified=False,progression_only=True)
    (OUT/'candidate.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__ == '__main__':
    main()
