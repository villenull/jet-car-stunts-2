#pragma once
#include <filesystem>
#include <map>
#include <sstream>
#include <string>
#include <vector>
namespace jcs2 {
namespace fs = std::filesystem;
inline std::wstring trim(std::wstring s) {
  auto a = s.find_first_not_of(L" \t\r\n"), b = s.find_last_not_of(L" \t\r\n");
  return a == std::wstring::npos ? L"" : s.substr(a, b - a + 1);
}
struct Config {
  std::map<std::wstring, std::wstring> v;
};
inline Config parse_ini(const std::wstring &t) {
  Config c;
  std::wistringstream in(t);
  std::wstring l;
  while (std::getline(in, l)) {
    l = trim(l);
    if (l.empty() || l[0] == L'#' || l[0] == L';' || l[0] == L'[')
      continue;
    auto n = l.find(L'=');
    if (n != std::wstring::npos)
      c.v[trim(l.substr(0, n))] = trim(l.substr(n + 1));
  }
  return c;
}
inline std::wstring get(const Config &c, const wchar_t *k,
                        const wchar_t *d = L"") {
  auto i = c.v.find(k);
  return i == c.v.end() ? d : i->second;
}
inline std::wstring quote_arg(const std::wstring &s) {
  std::wstring o = L"\"";
  size_t bs = 0;
  for (wchar_t x : s) {
    if (x == L'\\') {
      ++bs;
      continue;
    }
    if (x == L'\"') {
      o.append(bs * 2 + 1, L'\\');
      o += x;
      bs = 0;
      continue;
    }
    o.append(bs, L'\\');
    bs = 0;
    o += x;
  }
  o.append(bs * 2, L'\\');
  return o + L"\"";
}
inline fs::path resolve(const fs::path &b, fs::path p) {
  return p.is_absolute() ? p : b / p;
}
// The staged Windows runtime contains the emulator and image, but not a JRE or
// SDK manager. Keep the AVD files generated here. Keep the image path absolute because the emulator
// resolves image.sysdir relative to ANDROID_SDK_ROOT, not ANDROID_AVD_HOME.
inline std::wstring avd_config(const fs::path &image, const std::wstring &name) {
  auto p = image.lexically_normal().wstring();
  std::wstring o = L"AvdId = " + name + L"\n"
                   L"PlayStore.enabled = false\n"
                   L"abi.type = x86\n"
                   L"avd.ini.displayname = JCS2 API28 Google APIs x86\n"
                   L"avd.ini.encoding = UTF-8\n"
                   L"fastboot.forceColdBoot = yes\n"
                   L"fastboot.forceFastBoot = no\n"
                   L"hw.accelerometer = yes\n"
                   L"hw.audioInput = no\n"
                   L"hw.audioOutput = no\n"
                   L"hw.camera.back = none\n"
                   L"hw.camera.front = none\n"
                   L"hw.cpu.arch = x86\n"
                   L"hw.cpu.ncore = 2\n"
                   L"hw.dPad = no\n"
                   L"hw.gps = no\n"
                   L"hw.gpu.enabled = yes\n"
                   L"hw.gpu.mode = swiftshader_indirect\n"
                   L"hw.initialOrientation = portrait\n"
                   L"hw.keyboard = yes\n"
                   L"hw.lcd.density = 420\n"
                   L"hw.lcd.height = 1280\n"
                   L"hw.lcd.width = 800\n"
                   L"hw.mainKeys = no\n"
                   L"hw.ramSize = 1536\n"
                   L"hw.sdCard = no\n"
                   L"hw.sensors.orientation = no\n"
                   L"hw.sensors.proximity = no\n"
                   L"image.sysdir.1 = " + p + L"\n"
                   L"image.sysdir.2 = \n"
                   L"network.latency = none\n"
                   L"network.speed = full\n"
                   L"runtime.network.latency = none\n"
                   L"runtime.network.speed = full\n"
                   L"showDeviceFrame = no\n"
                   L"tag.display = Google APIs\n"
                   L"tag.id = google_apis\n"
                   L"vm.heapSize = 256\n"
                   L"disk.dataPartition.size = 6442450944\n";
  return o;
}
inline std::wstring avd_pointer(const fs::path &guest) {
  return L"path=" + guest.lexically_normal().wstring() + L"\n";
}
inline bool exact_boot_completed(const std::wstring &o) {
  std::wistringstream in(o);
  std::wstring l;
  while (std::getline(in, l))
    if (trim(l) == L"1")
      return true;
  return false;
}
inline bool valid_port(int p) { return p >= 1024 && p <= 65535; }
inline bool valid_emulator_port(int p) {
  return p >= 5554 && p <= 65532 && !(p & 1);
}
inline bool valid_port_pair(int adb, int emulator) {
  return valid_port(adb) && valid_emulator_port(emulator) && emulator < 65535 &&
         adb != emulator && adb != emulator + 1;
}
inline std::wstring serial_for(int p) {
  return L"emulator-" + std::to_wstring(p);
}
inline std::wstring join_hashes(const std::vector<std::wstring> &h) {
  std::wstring o;
  for (auto &x : h)
    o += x + L'\n';
  return o;
}
// A bootstrap marker is an immutable transaction identity.  An absent marker
// is valid only while claiming a genuinely new owned AVD; an existing AVD may
// retry exactly the pending identity, but can never infer that absence means
// first start.
inline bool bootstrap_marker_compatible(const std::wstring &prior,
                                        const std::wstring &identity,
                                        bool fresh_owned) {
  return prior == identity || prior == L"pending\n" + identity ||
         (prior.empty() && fresh_owned);
}
inline bool bootstrap_marker_completed(const std::wstring &prior,
                                       const std::wstring &identity) {
  return prior == identity;
}
} // namespace jcs2
