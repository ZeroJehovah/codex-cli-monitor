/* Native Windows smoke test; compile with mingw and -lshell32. */
#include "../windows/CodexMonitorWidget/src/diagnostics.c"
#include <assert.h>

static void fixture(const wchar_t *name) {
    wchar_t path[MAX_PATH];
    swprintf(path, MAX_PATH, L"%ls\\%ls", log_dir, name);
    HANDLE f = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    assert(f != INVALID_HANDLE_VALUE);
    CloseHandle(f);
}
static int exists(const wchar_t *name) {
    wchar_t path[MAX_PATH];
    swprintf(path, MAX_PATH, L"%ls\\%ls", log_dir, name);
    return GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES;
}
int main(void) {
    wchar_t temp[MAX_PATH], path[MAX_PATH];
    WIN32_FIND_DATAW entry;
    SYSTEMTIME st = {0};
    FILETIME ft;
    ULARGE_INTEGER now;
    LARGE_INTEGER size;
    DWORD bytes;
    char buffer[65537];
    assert(GetTempPathW(MAX_PATH, temp));
    assert(GetTempFileNameW(temp, L"cml", 0, log_dir));
    assert(DeleteFileW(log_dir));
    assert(CreateDirectoryW(log_dir, NULL));
    fixture(L"widget-20260927-1200.log");
    fixture(L"widget-20260928-1200.log");
    fixture(L"widget-20260928-1201.log");
    fixture(L"widget-20260927-1200.log.backup");
    fixture(L"unrelated.log");
    st.wYear=2026; st.wMonth=9; st.wDay=29; st.wHour=12;
    assert(SystemTimeToFileTime(&st, &ft));
    now.LowPart=ft.dwLowDateTime; now.HighPart=ft.dwHighDateTime;
    prune_logs(now.QuadPart);
    assert(!exists(L"widget-20260927-1200.log"));
    assert(!exists(L"widget-20260928-1200.log"));
    assert(exists(L"widget-20260928-1201.log"));
    assert(exists(L"widget-20260927-1200.log.backup"));
    assert(exists(L"unrelated.log"));
    SetLastError(1234);
    diagnostics_log("smoke\nline");
    assert(GetLastError()==1234);
    for(int i=0;i<2000;++i) diagnostics_log("bounded_record index=%d",i);
    GetSystemTime(&st);
    swprintf(path, MAX_PATH, L"%ls\\widget-%04u%02u%02u-%02u%02u.log", log_dir,
        st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute);
    HANDLE f=CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    assert(f!=INVALID_HANDLE_VALUE);
    assert(GetFileSizeEx(f,&size) && size.QuadPart>0 && size.QuadPart<=65536);
    assert(ReadFile(f,buffer,65536,&bytes,NULL)); buffer[bytes]=0;
    assert(strstr(buffer,"smoke line"));
    CloseHandle(f);
    swprintf(path,MAX_PATH,L"%ls\\*",log_dir);
    HANDLE search=FindFirstFileW(path,&entry);
    assert(search!=INVALID_HANDLE_VALUE);
    do {
        if(entry.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) continue;
        swprintf(path,MAX_PATH,L"%ls\\%ls",log_dir,entry.cFileName);
        assert(DeleteFileW(path));
    } while(FindNextFileW(search,&entry));
    FindClose(search);
    assert(RemoveDirectoryW(log_dir));
    puts("PASS: retention boundary, unrelated files, bounded log, sanitization, last-error preservation");
    return 0;
}
