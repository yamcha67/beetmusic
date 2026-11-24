/* "Beet Music.exe": a tiny windowless launcher that starts the bundled Python with the app.
 * Build:  windres launcher.rc -O coff -o launcher.res
 *         gcc -O2 -s -mwindows -municode launcher.c launcher.res -o "Beet Music.exe"
 */
#include <windows.h>
#include <wchar.h>

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE prev, PWSTR args, int show)
{
    wchar_t dir[MAX_PATH * 2];
    DWORD n = GetModuleFileNameW(NULL, dir, MAX_PATH * 2);
    if (n == 0 || n >= MAX_PATH * 2) return 1;
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (slash) *slash = 0;

    wchar_t exe[MAX_PATH * 2], cmd[MAX_PATH * 4 + 2048];
    swprintf(exe, MAX_PATH * 2, L"%ls\\bin\\BeetMusic.exe", dir);
    swprintf(cmd, MAX_PATH * 4 + 2048, L"\"%ls\" -X utf8 \"%ls\\app\\beet-music.py\" %ls", exe, dir, args ? args : L"");

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof si);
    si.cb = sizeof si;
    if (!CreateProcessW(exe, cmd, NULL, NULL, FALSE, 0, NULL, dir, &si, &pi)) {
        MessageBoxW(NULL, L"Beet Music could not start: bin\\BeetMusic.exe is missing.\n"
                          L"Please reinstall the app.", L"Beet Music", MB_ICONERROR | MB_OK);
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
