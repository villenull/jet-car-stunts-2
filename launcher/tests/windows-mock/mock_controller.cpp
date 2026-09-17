#include <windows.h>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>
namespace fs = std::filesystem;

static fs::path self() { wchar_t b[32768]; DWORD n=GetModuleFileNameW(nullptr,b,32768); return fs::path(std::wstring(b,n)); }
static bool has(const fs::path& d, const wchar_t* n) { return fs::exists(d / n); }
static std::string utf8(const std::wstring& s) {
  int n=WideCharToMultiByte(CP_UTF8,0,s.data(),(int)s.size(),nullptr,0,nullptr,nullptr);
  std::string out(n,'\0'); if(n) WideCharToMultiByte(CP_UTF8,0,s.data(),(int)s.size(),out.data(),n,nullptr,nullptr); return out;
}
int wmain(int ac, wchar_t** av) {
  fs::path d = self().parent_path(), ready;
  std::wstring serial = L"emulator-5596";
  std::wstring transport = L"uhid";
  for (int i=1; i+1<ac; ++i) {
    if (!wcscmp(av[i], L"--ready-file")) ready = av[++i];
    else if (!wcscmp(av[i], L"--adb-serial")) serial = av[++i];
  }
  {
    std::ofstream args(d / L"controller.args", std::ios::app);
    for (int i=1; i<ac; ++i) { auto x=utf8(av[i]); args.write(x.data(), (std::streamsize)x.size()); args.put('\n'); }
    args.flush();
  }
  if (has(d, L"controller_wrong_transport")) transport = L"console";
  if (has(d, L"controller_wrong_serial")) serial = L"emulator-9999";
  if (has(d, L"controller_no_ready")) ready.clear();
  if (has(d, L"controller_fail")) return 73;
  if (!ready.empty()) {
    std::wstring json = L"{\"serial\":\"" + serial + L"\",\"transport\":\"" + transport + L"\",\"device_id\":0}\n";
    std::wofstream f(ready); f << json;
    std::wofstream proof(d / L"controller.ready-proof"); proof << json;
  }
  if (has(d, L"controller_exit")) {
    // Keep the ready child alive through startup; once the mock game launch
    // creates stop, exit to exercise in-game controller liveness handling.
    while (!has(d, L"stop")) Sleep(100);
    return 74;
  }
  while (!has(d, L"stop")) Sleep(100);
  return 0;
}
