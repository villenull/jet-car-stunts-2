#include "../logic.hpp"
#include <cassert>
#include <iostream>
int main() {
  using namespace jcs2;
  auto c = parse_ini(L"[launcher]\n key = value\nutf8 = \u03bb\n");
  assert(get(c, L"key") == L"value" && get(c, L"utf8") == L"\u03bb");
  assert(quote_arg(L"C:\\Runtime Folder\\adb.exe") ==
         L"\"C:\\Runtime Folder\\adb.exe\"");
  assert(quote_arg(L"C:\\a\\\\") == L"\"C:\\a\\\\\\\\\"");
  assert(quote_arg(L"x\"y") == L"\"x\\\"y\"");
  assert(exact_boot_completed(L"0\n11\n") == false);
  assert(exact_boot_completed(L"0\r\n1\r\n") == true);
  assert(exact_boot_completed(L"property=1\n") == false);
  assert(resolve(L"C:\\JCS2", L"runtime\\x") ==
         fs::path(L"C:\\JCS2/runtime\\x"));
  assert(valid_port(5040) && !valid_port(80) && valid_emulator_port(5596) &&
         !valid_emulator_port(5597));
  assert(valid_port_pair(5040, 5596) && !valid_port_pair(5597, 5596) &&
         !valid_port_pair(5596, 5596));
  auto cfg = avd_config(L"C:\\JCS2\\sdk\\system-images\\android-28\\google_apis\\x86",
                        L"JCS2-personal");
  assert(cfg.find(L"image.sysdir.1 = C:\\JCS2\\sdk\\system-images\\android-28\\google_apis\\x86") != std::wstring::npos);
  assert(cfg.find(L"hw.ramSize = 1536") != std::wstring::npos &&
         cfg.find(L"hw.cpu.arch = x86") != std::wstring::npos &&
         cfg.find(L"tag.id = google_apis") != std::wstring::npos &&
         cfg.find(L"hw.gpu.mode = swiftshader_indirect") != std::wstring::npos);
  assert(avd_pointer(L"C:\\JCS2\\state\\avd\\JCS2-personal.avd") ==
         L"path=C:\\JCS2\\state\\avd\\JCS2-personal.avd\n");
  const std::wstring id = L"archive\npackage\nhelper\n";
  assert(bootstrap_marker_compatible(L"", id, true));
  assert(!bootstrap_marker_compatible(L"", id, false));
  assert(bootstrap_marker_compatible(L"pending\n" + id, id, false));
  assert(!bootstrap_marker_compatible(L"pending\nother\n", id, false));
  assert(bootstrap_marker_completed(id, id) && !bootstrap_marker_completed(L"pending\n" + id, id));
  std::cout << "native launcher logic tests passed\n";
}
