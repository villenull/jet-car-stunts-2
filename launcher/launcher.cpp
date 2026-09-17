#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#ifdef _WIN32
#include <winsock2.h>
#include <windows.h>
#include <bcrypt.h>
#include "logic.hpp"
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <vector>
#include <atomic>
#include <array>
#include <unordered_set>
#include <cstring>
#include <iomanip>
#include <random>
#if defined(_MSC_VER)
#pragma comment(lib, "bcrypt.lib")
#pragma comment(lib, "ws2_32.lib")
#endif
namespace fs = std::filesystem;
using namespace jcs2;
struct Handle {
  HANDLE h = nullptr;
  Handle() = default;
  explicit Handle(HANDLE x) : h(x) {}
  ~Handle() {
    if (h && h != INVALID_HANDLE_VALUE)
      CloseHandle(h);
  }
  Handle(const Handle &) = delete;
  Handle &operator=(const Handle &) = delete;
  Handle(Handle &&x) noexcept : h(x.h) { x.h = nullptr; }
  Handle &operator=(Handle &&x) noexcept {
    if (this != &x) {
      if (h && h != INVALID_HANDLE_VALUE) CloseHandle(h);
      h = x.h;
      x.h = nullptr;
    }
    return *this;
  }
  operator HANDLE() const { return h; }
  explicit operator bool() const { return h && h != INVALID_HANDLE_VALUE; }
};
struct Outcome {
  DWORD code = 1;
  DWORD pid = 0;
  bool started = false, timeout = false;
  std::wstring out;
  Handle process;
};
struct Job {
  Handle h;
  Job() : h(CreateJobObjectW(nullptr, nullptr)) {
    if (!h) {
      std::wcerr << L"job:create failed win32=" << GetLastError() << L"\n";
    } else {
      JOBOBJECT_EXTENDED_LIMIT_INFORMATION x{};
      x.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
      if (!SetInformationJobObject(h, JobObjectExtendedLimitInformation, &x,
                                   sizeof x)) {
        std::wcerr << L"job:set-kill-on-close failed win32=" << GetLastError() << L"\n";
        h = Handle();
      }
    }
  }
};
static fs::path exedir() {
  wchar_t b[32768];
  DWORD n = GetModuleFileNameW(nullptr, b, 32768);
  return fs::path(std::wstring(b, n)).parent_path();
}
static std::wstring decode(const char *b, size_t n) {
  int z =
      MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, b, (int)n, nullptr, 0);
  if (z <= 0)
    return L"";
  std::wstring o(z, L'\0');
  MultiByteToWideChar(CP_UTF8, 0, b, (int)n, o.data(), z);
  return o;
}
static bool read_utf8(const fs::path &p, std::wstring &out) {
  std::ifstream f(p, std::ios::binary);
  if (!f) return false;
  std::string b((std::istreambuf_iterator<char>(f)), {});
  out = decode(b.data(), b.size());
  return b.empty() || !out.empty();
}
static std::string encode_utf8(const std::wstring &s) {
  if (s.empty()) return {};
  int n = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, s.data(),
                              (int)s.size(), nullptr, 0, nullptr, nullptr);
  if (n <= 0) return {};
  std::string out(n, '\0');
  WideCharToMultiByte(CP_UTF8, 0, s.data(), (int)s.size(), out.data(), n,
                      nullptr, nullptr);
  return out;
}
static volatile LONG g_ctrlc = 0;
static BOOL WINAPI ctrlc(DWORD type) {
  if (type == CTRL_C_EVENT || type == CTRL_BREAK_EVENT ||
      type == CTRL_CLOSE_EVENT || type == CTRL_LOGOFF_EVENT ||
      type == CTRL_SHUTDOWN_EVENT) {
    InterlockedExchange(&g_ctrlc, 1);
    return TRUE;
  }
  return FALSE;
}
static Outcome run(const fs::path &e, const std::vector<std::wstring> &a,
                   DWORD ms, Job *job = nullptr,
                   const fs::path &longLog = {}) {
  Outcome o;
  std::wstring cmd = quote_arg(e.wstring());
  for (auto &x : a)
    cmd += L" " + quote_arg(x);
  std::vector<wchar_t> buf(cmd.begin(), cmd.end());
  buf.push_back(0);
  Handle rd, wr, log;
  SECURITY_ATTRIBUTES sa{sizeof sa, nullptr, TRUE};
  if (ms) {
    if (!CreatePipe(&rd.h, &wr.h, &sa, 0)) {
      std::wcerr << L"run:create-pipe failed win32=" << GetLastError() << L"\n";
      return o;
    }
    SetHandleInformation(rd.h, HANDLE_FLAG_INHERIT, 0);
  } else {
    fs::path lp = longLog.empty() ? (exedir() / L"jcs2-emulator.log") : longLog;
    fs::create_directories(lp.parent_path());
    log = Handle(CreateFileW(lp.c_str(), GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                             &sa, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr));
    if (!log) {
      std::wcerr << L"run:create-log failed path=" << lp.wstring()
                 << L" win32=" << GetLastError() << L"\n";
      return o;
    }
    wr = Handle(log.h);
    log.h = nullptr;
  }
  STARTUPINFOW si{};
  si.cb = sizeof si;
  si.dwFlags = STARTF_USESTDHANDLES;
  si.hStdOutput = wr.h;
  si.hStdError = wr.h;
  PROCESS_INFORMATION pi{};
  if (!CreateProcessW(e.c_str(), buf.data(), nullptr, nullptr, TRUE,
                      CREATE_NO_WINDOW | CREATE_SUSPENDED, nullptr, nullptr,
                      &si, &pi)) {
    std::wcerr << L"run:create-process failed exe=" << e.wstring()
               << L" win32=" << GetLastError() << L"\n";
    return o;
  }
  Handle p(pi.hProcess), t(pi.hThread);
  wr = Handle();
  if (job && (!AssignProcessToJobObject(job->h, p))) {
    std::wcerr << L"run:assign-job failed exe=" << e.wstring()
               << L" win32=" << GetLastError() << L"\n";
    TerminateProcess(p, 125);
    return o;
  }
  if (ResumeThread(t) == (DWORD)-1) {
    std::wcerr << L"run:resume failed exe=" << e.wstring()
               << L" win32=" << GetLastError() << L"\n";
    TerminateProcess(p, 125);
    return o;
  }
  o.started = true;
  o.pid = pi.dwProcessId;
  if (!ms) {
    // A zero-timeout run means "started asynchronously".  STILL_ACTIVE is
    // not a success code and made the ADB-server stage reject every launch.
    // Keep the live process handle in the outcome for liveness/cleanup.
    o.code = 0;
    o.process = std::move(p);
    return o;
  }
  auto end = std::chrono::steady_clock::now() + std::chrono::milliseconds(ms);
  for (;;) {
    DWORD n = 0;
    if (PeekNamedPipe(rd, nullptr, 0, nullptr, &n, nullptr) && n) {
      std::vector<char> x(std::min<DWORD>(n, 4096));
      DWORD got = 0;
      if (ReadFile(rd, x.data(), (DWORD)x.size(), &got, nullptr) && got)
        o.out += decode(x.data(), got);
    }
    DWORD w = WaitForSingleObject(p, 20);
    if (w == WAIT_OBJECT_0)
      break;
    if (ms && std::chrono::steady_clock::now() >= end) {
      o.timeout = true;
      TerminateProcess(p, 124);
      WaitForSingleObject(p, 2000);
      break;
    }
  }
  for (int i = 0; i < 5; i++) {
    DWORD n = 0;
    if (!PeekNamedPipe(rd, nullptr, 0, nullptr, &n, nullptr) || !n)
      break;
    std::vector<char> x(std::min<DWORD>(n, 4096));
    DWORD got = 0;
    if (!ReadFile(rd, x.data(), (DWORD)x.size(), &got, nullptr) || !got)
      break;
    o.out += decode(x.data(), got);
  }
  if (!GetExitCodeProcess(p, &o.code)) {
    std::wcerr << L"run:get-exit-code failed exe=" << e.wstring()
               << L" win32=" << GetLastError() << L"\n";
    o.code = 1;
  }
  return o;
}
static bool freeport(int p) {
  SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (s == INVALID_SOCKET)
    return false;
  sockaddr_in a{};
  a.sin_family = AF_INET;
  a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  a.sin_port = htons((u_short)p);
  BOOL exclusive = TRUE;
  setsockopt(s, SOL_SOCKET, SO_EXCLUSIVEADDRUSE,
             reinterpret_cast<const char *>(&exclusive), sizeof exclusive);
  bool ok = bind(s, (sockaddr *)&a, sizeof a) == 0;
  closesocket(s);
  return ok;
}
static bool hashfile(const fs::path &p, std::wstring &o) {
  Handle f(CreateFileW(p.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
                       OPEN_EXISTING, 0, nullptr));
  if (!f)
    return false;
  BCRYPT_ALG_HANDLE a = nullptr;
  BCRYPT_HASH_HANDLE h = nullptr;
  DWORD n = 0, len = 0;
  if (BCryptOpenAlgorithmProvider(&a, BCRYPT_SHA256_ALGORITHM, nullptr, 0) ||
      BCryptGetProperty(a, BCRYPT_OBJECT_LENGTH, (PUCHAR)&len, sizeof len, &n,
                        0)) {
    if (a) BCryptCloseAlgorithmProvider(a, 0);
    return false;
  }
  std::vector<UCHAR> obj(len), d(32);
  bool ok = BCryptCreateHash(a, &h, obj.data(), len, nullptr, 0, 0) == 0;
  BYTE b[65536];
  while (ok) {
    BOOL read = ReadFile(f, b, sizeof b, &n, nullptr);
    if (!read) { ok = false; break; }
    if (!n) break;
    ok = BCryptHashData(h, b, n, 0) == 0;
  }
  if (ok)
    ok = BCryptFinishHash(h, d.data(), 32, 0) == 0;
  for (auto x : d) {
    wchar_t z[3];
    swprintf(z, 3, L"%02x", x);
    o += z;
  }
  if (h)
    BCryptDestroyHash(h);
  BCryptCloseAlgorithmProvider(a, 0);
  return ok;
}
static bool marker(const fs::path &p, const std::wstring &s) {
  std::ofstream f(p, std::ios::binary);
  auto b = encode_utf8(s);
  f.write(b.data(), b.size());
  return (bool)f;
}
static bool readmarker(const fs::path &p, std::wstring &s) {
  return read_utf8(p, s);
}
static bool write_text(const fs::path &p, const std::wstring &s) {
  fs::create_directories(p.parent_path());
  return marker(p, s);
}
static bool json_string_field(const std::wstring &s, const wchar_t *key,
                              std::wstring &out) {
  std::wstring needle = L"\"" + std::wstring(key) + L"\"";
  auto p = s.find(needle); if (p == std::wstring::npos) return false;
  p = s.find(L':', p + needle.size()); if (p == std::wstring::npos) return false;
  p = s.find(L'\"', p + 1); if (p == std::wstring::npos) return false;
  auto e = s.find(L'\"', p + 1); if (e == std::wstring::npos) return false;
  out = s.substr(p + 1, e - p - 1); return true;
}
static bool json_device_id(const std::wstring &s, long long &out) {
  std::wstring needle = L"\"device_id\"";
  auto p = s.find(needle); if (p == std::wstring::npos) return false;
  p = s.find(L':', p + needle.size()); if (p == std::wstring::npos) return false;
  ++p; while (p < s.size() && iswspace(s[p])) ++p;
  auto e = p; while (e < s.size() && s[e] >= L'0' && s[e] <= L'9') ++e;
  if (e == p) return false;
  try { out = std::stoll(s.substr(p, e - p)); return out >= 0; }
  catch (...) { return false; }
}
static bool valid_controller_ready(const fs::path &p, const std::wstring &serial) {
  std::wstring s; if (!readmarker(p, s)) return false;
  std::wstring got, transport; long long device = -1;
  return json_string_field(s, L"serial", got) && got == serial &&
         json_string_field(s, L"transport", transport) && transport == L"uhid" &&
         json_device_id(s, device);
}
struct TarEntry { std::string path; unsigned long long size = 0; unsigned mode = 0; bool dir = false; };
static bool octal(const char *p, size_t n, unsigned long long &v) {
  v = 0; bool any = false;
  for (size_t i = 0; i < n && p[i]; ++i) {
    if (p[i] == ' ' || p[i] == '\0') continue;
    if (p[i] < '0' || p[i] > '7') return false;
    any = true; v = (v << 3) + unsigned(p[i] - '0');
  }
  return any;
}
static bool validate_tar(const fs::path &p, const std::wstring &game,
                         const std::wstring &helper, size_t &entries,
                         std::wstring &why) {
  std::ifstream f(p, std::ios::binary); if (!f) { why = L"open"; return false; }
  auto allowed = [&](const std::string &x) {
    auto slash = x.find('/'); if (slash == std::string::npos) return false;
    std::string a = x.substr(0, slash);
    std::string g = encode_utf8(game), h = encode_utf8(helper);
    return (a == g || a == h) && (x.size() > slash + 1 || x == a + "/");
  };
  std::array<char, 512> h{}; entries = 0; unsigned files = 0;
  std::unordered_set<std::string> seen;
  for (;;) {
    f.read(h.data(), h.size()); if (f.gcount() != (std::streamsize)h.size()) { why = L"short-header"; return false; }
    bool zero = true; for (char c : h) zero &= c == 0;
    if (zero) {
      // A tar stream terminates with two complete zero blocks.  Requiring the
      // second block prevents a truncated archive from being accepted.
      f.read(h.data(), h.size());
      if (f.gcount() != (std::streamsize)h.size()) { why = L"short-end"; return false; }
      for (char c : h) if (c != 0) { why = L"bad-end"; return false; }
      break;
    }
    unsigned long long stored = 0;
    if (!octal(h.data() + 148, 8, stored)) { why = L"bad-checksum"; return false; }
    unsigned long long sum = 0;
    for (size_t i = 0; i < h.size(); ++i)
      sum += (i >= 148 && i < 156) ? (unsigned char)' ' : (unsigned char)h[i];
    if (stored != sum) { why = L"bad-checksum"; return false; }
    char type = h[156]; if (type && type != '0' && type != '5') { why = L"unsupported-type"; return false; }
    unsigned long long sz = 0, mode = 0;
    unsigned long long uid = 0, gid = 0;
    if (!octal(h.data() + 100, 8, mode) || !octal(h.data() + 108, 8, uid) ||
        !octal(h.data() + 116, 8, gid) || !octal(h.data() + 124, 12, sz)) { why = L"bad-header"; return false; }
    if (uid < 10000 || uid > 19999) { why = L"unsafe-owner"; return false; }
    std::string name(h.data(), strnlen(h.data(), 100));
    std::string pref(h.data() + 345, strnlen(h.data() + 345, 155));
    if (!pref.empty()) name = pref + "/" + name;
    if (name.empty() || name[0] == '/' || name.find('\\') != std::string::npos || name.find("../") != std::string::npos || name.find("/..") != std::string::npos || !allowed(name)) { why = L"path-not-allowlisted"; return false; }
    if (!seen.insert(name).second) { why = L"duplicate-path"; return false; }
    ++entries; if (entries > 256) { why = L"too-many-entries"; return false; }
    if (type != '5') { ++files; if (sz > 8 * 1024 * 1024) { why = L"file-too-large"; return false; } }
    auto blocks = (sz + 511) / 512; f.seekg((std::streamoff)(blocks * 512), std::ios::cur);
    if (!f) { why = L"short-data"; return false; }
  }
  if (files == 0) { why = L"entry-count"; return false; }
  return true;
}
int wmain(int ac, wchar_t **av) {
  fs::path root = exedir();
  std::wstring ini;
  if (!read_utf8(root / L"launcher.ini", ini)) {
    std::wcerr << L"missing launcher.ini\n";
    return 2;
  }
  Config c = parse_ini(ini);
  bool dry = false;
  for (int i = 1; i < ac; i++)
    dry |= !wcscmp(av[i], L"--dry-run");
  fs::path sdkroot = resolve(root, get(c, L"sdk_root", L"sdk"));
  fs::path adb = resolve(root, get(c, L"adb", L"sdk\\platform-tools\\adb.exe")),
           emu = resolve(root, get(c, L"emulator", L"sdk\\emulator\\emulator.exe")),
           avdh = resolve(root, get(c, L"avd_dir"));
  int ap = 0, ep = 0;
  try {
    ap = std::stoi(get(c, L"adb_port", L"5040"));
    ep = std::stoi(get(c, L"emulator_port", L"5596"));
  } catch (...) {
    return 2;
  }
  if (!valid_port_pair(ap, ep))
    return 2;
  std::wstring serial = serial_for(ep),
               pkg = get(c, L"package", L"com.trueaxis.jetcarstunts2"),
               avd = get(c, L"avd_name", L"JCS2-personal");
  auto bootstrapName = get(c, L"bootstrap_archive");
  fs::path bootstrap = bootstrapName.empty() ? fs::path{} : resolve(root, bootstrapName);
  auto bootstrapHash = get(c, L"bootstrap_sha256");
  auto helper = get(c, L"helper_apk"), helperPkg = get(c, L"helper_package");
  if (!helper.empty() != !helperPkg.empty()) { std::wcerr << L"stage=config helper_apk/helper_package must be paired\n"; return 2; }
  auto controllerExeName = get(c, L"controller_exe");
  auto controllerJarName = get(c, L"controller_helper_jar");
  auto controllerMappingName = get(c, L"controller_mapping");
  bool controllerConfigured = !controllerExeName.empty() || !controllerJarName.empty() || !controllerMappingName.empty();
  if (controllerConfigured && (controllerExeName.empty() || controllerJarName.empty() || controllerMappingName.empty())) {
    std::wcerr << L"stage=config controller_exe/controller_helper_jar/controller_mapping must be paired\n";
    return 2;
  }
  fs::path controllerExe, controllerJar, controllerMapping;
  if (controllerConfigured) {
    controllerExe = resolve(root, controllerExeName);
    controllerJar = resolve(root, controllerJarName);
    controllerMapping = resolve(root, controllerMappingName);
    if (!fs::is_regular_file(controllerExe) || !fs::is_regular_file(controllerJar) ||
        !fs::is_regular_file(controllerMapping)) {
      std::wcerr << L"stage=config controller files missing\n"; return 2;
    }
  }
  if (!bootstrap.empty() && (helper.empty() || helperPkg.empty())) { std::wcerr << L"stage=config bootstrap requires helper_apk/helper_package\n"; return 2; }
  std::vector<fs::path> apk;
  for (int i = 1;; i++) {
    auto k = L"apk" + std::to_wstring(i), v = get(c, k.c_str());
    if (v.empty())
      break;
    apk.push_back(resolve(root, v));
  }
  if (apk.size() != 5)
    return 2;
  if (!fs::is_regular_file(adb) || (!dry && !fs::is_regular_file(emu)))
    return 2;
  for (auto &p : apk)
    if (!fs::is_regular_file(p))
      return 2;
  // Compute the complete identity before starting the guest or installing an
  // APK.  Bootstrap compatibility is a fail-closed precondition for all data
  // mutation, not a check performed after installation.
  std::wstring state = pkg + L"\n";
  for (auto &p : apk) {
    std::wstring h;
    if (!hashfile(p, h)) return 2;
    state += h + L"\n";
  }
  std::wstring helperHash;
  if (!helper.empty() && !hashfile(resolve(root, helper), helperHash)) {
    std::wcerr << L"stage=bootstrap helper hash failed\n"; return 2;
  }
  std::wstring bootState;
  if (!bootstrap.empty()) {
    if (!fs::is_regular_file(bootstrap) || bootstrapHash.size() != 64) {
      std::wcerr << L"stage=config bootstrap archive/hash missing or invalid\n"; return 2;
    }
    std::wstring actual; if (!hashfile(bootstrap, actual) || actual != bootstrapHash) {
      std::wcerr << L"stage=bootstrap archive sha256 mismatch\n"; return 2;
    }
    size_t n = 0; std::wstring why;
    if (!validate_tar(bootstrap, pkg, helperPkg, n, why)) {
      std::wcerr << L"stage=bootstrap archive validation failed reason=" << why << L"\n"; return 2;
    }
    bootState = bootstrapHash + L"\n" + state + L"\n" + helperPkg + L"\n" + helperHash + L"\n";
  }
  if (dry) {
    std::wcout << L"root=" << root << L"\nemulator=" << emu << L"\nadb=" << adb
               << L"\navd_home=" << avdh << L"\nserial=" << serial
               << L" adb_port=" << ap
               << L"\nsdk_root=" << sdkroot
               << L"\nflags=-memory 1536 -gpu swiftshader_indirect -accel auto "
                  L"-qemu -m 1536 -net none\n";
    for (auto &p : apk)
      std::wcout << L"apk=" << p << L"\n";
    return 0;
  }
  if (!fs::path(avdh).is_absolute())
    return 2;
  WSADATA ws{};
  if (WSAStartup(MAKEWORD(2, 2), &ws))
    return 2;
  if (!freeport(ap) || !freeport(ep) || !freeport(ep + 1)) {
    WSACleanup();
    std::wcerr << L"port busy; no mutation performed\n";
    return 3;
  }
  SetEnvironmentVariableW(L"ANDROID_ADB_SERVER_PORT",
                          std::to_wstring(ap).c_str());
  SetEnvironmentVariableW(L"ANDROID_AVD_HOME", avdh.c_str());
  SetEnvironmentVariableW(L"ANDROID_SDK_ROOT", sdkroot.c_str());
  SetEnvironmentVariableW(L"ANDROID_HOME", sdkroot.c_str());
  SetConsoleCtrlHandler(ctrlc, TRUE);
  Job job;
  bool normalControllerCleanup = false;
  fs::path controllerReady;
  auto stop = [&]() {
    if (job.h) TerminateJobObject(job.h, 0);
    if (normalControllerCleanup && !controllerReady.empty()) {
      std::error_code ignored; fs::remove(controllerReady, ignored);
    }
    SetConsoleCtrlHandler(ctrlc, FALSE);
    WSACleanup();
  };
  if (!job.h) {
    std::wcerr << L"stage=job-setup failed; owned child cleanup unavailable\n";
    stop(); return 4;
  }
  // ANDROID_AVD_HOME is the directory containing the launcher-owned pointer
  // and guest directory, never the guest directory itself.
  fs::path guest = avdh / (avd + L".avd");
  fs::path pointer = avdh / (avd + L".ini");
  fs::path own = guest / L".jcs2-owned";
  std::wstring ownText;
  bool freshOwned = false;
  if (readmarker(own, ownText)) {
    if (ownText != avd + L"\n") { stop(); return 4; }
  } else {
    // Only an empty, fresh state root may be claimed.  Existing state with a
    // marker is left untouched so user progress and userdata survive relaunch.
    if (fs::exists(avdh) && !fs::is_empty(avdh)) { stop(); return 4; }
    freshOwned = true;
    fs::create_directories(guest);
    fs::path image = sdkroot / L"system-images" / L"android-28" /
                     L"google_apis" / L"x86";
    if (!fs::is_directory(image) || !fs::is_regular_file(image / L"source.properties")) {
      stop(); return 4;
    }
    if (!write_text(guest / L"config.ini", avd_config(fs::absolute(image), avd)) ||
        !write_text(pointer, avd_pointer(fs::absolute(guest))) ||
        !write_text(own, avd + L"\n")) {
      stop(); return 4;
    }
  }
  if (!fs::is_regular_file(guest / L"config.ini") ||
      !fs::is_regular_file(pointer)) { stop(); return 4; }
  fs::path bst = avdh / L".jcs2-bootstrap-state";
  std::wstring prior;
  bool bootstrap_complete = false;
  if (!bootstrap.empty()) {
    bool hasPrior = readmarker(bst, prior);
    const std::wstring pending = L"pending\n" + bootState;
    if ((hasPrior && prior.empty()) ||
        (hasPrior && !bootstrap_marker_compatible(prior, bootState, freshOwned))) {
      std::wcerr << L"stage=bootstrap marker mismatch; refusing migration\n";
      stop(); return 13;
    }
    if (!hasPrior && !bootstrap_marker_compatible(prior, bootState, freshOwned)) {
      std::wcerr << L"stage=bootstrap marker missing on existing owned state; refusing restore\n";
      stop(); return 13;
    }
    bootstrap_complete = bootstrap_marker_completed(prior, bootState);
    // This immutable identity is written while the AVD is genuinely new,
    // before first boot/install.  A pending marker is the only proof that a
    // later retry may restore into that state.
    if (!bootstrap_complete && !hasPrior &&
        !marker(bst, pending)) {
      std::wcerr << L"stage=bootstrap pending marker write failed\n";
      stop(); return 13;
    }
  }
  // Keep the ADB server in our kill-on-close job.  No detached daemon and no
  // shared host ADB state are permitted.
  auto server = run(adb, {L"-P", std::to_wstring(ap), L"nodaemon", L"server"}, 0,
                    &job, root / L"jcs2-adb.log");
  if (!server.started || server.code != 0) {
    stop();
    return 4;
  }
  auto child =
      run(emu,
          {L"-avd", avd, L"-port", std::to_wstring(ep), L"-memory", L"1536",
           L"-gpu", L"swiftshader_indirect", L"-accel", L"auto",
           L"-no-snapshot-save", L"-no-boot-anim", L"-qemu", L"-m", L"1536",
           L"-net", L"none"},
          0, &job, root / L"jcs2-emulator.log");
  if (!child.started) {
    stop();
    return 5;
  }
  auto identity_ok = [&]() {
    if (!child.process || WaitForSingleObject(child.process, 0) != WAIT_TIMEOUT)
      return false;
    auto d = run(adb, {L"-P", std::to_wstring(ap), L"devices"}, 5000);
    std::wistringstream in(d.out); std::wstring line;
    while (std::getline(in, line)) {
      if (line.rfind(serial + L"\tdevice", 0) == 0) return true;
    }
    return false;
  };
  auto arun = [&](std::vector<std::wstring> x, DWORD t) {
    if (!identity_ok()) return Outcome{};
    x.insert(x.begin(), {L"-P", std::to_wstring(ap), L"-s", serial});
    return run(adb, x, t);
  };
  auto direct = [&](std::vector<std::wstring> x, DWORD t) {
    x.insert(x.begin(), {L"-P", std::to_wstring(ap), L"-s", serial});
    return run(adb, x, t);
  };
  auto wait_transport = [&](bool root) {
    for (int n = 0; n < 30; ++n) {
      if (InterlockedCompareExchange(&g_ctrlc, 0, 0)) return false;
      auto d = run(adb, {L"-P", std::to_wstring(ap), L"devices"}, 2000);
      std::wistringstream in(d.out); std::wstring line;
      while (std::getline(in, line)) {
        if (line.rfind(serial + L"\tdevice", 0) != 0) continue;
        auto id = direct({L"shell", L"id"}, 2000);
        bool isroot = id.code == 0 && id.out.find(L"uid=0") != std::wstring::npos;
        if (isroot == root) return true;
      }
      Sleep(500);
    }
    return false;
  };
  bool boot = false;
  for (int n = 0; n < 90 && !boot; n++) {
    if (InterlockedCompareExchange(&g_ctrlc, 0, 0)) { stop(); return 6; }
    // A process started asynchronously can fail before the first transport
    // poll.  Do not misreport that as a 90-second boot timeout.
    if (child.process && WaitForSingleObject(child.process, 0) != WAIT_TIMEOUT) {
      std::wcerr << L"stage=emulator-start process-exited-before-boot\n";
      stop(); return 5;
    }
    Sleep(1000);
    auto z = arun({L"shell", L"getprop", L"sys.boot_completed"}, 5000);
    boot = z.code == 0 && exact_boot_completed(z.out);
  }
  if (!boot) {
    stop();
    return 6;
  }
  // Network isolation is proven while adbd is root, then the transport is
  // reconnected and explicitly dropped back to shell uid 2000.
  auto rooted = direct({L"root"}, 10000);
  if (rooted.code != 0 || !wait_transport(true)) { stop(); return 7; }
  for (auto x : std::vector<std::vector<std::wstring>>{
           {L"shell", L"svc", L"wifi", L"disable"},
           {L"shell", L"svc", L"data", L"disable"},
           {L"shell", L"settings", L"put", L"global", L"airplane_mode_on", L"1"},
           {L"shell", L"am", L"broadcast", L"-a", L"android.intent.action.AIRPLANE_MODE",
            L"--ez", L"state", L"true"}})
    if (direct(x, 10000).code != 0) { stop(); return 7; }
  auto links = direct({L"shell", L"ip", L"-o", L"link", L"show"}, 10000);
  if (links.code != 0) { stop(); return 7; }
  std::wistringstream li(links.out); std::wstring ll;
  while (std::getline(li, ll)) {
    auto colon = ll.find(L':');
    if (colon == std::wstring::npos) continue;
    auto start = ll.find_first_not_of(L" \t", colon + 1);
    if (start == std::wstring::npos) continue;
    auto end = ll.find_first_of(L" :@\t", start);
    auto name = ll.substr(start, end == std::wstring::npos ? end : end - start);
    if (name.empty() || name == L"lo") continue;
    if (direct({L"shell", L"ip", L"link", L"set", L"dev", name, L"down"}, 5000).code != 0 ||
        direct({L"shell", L"ip", L"addr", L"flush", L"dev", name}, 5000).code != 0) {
      stop(); return 7;
    }
  }
  auto after = direct({L"shell", L"ip", L"route"}, 10000);
  auto live = direct({L"shell", L"ip", L"-o", L"link", L"show", L"up"}, 10000);
  bool external_up = false;
  std::wistringstream ui(live.out);
  while (std::getline(ui, ll)) {
    auto c0 = ll.find(L':');
    auto s0 = c0 == std::wstring::npos ? std::wstring::npos : ll.find_first_not_of(L" \t", c0 + 1);
    auto e0 = s0 == std::wstring::npos ? s0 : ll.find_first_of(L" :@\t", s0);
    if (s0 != std::wstring::npos && ll.substr(s0, e0 == std::wstring::npos ? e0 : e0 - s0) != L"lo")
      external_up = true;
  }
  if (after.code != 0 || !trim(after.out).empty() || live.code != 0 || external_up) {
    stop(); return 7;
  }
  auto bt = arun({L"shell", L"svc", L"bluetooth", L"disable"}, 10000);
  (void)bt; // Bluetooth is optional on Android 9 and must not block launch.
  if (direct({L"unroot"}, 10000).code != 0 || !wait_transport(false)) { stop(); return 7; }
  auto id = arun({L"shell", L"id"}, 10000);
  if (id.code != 0 || trim(id.out).find(L"uid=2000") != 0 || id.out.find(L"uid=0") != std::wstring::npos) {
    stop();
    return 8;
  }
  Outcome controller;
  if (controllerConfigured) {
    // The ready path is deliberately unique to this process.  It is created
    // only after guest boot/network isolation, so a stale marker from another
    // launcher session can never satisfy this launch.
    fs::path sessions = avdh / L".jcs2-sessions";
    fs::create_directories(sessions);
    unsigned char nonce[16]{};
    if (BCryptGenRandom(nullptr, nonce, sizeof nonce, BCRYPT_USE_SYSTEM_PREFERRED_RNG) != 0) {
      std::wcerr << L"stage=controller nonce generation failed\n"; stop(); return 14;
    }
    std::wstringstream ns; ns << std::hex << GetCurrentProcessId();
    for (auto b : nonce) { ns.width(2); ns.fill(L'0'); ns << std::hex << (unsigned)b; }
    controllerReady = sessions / (L"ready-" + ns.str() + L".json");
    std::error_code ignored; fs::remove(controllerReady, ignored);
    fs::path controllerLog = sessions / (L"controller-" + ns.str() + L".log");
    controller = run(controllerExe,
                      {L"--helper-jar", controllerJar.wstring(), L"--adb-path", adb.wstring(),
                       L"--adb-port", std::to_wstring(ap), L"--adb-serial", serial,
                       L"--mapping", controllerMapping.wstring(), L"--ready-file", controllerReady.wstring()},
                      0, &job, controllerLog);
    if (!controller.started || !controller.process) {
      std::wcerr << L"stage=controller-start failed\n"; stop(); return 14;
    }
    bool ready = false;
    auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(30);
    while (std::chrono::steady_clock::now() < deadline) {
      if (InterlockedCompareExchange(&g_ctrlc, 0, 0) ||
          WaitForSingleObject(child.process, 0) != WAIT_TIMEOUT ||
          WaitForSingleObject(controller.process, 0) != WAIT_TIMEOUT) break;
      if (valid_controller_ready(controllerReady, serial) &&
          WaitForSingleObject(controller.process, 0) == WAIT_TIMEOUT) {
        ready = true; break;
      }
      Sleep(100);
    }
    if (!ready) {
      std::wcerr << L"stage=controller-ready timeout or invalid marker\n";
      stop(); return 14;
    }
  }
  std::wstring old;
  fs::path st = avdh / L".jcs2-install-state";
  bool ok = readmarker(st, old) && old == state;
  if (ok) {
    auto z = arun({L"shell", L"pm", L"path", pkg}, 10000);
    ok = z.code == 0 && z.out.find(L"package:") != std::wstring::npos;
  }
  if (!ok) {
    std::vector<std::wstring> x = {L"install-multiple", L"-r"};
    for (auto &p : apk)
      x.push_back(p.wstring());
    if (arun(x, 180000).code != 0) {
      stop();
      return 10;
    }
    if (!marker(st, state)) {
      stop();
      return 11;
    }
  }
  if (!helper.empty() && !helperPkg.empty()) {
    auto hp = resolve(root, helper);
    if (!fs::is_regular_file(hp) || arun({L"install", L"-r", hp.wstring()}, 120000).code != 0) {
      stop(); return 12;
    }
  }
  if (!bootstrap.empty()) {
    if (!bootstrap_complete) {
      if (arun({L"shell", L"am", L"force-stop", pkg}, 10000).code != 0 ||
          (!helperPkg.empty() && arun({L"shell", L"am", L"force-stop", helperPkg}, 10000).code != 0) ||
          direct({L"root"}, 10000).code != 0 || !wait_transport(true)) {
        std::wcerr << L"stage=bootstrap root transition failed\n"; stop(); return 13;
      }
      const std::wstring remote = L"/data/local/tmp/jcs2-synthetic-data.tar";
      if (arun({L"push", bootstrap.wstring(), remote}, 120000).code != 0 ||
          direct({L"shell", L"tar", L"-xf", remote, L"-C", L"/data/user/0"}, 120000).code != 0) {
        std::wcerr << L"stage=bootstrap archive restore failed\n"; stop(); return 13;
      }
      auto uid = [&](const std::wstring &p, std::wstring &u) {
        auto x = direct({L"shell", L"dumpsys", L"package", p}, 10000);
        auto n = x.out.find(L"userId="); if (x.code != 0 || n == std::wstring::npos) return false;
        n += 7; auto e = x.out.find_first_not_of(L"0123456789", n); u = x.out.substr(n, e - n);
        if (u.empty() || u == L"0") return false;
        try { auto v = std::stoull(u); return v >= 10000 && v <= 19999; }
        catch (...) { return false; }
      };
      std::wstring gu, hu;
      if (!uid(pkg, gu) || (!helperPkg.empty() && !uid(helperPkg, hu))) {
        std::wcerr << L"stage=bootstrap dynamic UID query failed\n"; stop(); return 13;
      }
      auto restore_root = [&](const std::wstring &p, const std::wstring &u) {
        return direct({L"shell", L"chown", L"-R", u + L":" + u, L"/data/user/0/" + p}, 120000).code == 0 &&
               direct({L"shell", L"restorecon", L"-RF", L"/data/user/0/" + p}, 120000).code == 0 &&
               direct({L"shell", L"test", L"-d", L"/data/user/0/" + p}, 10000).code == 0;
      };
      if (!restore_root(pkg, gu) || (!helperPkg.empty() && !restore_root(helperPkg, hu))) {
        std::wcerr << L"stage=bootstrap ownership/SELinux restore failed\n"; stop(); return 13;
      }
      if (direct({L"shell", L"rm", L"-f", remote}, 10000).code != 0 ||
          direct({L"unroot"}, 10000).code != 0 || !wait_transport(false)) {
        std::wcerr << L"stage=bootstrap unroot transition failed\n"; stop(); return 13;
      }
      auto post = arun({L"shell", L"id"}, 10000);
      if (post.code != 0 || trim(post.out).find(L"uid=2000") != 0) {
        std::wcerr << L"stage=bootstrap post-restore uid is not shell\n"; stop(); return 13;
      }
      if (!marker(bst, bootState)) { std::wcerr << L"stage=bootstrap completion marker write failed\n"; stop(); return 13; }
    }
  }
  if (!helperPkg.empty() && arun({L"shell", L"monkey", L"-p", helperPkg, L"1"}, 30000).code != 0) {
    std::wcerr << L"stage=helper launch failed\n"; stop(); return 12;
  }
  if (arun({L"shell", L"monkey", L"-p", pkg, L"1"}, 30000).code != 0) {
    stop();
    return 12;
  }
  std::wcout << L"JCS2 started on " << serial << L" with owned ADB port " << ap
             << L"\n";
  if (child.process) {
    for (;;) {
      DWORD w = WaitForSingleObject(child.process, 100);
      if (w != WAIT_TIMEOUT) break;
      if (controllerConfigured && WaitForSingleObject(controller.process, 0) != WAIT_TIMEOUT) {
        std::wcerr << L"stage=controller-exited-during-game\n";
        stop(); return 15;
      }
      if (InterlockedCompareExchange(&g_ctrlc, 0, 0)) {
        TerminateJobObject(job.h, 130);
        break;
      }
    }
  }
  normalControllerCleanup = true;
  stop();
  return 0;
}
#else
#include "logic.hpp"
int main() { return 0; }
#endif
