#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shlobj.h>
#include <stdio.h>
#include <stdarg.h>
#include "diagnostics.h"

static SRWLOCK log_lock = SRWLOCK_INIT;
static wchar_t log_dir[MAX_PATH];
static ULONGLONG last_minute;

/* Only our exact UTC minute filenames are eligible for retention cleanup. */
static void prune_logs(ULONGLONG now) {
    WIN32_FIND_DATAW entry;
    wchar_t path[MAX_PATH];
    HANDLE search;
    swprintf(path, MAX_PATH, L"%ls\\widget-*.log", log_dir);
    search = FindFirstFileW(path, &entry);
    if (search == INVALID_HANDLE_VALUE) return;
    do {
        unsigned y, m, d, h, minute;
        SYSTEMTIME st = {0};
        FILETIME ft;
        ULARGE_INTEGER value;
        wchar_t canonical[64];
        if (entry.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) continue;
        if (swscanf(entry.cFileName, L"widget-%4u%2u%2u-%2u%2u.log", &y, &m, &d, &h, &minute) != 5) continue;
        swprintf(canonical, 64, L"widget-%04u%02u%02u-%02u%02u.log", y, m, d, h, minute);
        if (wcscmp(canonical, entry.cFileName)) continue;
        st.wYear = (WORD)y; st.wMonth = (WORD)m; st.wDay = (WORD)d;
        st.wHour = (WORD)h; st.wMinute = (WORD)minute;
        if (!SystemTimeToFileTime(&st, &ft)) continue;
        value.LowPart = ft.dwLowDateTime; value.HighPart = ft.dwHighDateTime;
        if (now >= value.QuadPart && now - value.QuadPart >= 864000000000ULL) {
            swprintf(path, MAX_PATH, L"%ls\\%ls", log_dir, entry.cFileName);
            DeleteFileW(path);
        }
    } while (FindNextFileW(search, &entry));
    FindClose(search);
}

void diagnostics_init(void) {
    wchar_t base[MAX_PATH];
    if (FAILED(SHGetFolderPathW(NULL, CSIDL_LOCAL_APPDATA, NULL, SHGFP_TYPE_CURRENT, base))) return;
    if (wcslen(base) + 64 >= MAX_PATH) return;
    swprintf(log_dir, MAX_PATH, L"%ls\\CodexMonitorWidget", base);
    CreateDirectoryW(log_dir, NULL);
    swprintf(log_dir, MAX_PATH, L"%ls\\CodexMonitorWidget\\logs", base);
    CreateDirectoryW(log_dir, NULL);
    diagnostics_log("startup build=%s_%s retention_hours=24 minute_limit_bytes=65536", __DATE__, __TIME__);
}

void diagnostics_log(const char *format, ...) {
    DWORD saved_error = GetLastError(), written;
    SYSTEMTIME st;
    FILETIME ft;
    ULARGE_INTEGER now;
    LARGE_INTEGER size;
    HANDLE file;
    wchar_t path[MAX_PATH];
    char body[1536], line[1792];
    va_list args;
    if (!log_dir[0] || !TryAcquireSRWLockExclusive(&log_lock)) return;
    GetSystemTime(&st);
    SystemTimeToFileTime(&st, &ft);
    now.LowPart = ft.dwLowDateTime; now.HighPart = ft.dwHighDateTime;
    if (last_minute != now.QuadPart / 600000000ULL) {
        prune_logs(now.QuadPart);
        last_minute = now.QuadPart / 600000000ULL;
    }
    va_start(args, format);
    vsnprintf(body, sizeof(body), format, args);
    va_end(args);
    body[sizeof(body)-1] = '\0';
    for (char *p = body; *p; ++p) if ((unsigned char)*p < 32) *p = ' ';
    snprintf(line, sizeof(line), "%04u-%02u-%02uT%02u:%02u:%02u.%03uZ pid=%lu tid=%lu %s\r\n",
        st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond, st.wMilliseconds,
        GetCurrentProcessId(), GetCurrentThreadId(), body);
    swprintf(path, MAX_PATH, L"%ls\\widget-%04u%02u%02u-%02u%02u.log", log_dir,
        st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute);
    file = CreateFileW(path, FILE_APPEND_DATA | FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file != INVALID_HANDLE_VALUE) {
        if (GetFileSizeEx(file, &size) && size.QuadPart + (LONGLONG)strlen(line) <= 65536)
            WriteFile(file, line, (DWORD)strlen(line), &written, NULL);
        CloseHandle(file);
    }
    ReleaseSRWLockExclusive(&log_lock);
    SetLastError(saved_error);
}
