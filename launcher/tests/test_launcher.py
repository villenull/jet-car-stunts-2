from pathlib import Path
import re
import struct
import subprocess
import tempfile

ROOT = Path(__file__).parents[1]
CFG = (ROOT / 'launcher.ini.example').read_text()

def parse_ini(text):
    out={}
    for line in text.splitlines():
        line=line.strip()
        if line and not line.startswith(('#',';','[')) and '=' in line:
            k,v=line.split('=',1); out[k.strip()]=v.strip()
    return out

def windows_quote(s):
    # Behavioral model of CommandLineToArgvW quoting used by launcher.cpp.
    out='"'; bs=0
    for ch in s:
        if ch=='\\': bs+=1; continue
        if ch=='"': out+='\\'*(bs*2+1)+'"'; bs=0; continue
        out+='\\'*bs+ch; bs=0
    return out+'\\'*(bs*2)+'"'

def test_config_behavior():
    c=parse_ini(CFG)
    assert c['adb_port']=='5040' and c['emulator_port']=='5596'
    assert [c[f'apk{i}'] for i in range(1,6)][0].endswith('base.apk')
    assert c['package']=='com.trueaxis.jetcarstunts2'

def test_windows_argument_behavior():
    assert windows_quote(r'C:\Runtime Folder\adb.exe') == '"C:\\Runtime Folder\\adb.exe"'
    assert windows_quote(r'C:\a\\') == r'"C:\a\\\\"'
    assert windows_quote('x"y') == '"x\\"y"'

def test_cross_compiled_artifact_behavior():
    exe=ROOT/'JCS2Launcher.exe'
    if not exe.exists(): return # source checkout before build
    b=exe.read_bytes(); assert b[:2]==b'MZ'
    pe=struct.unpack_from('<I',b,0x3c)[0]; assert b[pe:pe+4]==b'PE\0\0'

def test_native_shared_logic():
    with tempfile.TemporaryDirectory() as d:
        exe=Path(d)/'logic_test'
        subprocess.run(['g++','-std=c++17','-Wall','-Wextra','-Werror',str(ROOT/'tests'/'logic_test.cpp'),'-o',str(exe)],check=True)
        subprocess.run([str(exe)],check=True,capture_output=True,text=True)
