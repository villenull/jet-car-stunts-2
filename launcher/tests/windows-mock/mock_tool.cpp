#include <filesystem>
#include <fstream>
#include <string>
#include <vector>
#include <sstream>
#include <iostream>
#include <thread>
#include <chrono>
#include <windows.h>
namespace fs = std::filesystem;

static fs::path self() { wchar_t b[32768]; DWORD n=GetModuleFileNameW(nullptr,b,32768); return fs::path(std::wstring(b,n)); }
static fs::path root() { return self().parent_path().parent_path().parent_path(); }
static std::string utf8(const std::wstring &s) {
  if (s.empty()) return {};
  int n = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, s.data(), (int)s.size(), nullptr, 0, nullptr, nullptr);
  if (n <= 0) return {};
  std::string o(n, '\0'); WideCharToMultiByte(CP_UTF8, 0, s.data(), (int)s.size(), o.data(), n, nullptr, nullptr); return o;
}
static void append(const fs::path& p, const std::wstring& s) { std::ofstream f(p, std::ios::binary | std::ios::app); auto b=utf8(s); f.write(b.data(), (std::streamsize)b.size()); f.put('\n'); }
static std::wstring join(int ac, wchar_t** av) { std::wstring s; for(int i=1;i<ac;i++){ if(i>1)s+=L" | "; s+=av[i]; } return s; }
static bool has(int ac, wchar_t** av, const wchar_t* x) { for(int i=1;i<ac;i++) if(!wcscmp(av[i],x)) return true; return false; }
static int adb(int ac, wchar_t** av) {
  auto d=root(); auto trace=d/L"trace.txt"; append(trace,L"adb " + join(ac,av));
  auto state=d/L"root.state";
  if (has(ac,av,L"nodaemon")) { while (true) Sleep(1000); }
  if (has(ac,av,L"root")) { std::wofstream f(state); return 0; }
  if (has(ac,av,L"unroot")) { std::error_code e; fs::remove(state,e); return 0; }
  if (has(ac,av,L"devices")) { std::wcout << L"List of devices attached\nemulator-5596\tdevice\n"; return 0; }
  if (has(ac,av,L"push")) { std::ofstream(d/L"bootstrap-pushed"); return 0; }
  bool shell=false; for(int i=1;i<ac;i++) shell |= !wcscmp(av[i],L"shell");
  if (!shell) {
    if (has(ac,av,L"install-multiple")) { if(fs::exists(d/L"fail_install")) return 41; std::wofstream(d/L"installed.state"); return 0; }
    if (has(ac,av,L"install")) { std::wofstream(d/L"helper.state"); return 0; }
    return 0;
  }
  if (has(ac,av,L"getprop")) {
    std::wcout << L"1\n";
    if (fs::exists(d / L"long_stdout"))
      for (int i = 0; i < 20000; ++i) std::wcout << L"long-output-" << i << L"\n";
    return 0;
  }
  if (has(ac,av,L"id") && has(ac,av,L"-u")) { std::wcout << (fs::exists(state)?L"0\n":L"2000\n"); return 0; }
  if (has(ac,av,L"id")) { std::wcout << (fs::exists(state)?L"uid=0(root) gid=0(root)\n":L"uid=2000(shell) gid=2000(shell)\n"); return 0; }
  // Match the persistent helper transport used by controller/helper.go.  The
  // real Go bridge expects one READY line followed by ACK for every command;
  // keep this child alive until the bridge sends its unacknowledged quit.
  if (has(ac,av,L"app_process")) {
    if (fs::exists(d / L"early_child_fail")) return 44;
    std::wcout << (fs::exists(d / L"invalid_ready") ? L"READY WRONG 3\n" : L"READY JCS2_UHID 3\n") << std::flush;
    std::wstring line;
    while (std::getline(std::wcin, line)) {
      if (line == L"quit") break;
      if (line.rfind(L"key ", 0) == 0 || line.rfind(L"axis ", 0) == 0 || line == L"neutral") {
        std::wcout << (fs::exists(d / L"helper_error") ? L"ERR\n" : L"ACK\n") << std::flush;
      } else {
        std::wcerr << L"mock helper protocol error: " << line << L"\n";
        return 43;
      }
    }
    return 0;
  }
  if (has(ac,av,L"dumpsys")) {
    std::wstring p; for (int i=1;i<ac-1;i++) if (!wcscmp(av[i],L"package")) p=av[i+1];
    std::wcout << L"Packages:\n  userId=" << (p==L"com.trueaxis.jetcarstunts2" ? L"10123" : L"10124") << L"\n"; return 0;
  }
  if (has(ac,av,L"ip") && has(ac,av,L"-o")) {
    bool up=has(ac,av,L"up"); if(up) std::wcout << L"1: lo: <LOOPBACK,UP> mtu 65536\n";
    else std::wcout << L"1: lo: <LOOPBACK,UP> mtu 65536\n2: eth0: <BROADCAST,UP> mtu 1500\n";
    return 0;
  }
  if (has(ac,av,L"route")) return 0;
  if (has(ac,av,L"path")) { std::wcout << L"package:/data/app/com.trueaxis.jetcarstunts2/base.apk\n"; return 0; }
  if (has(ac,av,L"tar") || has(ac,av,L"chown") || has(ac,av,L"restorecon") || has(ac,av,L"test") || has(ac,av,L"rm")) { append(d/L"trace.txt", L"bootstrap-op " + join(ac,av)); return has(ac,av,L"tar") && fs::exists(d/L"bootstrap_fail") ? 44 : 0; }
  bool game_launch = false;
  for (int i = 1; i < ac; ++i) game_launch |= !wcscmp(av[i], L"com.trueaxis.jetcarstunts2");
  if (has(ac,av,L"monkey") && game_launch) {
    // Signal the synthetic emulator after the launch command has reached the
    // guest; this models a user closing the owned guest and lets teardown be
    // covered without an unbounded test wait.
    std::wofstream(d/L"stop") << L"done\n";
    return 0;
  }
  if (has(ac,av,L"settings") || has(ac,av,L"svc") || has(ac,av,L"am")) return 0;
  return 0;
}
static int emu(int ac, wchar_t** av) {
  auto d=root(); append(d/L"trace.txt",L"emulator " + join(ac,av));
  if (fs::exists(d/L"emu_fail")) return 42;
  std::wofstream ready(d/L"emu.ready"); ready << L"ready\n";
  // The integration harness has no real guest window to close.  Once the
  // launcher has completed its install transaction, let the owned emulator
  // exit so the launcher can exercise normal teardown instead of hanging the
  // test process forever.
  for (int n = 0; !fs::exists(d/L"stop"); ++n) {
    if (n > 100 && fs::exists(d/L"installed.state")) return 0;
    Sleep(100);
  }
  return 0;
}
int wmain(int ac, wchar_t** av) { auto n=self().filename().wstring(); return n.find(L"adb")!=std::wstring::npos ? adb(ac,av) : emu(ac,av); }
