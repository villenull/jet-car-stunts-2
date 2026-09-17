param([switch]$Clean)
$ErrorActionPreference='Stop'; $out=Join-Path $PSScriptRoot 'JCS2Launcher.exe'
$src=Join-Path $PSScriptRoot 'launcher.cpp'
if (Get-Command clang-cl -ErrorAction SilentlyContinue) { clang-cl /nologo /std:c++17 /EHsc /W4 /O2 /DUNICODE /D_UNICODE $src /link bcrypt.lib ws2_32.lib /OUT:$out }
elseif (Get-Command cl.exe -ErrorAction SilentlyContinue) { cl.exe /nologo /std:c++17 /EHsc /W4 /O2 /DUNICODE /D_UNICODE $src /link bcrypt.lib ws2_32.lib /OUT:$out }
elseif (Get-Command x86_64-w64-mingw32-clang++ -ErrorAction SilentlyContinue) { x86_64-w64-mingw32-clang++ -std=c++17 -Wall -Wextra -Wpedantic -O2 -municode -static -o $out $src -lbcrypt -lws2_32 }
elseif (Get-Command x86_64-w64-mingw32-g++ -ErrorAction SilentlyContinue) { x86_64-w64-mingw32-g++ -std=c++17 -Wall -Wextra -Wpedantic -O2 -municode -static -o $out $src -lbcrypt -lws2_32 }
else { throw 'No supported Windows C++ compiler found. Install/use an existing MSVC, clang-cl, or MinGW toolchain; do not auto-download.' }
Get-FileHash $out -Algorithm SHA256
