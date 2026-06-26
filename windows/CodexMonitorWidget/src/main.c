#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <windowsx.h>
#include <commctrl.h>
#include <shellapi.h>
#include <winhttp.h>
#include <ctype.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#define APP_CLASS_NAME L"CodexMonitorWidget"
#define DEFAULT_API_URL L"http://localhost:8765/api/sessions"
#define REFRESH_TIMER_ID 1
#define ANIMATION_TIMER_ID 2
#define REFRESH_INTERVAL_MS 1500
#define ANIMATION_INTERVAL_MS 80
#define RUNNING_PULSE_PERIOD_MS 1600
#define WM_FETCH_DONE (WM_APP + 1)
#define MENU_EXIT_ID 1001
#define MAX_SESSIONS 128
#define DOT_SIZE 14
#define DOT_GAP 8
#define PADDING_X 10
#define PADDING_Y 6
#define ROW_HEIGHT 26
#define DIRECTORY_TEXT_PADDING 8
#define COLUMN_GAP 12
#define MIN_PANEL_WIDTH 48
#define MIN_PANEL_HEIGHT 32

typedef struct Session {
    int pid;
    char status[32];
    char directory[512];
    char started_at_iso[64];
} Session;

typedef struct FetchResult {
    int count;
    int ok;
    char error[256];
    Session sessions[MAX_SESSIONS];
} FetchResult;

typedef struct DirectoryRow {
    char directory[512];
    int session_indexes[MAX_SESSIONS];
    int session_count;
} DirectoryRow;

typedef struct AppState {
    HWND hwnd;
    HWND tooltip;
    wchar_t api_url[1024];
    wchar_t tooltip_text[1024];
    TOOLINFOW tooltip_info;
    Session sessions[MAX_SESSIONS];
    DirectoryRow rows[MAX_SESSIONS];
    int session_count;
    int row_count;
    int directory_column_width;
    int empty_success_count;
    int hovered_session;
    int dragging;
    int drag_refresh_pending;
    int animation_timer_active;
    POINT drag_start;
    RECT drag_window;
    LONG fetching;
    char last_error[256];
} AppState;

static const char STATUS_IDLE[] = "\xe6\x9c\xaa\xe8\xbf\x90\xe8\xa1\x8c";
static const char STATUS_RUNNING[] = "\xe8\xbf\x90\xe8\xa1\x8c\xe4\xb8\xad";
static const char STATUS_SUCCESS[] = "\xe6\x88\x90\xe5\x8a\x9f";
static const char STATUS_FAILED[] = "\xe5\xa4\xb1\xe8\xb4\xa5";

static AppState g_app;

static void set_tooltip_for_hover(int index);
static void show_context_menu(HWND hwnd, POINT point);

static void utf8_to_wide(const char *source, wchar_t *target, int target_count) {
    if (target_count <= 0) {
        return;
    }
    target[0] = L'\0';
    if (source == NULL || source[0] == '\0') {
        return;
    }
    MultiByteToWideChar(CP_UTF8, 0, source, -1, target, target_count);
    target[target_count - 1] = L'\0';
}

static void copy_wide(wchar_t *target, int target_count, const wchar_t *source) {
    if (target_count <= 0) {
        return;
    }
    wcsncpy(target, source, target_count - 1);
    target[target_count - 1] = L'\0';
}

static void copy_ascii(char *target, int target_count, const char *source) {
    if (target_count <= 0) {
        return;
    }
    if (source == NULL) {
        target[0] = '\0';
        return;
    }
    strncpy(target, source, target_count - 1);
    target[target_count - 1] = '\0';
}

static void directory_display_name(const char *directory, char *target, int target_count) {
    size_t length;
    size_t end;
    size_t start;
    if (target_count <= 0) {
        return;
    }
    if (directory == NULL || directory[0] == '\0') {
        copy_ascii(target, target_count, "-");
        return;
    }
    length = strlen(directory);
    end = length;
    while (end > 1 && (directory[end - 1] == '/' || directory[end - 1] == '\\')) {
        end--;
    }
    start = end;
    while (start > 0 && directory[start - 1] != '/' && directory[start - 1] != '\\') {
        start--;
    }
    if (end <= start) {
        copy_ascii(target, target_count, directory);
        return;
    }
    if ((int)(end - start) >= target_count) {
        end = start + target_count - 1;
    }
    memcpy(target, directory + start, end - start);
    target[end - start] = '\0';
}

static int row_dot_width(int count) {
    if (count <= 0) {
        return 0;
    }
    return count * DOT_SIZE + (count - 1) * DOT_GAP;
}

static int max_row_session_count(void) {
    int row;
    int max_count = 0;
    for (row = 0; row < g_app.row_count; row++) {
        if (g_app.rows[row].session_count > max_count) {
            max_count = g_app.rows[row].session_count;
        }
    }
    return max_count;
}

static int dot_column_left(void) {
    return PADDING_X + g_app.directory_column_width + COLUMN_GAP;
}

static int panel_width(void) {
    int width;
    if (g_app.row_count <= 0) {
        return MIN_PANEL_WIDTH;
    }
    width = PADDING_X * 2 + g_app.directory_column_width + COLUMN_GAP + row_dot_width(max_row_session_count());
    if (width < MIN_PANEL_WIDTH) {
        return MIN_PANEL_WIDTH;
    }
    return width;
}

static int panel_height(void) {
    int height;
    if (g_app.row_count <= 0) {
        return MIN_PANEL_HEIGHT;
    }
    height = PADDING_Y * 2 + g_app.row_count * ROW_HEIGHT;
    if (height < MIN_PANEL_HEIGHT) {
        return MIN_PANEL_HEIGHT;
    }
    return height;
}

static RECT dot_rect(int row_index, int dot_index) {
    RECT rect;
    rect.left = dot_column_left() + dot_index * (DOT_SIZE + DOT_GAP);
    rect.top = PADDING_Y + row_index * ROW_HEIGHT + (ROW_HEIGHT - DOT_SIZE) / 2;
    rect.right = rect.left + DOT_SIZE;
    rect.bottom = rect.top + DOT_SIZE;
    return rect;
}

static int dot_at_point(POINT point) {
    int row;
    for (row = 0; row < g_app.row_count; row++) {
        int dot;
        for (dot = 0; dot < g_app.rows[row].session_count; dot++) {
            RECT rect = dot_rect(row, dot);
            if (PtInRect(&rect, point)) {
                return g_app.rows[row].session_indexes[dot];
            }
        }
    }
    return -1;
}

static void update_directory_column_width(void) {
    HDC hdc;
    HGDIOBJ old_font;
    int row;
    int max_width = 0;
    if (g_app.row_count <= 0 || g_app.hwnd == NULL) {
        g_app.directory_column_width = 0;
        return;
    }
    hdc = GetDC(g_app.hwnd);
    if (hdc == NULL) {
        g_app.directory_column_width = 0;
        return;
    }
    old_font = SelectObject(hdc, GetStockObject(DEFAULT_GUI_FONT));
    for (row = 0; row < g_app.row_count; row++) {
        char display_name[512];
        wchar_t display_name_wide[512];
        SIZE size;
        directory_display_name(g_app.rows[row].directory, display_name, sizeof(display_name));
        utf8_to_wide(display_name, display_name_wide, (int)(sizeof(display_name_wide) / sizeof(display_name_wide[0])));
        if (GetTextExtentPoint32W(hdc, display_name_wide, (int)wcslen(display_name_wide), &size) &&
            size.cx > max_width) {
            max_width = size.cx;
        }
    }
    SelectObject(hdc, old_font);
    ReleaseDC(g_app.hwnd, hdc);
    g_app.directory_column_width = max_width + DIRECTORY_TEXT_PADDING;
}

static void rebuild_directory_rows(void) {
    int index;
    g_app.row_count = 0;
    for (index = 0; index < g_app.session_count; index++) {
        Session *session = &g_app.sessions[index];
        const char *directory = session->directory[0] ? session->directory : "-";
        int row;
        int target_row = -1;
        for (row = 0; row < g_app.row_count; row++) {
            if (strcmp(g_app.rows[row].directory, directory) == 0) {
                target_row = row;
                break;
            }
        }
        if (target_row < 0) {
            if (g_app.row_count >= MAX_SESSIONS) {
                break;
            }
            target_row = g_app.row_count++;
            ZeroMemory(&g_app.rows[target_row], sizeof(g_app.rows[target_row]));
            copy_ascii(g_app.rows[target_row].directory, sizeof(g_app.rows[target_row].directory), directory);
        }
        if (g_app.rows[target_row].session_count < MAX_SESSIONS) {
            g_app.rows[target_row].session_indexes[g_app.rows[target_row].session_count++] = index;
        }
    }
    if (g_app.hovered_session >= g_app.session_count) {
        g_app.hovered_session = -1;
    }
    update_directory_column_width();
}

static int is_running_status(const char *status) {
    return strcmp(status, STATUS_RUNNING) == 0;
}

static int has_running_sessions(void) {
    int index;
    for (index = 0; index < g_app.session_count; index++) {
        if (is_running_status(g_app.sessions[index].status)) {
            return 1;
        }
    }
    return 0;
}

static COLORREF status_color(const char *status) {
    if (is_running_status(status)) {
        return RGB(37, 99, 235);
    }
    if (strcmp(status, STATUS_SUCCESS) == 0) {
        return RGB(132, 204, 22);
    }
    if (strcmp(status, STATUS_FAILED) == 0) {
        return RGB(235, 87, 87);
    }
    if (strcmp(status, STATUS_IDLE) == 0) {
        return RGB(139, 143, 152);
    }
    return RGB(139, 143, 152);
}

static int running_pulse_level(void) {
    DWORD elapsed = GetTickCount() % RUNNING_PULSE_PERIOD_MS;
    DWORD half_period = RUNNING_PULSE_PERIOD_MS / 2;
    if (elapsed > half_period) {
        elapsed = RUNNING_PULSE_PERIOD_MS - elapsed;
    }
    return (int)(elapsed * 100 / half_period);
}

static const char *skip_space(const char *p, const char *end) {
    while (p < end && isspace((unsigned char)*p)) {
        p++;
    }
    return p;
}

static const char *find_top_level_key(const char *start, const char *end, const char *key) {
    size_t key_len = strlen(key);
    const char *p = start;
    int depth = 0;
    int in_string = 0;
    int escape = 0;
    while (p < end) {
        char c = *p;
        if (in_string) {
            if (escape) {
                escape = 0;
            } else if (c == '\\') {
                escape = 1;
            } else if (c == '"') {
                in_string = 0;
            }
            p++;
            continue;
        }
        if (c == '"') {
            if (depth == 1 && p + key_len + 2 < end &&
                memcmp(p + 1, key, key_len) == 0 && p[key_len + 1] == '"') {
                const char *colon = p + key_len + 2;
                colon = skip_space(colon, end);
                if (colon < end && *colon == ':') {
                    return skip_space(colon + 1, end);
                }
            }
            in_string = 1;
        } else if (c == '{' || c == '[') {
            depth++;
        } else if (c == '}' || c == ']') {
            depth--;
        }
        p++;
    }
    return NULL;
}

static void parse_json_string(const char *value, const char *end, char *out, int out_count) {
    int written = 0;
    const char *p;
    if (out_count <= 0) {
        return;
    }
    out[0] = '\0';
    if (value == NULL || value >= end || *value != '"') {
        return;
    }
    p = value + 1;
    while (p < end && *p != '"' && written < out_count - 1) {
        if (*p == '\\' && p + 1 < end) {
            p++;
            if (*p == '"' || *p == '\\' || *p == '/') {
                out[written++] = *p++;
            } else if (*p == 'n') {
                out[written++] = '\n';
                p++;
            } else if (*p == 'r') {
                out[written++] = '\r';
                p++;
            } else if (*p == 't') {
                out[written++] = '\t';
                p++;
            } else if (*p == 'u' && p + 4 < end) {
                p += 5;
            } else {
                p++;
            }
        } else {
            out[written++] = *p++;
        }
    }
    out[written] = '\0';
}

static int parse_json_int(const char *value, const char *end) {
    char buffer[32];
    int length = 0;
    const char *p = value;
    if (p == NULL) {
        return 0;
    }
    while (p < end && (isdigit((unsigned char)*p) || *p == '-') && length < (int)sizeof(buffer) - 1) {
        buffer[length++] = *p++;
    }
    buffer[length] = '\0';
    return atoi(buffer);
}

static const char *matching_brace(const char *start, const char *end) {
    int depth = 0;
    int in_string = 0;
    int escape = 0;
    const char *p;
    for (p = start; p < end; p++) {
        char c = *p;
        if (in_string) {
            if (escape) {
                escape = 0;
            } else if (c == '\\') {
                escape = 1;
            } else if (c == '"') {
                in_string = 0;
            }
            continue;
        }
        if (c == '"') {
            in_string = 1;
        } else if (c == '{') {
            depth++;
        } else if (c == '}') {
            depth--;
            if (depth == 0) {
                return p + 1;
            }
        }
    }
    return NULL;
}

static void parse_session_object(const char *start, const char *end, Session *session) {
    memset(session, 0, sizeof(*session));
    session->pid = parse_json_int(find_top_level_key(start, end, "pid"), end);
    parse_json_string(find_top_level_key(start, end, "status"), end, session->status, sizeof(session->status));
    parse_json_string(find_top_level_key(start, end, "directory"), end, session->directory, sizeof(session->directory));
    parse_json_string(find_top_level_key(start, end, "started_at_iso"), end, session->started_at_iso, sizeof(session->started_at_iso));
}

static void parse_sessions_json(const char *json, FetchResult *result) {
    const char *end = json + strlen(json);
    const char *sessions = strstr(json, "\"sessions\"");
    const char *p;
    result->count = 0;
    if (sessions == NULL) {
        copy_ascii(result->error, sizeof(result->error), "missing sessions");
        result->ok = 0;
        return;
    }
    p = (const char *)memchr(sessions, '[', (size_t)(end - sessions));
    if (p == NULL) {
        copy_ascii(result->error, sizeof(result->error), "invalid sessions");
        result->ok = 0;
        return;
    }
    p++;
    while (p < end && result->count < MAX_SESSIONS) {
        const char *object_end;
        p = skip_space(p, end);
        if (p >= end || *p == ']') {
            break;
        }
        if (*p != '{') {
            p++;
            continue;
        }
        object_end = matching_brace(p, end);
        if (object_end == NULL) {
            break;
        }
        parse_session_object(p, object_end, &result->sessions[result->count]);
        result->count++;
        p = object_end;
    }
    result->ok = 1;
}

static int append_bytes(char **buffer, DWORD *length, DWORD chunk_size) {
    char *new_buffer;
    if (*buffer == NULL) {
        new_buffer = (char *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, *length + chunk_size + 1);
    } else {
        new_buffer = (char *)HeapReAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, *buffer, *length + chunk_size + 1);
    }
    if (new_buffer == NULL) {
        return 0;
    }
    *buffer = new_buffer;
    return 1;
}

static void build_request_path(const URL_COMPONENTSW *parts, wchar_t *path, int path_count) {
    int offset = 0;
    if (
        parts->dwUrlPathLength > 1 ||
        (parts->dwUrlPathLength == 1 && parts->lpszUrlPath[0] != L'/')
    ) {
        int copy_len = (int)parts->dwUrlPathLength;
        if (copy_len >= path_count) {
            copy_len = path_count - 1;
        }
        wcsncpy(path, parts->lpszUrlPath, copy_len);
        path[copy_len] = L'\0';
        offset = copy_len;
    } else {
        copy_wide(path, path_count, L"/api/sessions");
        offset = (int)wcslen(path);
    }
    if (parts->dwExtraInfoLength > 0 && offset < path_count - 1) {
        int copy_len = (int)parts->dwExtraInfoLength;
        if (offset + copy_len >= path_count) {
            copy_len = path_count - offset - 1;
        }
        wcsncpy(path + offset, parts->lpszExtraInfo, copy_len);
        path[offset + copy_len] = L'\0';
    }
}

static int fetch_json(char **json, char *error, int error_count) {
    wchar_t host[256];
    wchar_t path[1024];
    URL_COMPONENTSW parts;
    HINTERNET session = NULL;
    HINTERNET connect = NULL;
    HINTERNET request = NULL;
    DWORD flags;
    DWORD status_code = 0;
    DWORD status_size = sizeof(status_code);
    DWORD total = 0;
    int ok = 0;
    *json = NULL;

    ZeroMemory(&parts, sizeof(parts));
    parts.dwStructSize = sizeof(parts);
    parts.lpszHostName = host;
    parts.dwHostNameLength = (DWORD)(sizeof(host) / sizeof(host[0]));
    parts.dwUrlPathLength = (DWORD)-1;
    parts.dwExtraInfoLength = (DWORD)-1;

    if (!WinHttpCrackUrl(g_app.api_url, 0, 0, &parts)) {
        copy_ascii(error, error_count, "invalid API URL");
        return 0;
    }
    host[parts.dwHostNameLength] = L'\0';
    build_request_path(&parts, path, (int)(sizeof(path) / sizeof(path[0])));

    session = WinHttpOpen(L"CodexMonitorWidget/1.0", WINHTTP_ACCESS_TYPE_DEFAULT_PROXY, WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (session == NULL) {
        copy_ascii(error, error_count, "WinHttpOpen failed");
        goto cleanup;
    }
    WinHttpSetTimeouts(session, 2000, 2000, 5000, 5000);
    connect = WinHttpConnect(session, host, parts.nPort, 0);
    if (connect == NULL) {
        copy_ascii(error, error_count, "WinHttpConnect failed");
        goto cleanup;
    }
    flags = parts.nScheme == INTERNET_SCHEME_HTTPS ? WINHTTP_FLAG_SECURE : 0;
    request = WinHttpOpenRequest(connect, L"GET", path, NULL, WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, flags);
    if (request == NULL) {
        copy_ascii(error, error_count, "WinHttpOpenRequest failed");
        goto cleanup;
    }
    if (!WinHttpSendRequest(request, WINHTTP_NO_ADDITIONAL_HEADERS, 0, WINHTTP_NO_REQUEST_DATA, 0, 0, 0) ||
        !WinHttpReceiveResponse(request, NULL)) {
        copy_ascii(error, error_count, "API request failed");
        goto cleanup;
    }
    if (!WinHttpQueryHeaders(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, WINHTTP_HEADER_NAME_BY_INDEX, &status_code, &status_size, WINHTTP_NO_HEADER_INDEX) ||
        status_code < 200 || status_code >= 300) {
        copy_ascii(error, error_count, "API returned non-2xx status");
        goto cleanup;
    }
    for (;;) {
        DWORD available = 0;
        DWORD read = 0;
        if (!WinHttpQueryDataAvailable(request, &available)) {
            copy_ascii(error, error_count, "WinHttpQueryDataAvailable failed");
            goto cleanup;
        }
        if (available == 0) {
            break;
        }
        if (!append_bytes(json, &total, available)) {
            copy_ascii(error, error_count, "out of memory");
            goto cleanup;
        }
        if (!WinHttpReadData(request, *json + total, available, &read)) {
            copy_ascii(error, error_count, "WinHttpReadData failed");
            goto cleanup;
        }
        total += read;
        (*json)[total] = '\0';
    }
    ok = 1;

cleanup:
    if (!ok && *json != NULL) {
        HeapFree(GetProcessHeap(), 0, *json);
        *json = NULL;
    }
    if (request != NULL) {
        WinHttpCloseHandle(request);
    }
    if (connect != NULL) {
        WinHttpCloseHandle(connect);
    }
    if (session != NULL) {
        WinHttpCloseHandle(session);
    }
    return ok;
}

static DWORD WINAPI fetch_thread(LPVOID parameter) {
    FetchResult *result = (FetchResult *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(FetchResult));
    char *json = NULL;
    HWND hwnd = (HWND)parameter;
    if (result == NULL) {
        InterlockedExchange(&g_app.fetching, 0);
        return 1;
    }
    if (fetch_json(&json, result->error, sizeof(result->error))) {
        parse_sessions_json(json, result);
        HeapFree(GetProcessHeap(), 0, json);
    } else {
        result->ok = 0;
    }
    PostMessageW(hwnd, WM_FETCH_DONE, 0, (LPARAM)result);
    return 0;
}

static void start_fetch(void) {
    HANDLE thread;
    if (InterlockedExchange(&g_app.fetching, 1) != 0) {
        return;
    }
    thread = CreateThread(NULL, 0, fetch_thread, g_app.hwnd, 0, NULL);
    if (thread == NULL) {
        InterlockedExchange(&g_app.fetching, 0);
        return;
    }
    CloseHandle(thread);
}

static void resize_panel(void) {
    RECT rect;
    int width = panel_width();
    int height = panel_height();
    GetWindowRect(g_app.hwnd, &rect);
    SetWindowPos(g_app.hwnd, HWND_TOPMOST, rect.left, rect.top, width, height, SWP_NOACTIVATE);
}

static void update_tool_rect(void) {
    RECT rect;
    GetClientRect(g_app.hwnd, &rect);
    g_app.tooltip_info.rect = rect;
    SendMessageW(g_app.tooltip, TTM_NEWTOOLRECTW, 0, (LPARAM)&g_app.tooltip_info);
}

static void update_animation_timer(void) {
    if (has_running_sessions()) {
        if (!g_app.animation_timer_active) {
            SetTimer(g_app.hwnd, ANIMATION_TIMER_ID, ANIMATION_INTERVAL_MS, NULL);
            g_app.animation_timer_active = 1;
        }
    } else if (g_app.animation_timer_active) {
        KillTimer(g_app.hwnd, ANIMATION_TIMER_ID);
        g_app.animation_timer_active = 0;
    }
}

static void refresh_widget_view(void) {
    resize_panel();
    update_tool_rect();
    update_animation_timer();
    InvalidateRect(g_app.hwnd, NULL, FALSE);
    set_tooltip_for_hover(g_app.hovered_session);
}

static void set_tooltip_for_hover(int index) {
    wchar_t status[64];
    wchar_t directory[512];
    wchar_t started[128];
    if (index >= 0 && index < g_app.session_count) {
        Session *session = &g_app.sessions[index];
        utf8_to_wide(session->status, status, (int)(sizeof(status) / sizeof(status[0])));
        utf8_to_wide(session->directory[0] ? session->directory : "-", directory, (int)(sizeof(directory) / sizeof(directory[0])));
        utf8_to_wide(session->started_at_iso[0] ? session->started_at_iso : "-", started, (int)(sizeof(started) / sizeof(started[0])));
        _snwprintf(g_app.tooltip_text, sizeof(g_app.tooltip_text) / sizeof(g_app.tooltip_text[0]) - 1,
            L"PID: %d\nStatus: %ls\nDirectory: %ls\nStarted: %ls",
            session->pid, status, directory, started);
    } else if (g_app.last_error[0] != '\0') {
        wchar_t error_text[512];
        utf8_to_wide(g_app.last_error, error_text, (int)(sizeof(error_text) / sizeof(error_text[0])));
        _snwprintf(g_app.tooltip_text, sizeof(g_app.tooltip_text) / sizeof(g_app.tooltip_text[0]) - 1,
            L"API unavailable: %ls", error_text);
    } else {
        g_app.tooltip_text[0] = L'\0';
    }
    g_app.tooltip_text[sizeof(g_app.tooltip_text) / sizeof(g_app.tooltip_text[0]) - 1] = L'\0';
    g_app.tooltip_info.lpszText = g_app.tooltip_text;
    SendMessageW(g_app.tooltip, TTM_UPDATETIPTEXTW, 0, (LPARAM)&g_app.tooltip_info);
}

static void init_tooltip(HWND hwnd) {
    INITCOMMONCONTROLSEX controls;
    RECT rect;
    controls.dwSize = sizeof(controls);
    controls.dwICC = ICC_WIN95_CLASSES;
    InitCommonControlsEx(&controls);
    g_app.tooltip = CreateWindowExW(WS_EX_TOPMOST, TOOLTIPS_CLASSW, NULL,
        WS_POPUP | TTS_ALWAYSTIP | TTS_NOPREFIX,
        CW_USEDEFAULT, CW_USEDEFAULT, CW_USEDEFAULT, CW_USEDEFAULT,
        hwnd, NULL, GetModuleHandleW(NULL), NULL);
    SetWindowPos(g_app.tooltip, HWND_TOPMOST, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
    GetClientRect(hwnd, &rect);
    ZeroMemory(&g_app.tooltip_info, sizeof(g_app.tooltip_info));
    g_app.tooltip_info.cbSize = sizeof(g_app.tooltip_info);
    g_app.tooltip_info.uFlags = TTF_SUBCLASS;
    g_app.tooltip_info.hwnd = hwnd;
    g_app.tooltip_info.uId = 1;
    g_app.tooltip_info.rect = rect;
    g_app.tooltip_info.lpszText = g_app.tooltip_text;
    SendMessageW(g_app.tooltip, TTM_ADDTOOLW, 0, (LPARAM)&g_app.tooltip_info);
}

static void show_context_menu(HWND hwnd, POINT point) {
    HMENU menu = CreatePopupMenu();
    UINT command;
    if (menu == NULL) {
        return;
    }
    AppendMenuW(menu, MF_STRING, MENU_EXIT_ID, L"\x9000\x51fa");
    SetForegroundWindow(hwnd);
    command = TrackPopupMenuEx(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
        point.x, point.y, hwnd, NULL);
    DestroyMenu(menu);
    if (command == MENU_EXIT_ID) {
        DestroyWindow(hwnd);
    }
    PostMessageW(hwnd, WM_NULL, 0, 0);
}

static void fill_dot(HDC hdc, const RECT *rect, COLORREF color) {
    HBRUSH brush = CreateSolidBrush(color);
    HGDIOBJ old_brush = SelectObject(hdc, brush);
    HPEN pen = CreatePen(PS_SOLID, 1, color);
    HGDIOBJ old_pen = SelectObject(hdc, pen);
    Ellipse(hdc, rect->left, rect->top, rect->right, rect->bottom);
    SelectObject(hdc, old_pen);
    SelectObject(hdc, old_brush);
    DeleteObject(pen);
    DeleteObject(brush);
}

static void draw_status_dot(HDC hdc, const RECT *rect, const char *status) {
    if (is_running_status(status)) {
        int pulse = running_pulse_level();
        int expand = 2 + pulse / 50;
        RECT halo = *rect;
        RECT core = *rect;
        COLORREF halo_color = RGB(22 + pulse * 10 / 100, 50 + pulse * 35 / 100, 120 + pulse * 65 / 100);
        COLORREF core_color = RGB(37 + pulse * 38 / 100, 99 + pulse * 56 / 100, 235 + pulse * 20 / 100);
        InflateRect(&halo, expand, expand);
        InflateRect(&core, -(1 - pulse / 100), -(1 - pulse / 100));
        fill_dot(hdc, &halo, halo_color);
        fill_dot(hdc, &core, core_color);
        return;
    }
    fill_dot(hdc, rect, status_color(status));
}

static void paint_widget(HWND hwnd, HDC hdc) {
    RECT client;
    HBRUSH background;
    HBRUSH row_background;
    HPEN border;
    HPEN grid;
    HGDIOBJ old_pen;
    HGDIOBJ old_font;
    int row;
    GetClientRect(hwnd, &client);
    background = CreateSolidBrush(RGB(34, 34, 34));
    FillRect(hdc, &client, background);
    DeleteObject(background);
    border = CreatePen(PS_SOLID, 1, RGB(86, 86, 86));
    old_pen = SelectObject(hdc, border);
    SelectObject(hdc, GetStockObject(NULL_BRUSH));
    Rectangle(hdc, client.left, client.top, client.right, client.bottom);
    SelectObject(hdc, old_pen);
    DeleteObject(border);
    if (g_app.row_count > 0) {
        grid = CreatePen(PS_SOLID, 1, RGB(60, 60, 60));
        old_pen = SelectObject(hdc, grid);
        MoveToEx(hdc, dot_column_left() - COLUMN_GAP / 2, PADDING_Y, NULL);
        LineTo(hdc, dot_column_left() - COLUMN_GAP / 2, client.bottom - PADDING_Y);
        SelectObject(hdc, old_pen);
        DeleteObject(grid);
    }
    SetBkMode(hdc, TRANSPARENT);
    SetTextColor(hdc, RGB(225, 225, 225));
    old_font = SelectObject(hdc, GetStockObject(DEFAULT_GUI_FONT));
    for (row = 0; row < g_app.row_count; row++) {
        RECT row_rect;
        RECT text_rect;
        char display_name[512];
        wchar_t display_name_wide[512];
        int dot;
        row_rect.left = 1;
        row_rect.top = PADDING_Y + row * ROW_HEIGHT;
        row_rect.right = client.right - 1;
        row_rect.bottom = row_rect.top + ROW_HEIGHT;
        if (row % 2 == 1) {
            row_background = CreateSolidBrush(RGB(39, 39, 39));
            FillRect(hdc, &row_rect, row_background);
            DeleteObject(row_background);
        }
        if (row < g_app.row_count - 1) {
            HPEN row_line = CreatePen(PS_SOLID, 1, RGB(54, 54, 54));
            HGDIOBJ old_row_pen = SelectObject(hdc, row_line);
            MoveToEx(hdc, 1, row_rect.bottom, NULL);
            LineTo(hdc, client.right - 1, row_rect.bottom);
            SelectObject(hdc, old_row_pen);
            DeleteObject(row_line);
        }
        directory_display_name(g_app.rows[row].directory, display_name, sizeof(display_name));
        utf8_to_wide(display_name, display_name_wide, (int)(sizeof(display_name_wide) / sizeof(display_name_wide[0])));
        text_rect.left = PADDING_X;
        text_rect.top = row_rect.top;
        text_rect.right = PADDING_X + g_app.directory_column_width;
        text_rect.bottom = row_rect.bottom;
        DrawTextW(hdc, display_name_wide, -1, &text_rect,
            DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX);
        for (dot = 0; dot < g_app.rows[row].session_count; dot++) {
            int session_index = g_app.rows[row].session_indexes[dot];
            RECT rect = dot_rect(row, dot);
            draw_status_dot(hdc, &rect, g_app.sessions[session_index].status);
        }
    }
    SelectObject(hdc, old_font);
}

static LRESULT CALLBACK window_proc(HWND hwnd, UINT message, WPARAM wparam, LPARAM lparam) {
    switch (message) {
    case WM_CREATE:
        g_app.hwnd = hwnd;
        g_app.hovered_session = -1;
        init_tooltip(hwnd);
        SetTimer(hwnd, REFRESH_TIMER_ID, REFRESH_INTERVAL_MS, NULL);
        start_fetch();
        return 0;
    case WM_TIMER:
        if (wparam == REFRESH_TIMER_ID) {
            start_fetch();
        } else if (wparam == ANIMATION_TIMER_ID) {
            InvalidateRect(hwnd, NULL, FALSE);
        }
        return 0;
    case WM_FETCH_DONE: {
        FetchResult *result = (FetchResult *)lparam;
        if (result != NULL) {
            if (result->ok) {
                if (result->count > 0 || g_app.session_count == 0) {
                    g_app.session_count = result->count;
                    memcpy(g_app.sessions, result->sessions, sizeof(Session) * result->count);
                    rebuild_directory_rows();
                    g_app.empty_success_count = 0;
                } else {
                    g_app.empty_success_count++;
                    if (g_app.empty_success_count >= 3) {
                        g_app.session_count = 0;
                        rebuild_directory_rows();
                        g_app.empty_success_count = 0;
                    }
                }
                g_app.last_error[0] = '\0';
            } else {
                copy_ascii(g_app.last_error, sizeof(g_app.last_error), result->error);
            }
            HeapFree(GetProcessHeap(), 0, result);
        }
        InterlockedExchange(&g_app.fetching, 0);
        if (g_app.dragging) {
            g_app.drag_refresh_pending = 1;
        } else {
            refresh_widget_view();
        }
        return 0;
    }
    case WM_PAINT: {
        PAINTSTRUCT ps;
        HDC hdc = BeginPaint(hwnd, &ps);
        paint_widget(hwnd, hdc);
        EndPaint(hwnd, &ps);
        return 0;
    }
    case WM_LBUTTONDOWN:
        g_app.dragging = 1;
        g_app.drag_refresh_pending = 0;
        g_app.drag_start.x = GET_X_LPARAM(lparam);
        g_app.drag_start.y = GET_Y_LPARAM(lparam);
        ClientToScreen(hwnd, &g_app.drag_start);
        GetWindowRect(hwnd, &g_app.drag_window);
        SetCapture(hwnd);
        return 0;
    case WM_MOUSEMOVE: {
        POINT point;
        int hovered;
        point.x = GET_X_LPARAM(lparam);
        point.y = GET_Y_LPARAM(lparam);
        if (g_app.dragging) {
            ClientToScreen(hwnd, &point);
            int dx = point.x - g_app.drag_start.x;
            int dy = point.y - g_app.drag_start.y;
            SetWindowPos(hwnd, HWND_TOPMOST, g_app.drag_window.left + dx, g_app.drag_window.top + dy,
                0, 0, SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_NOSENDCHANGING);
            return 0;
        }
        hovered = dot_at_point(point);
        if (hovered != g_app.hovered_session) {
            g_app.hovered_session = hovered;
            set_tooltip_for_hover(hovered);
        }
        return 0;
    }
    case WM_LBUTTONUP:
        if (g_app.dragging) {
            g_app.dragging = 0;
            ReleaseCapture();
            if (g_app.drag_refresh_pending) {
                g_app.drag_refresh_pending = 0;
                refresh_widget_view();
            }
        }
        return 0;
    case WM_RBUTTONUP: {
        POINT point;
        if (g_app.dragging) {
            g_app.dragging = 0;
            ReleaseCapture();
        }
        point.x = GET_X_LPARAM(lparam);
        point.y = GET_Y_LPARAM(lparam);
        ClientToScreen(hwnd, &point);
        show_context_menu(hwnd, point);
        return 0;
    }
    case WM_CONTEXTMENU: {
        POINT point;
        if ((int)(short)LOWORD(lparam) == -1 && (int)(short)HIWORD(lparam) == -1) {
            RECT rect;
            GetWindowRect(hwnd, &rect);
            point.x = rect.left + (rect.right - rect.left) / 2;
            point.y = rect.top + (rect.bottom - rect.top) / 2;
        } else {
            point.x = (int)(short)LOWORD(lparam);
            point.y = (int)(short)HIWORD(lparam);
        }
        show_context_menu(hwnd, point);
        return 0;
    }
    case WM_COMMAND:
        if (LOWORD(wparam) == MENU_EXIT_ID) {
            DestroyWindow(hwnd);
            return 0;
        }
        return DefWindowProcW(hwnd, message, wparam, lparam);
    case WM_SIZE:
        update_tool_rect();
        return 0;
    case WM_DESTROY:
        KillTimer(hwnd, REFRESH_TIMER_ID);
        KillTimer(hwnd, ANIMATION_TIMER_ID);
        PostQuitMessage(0);
        return 0;
    default:
        return DefWindowProcW(hwnd, message, wparam, lparam);
    }
}

static void resolve_api_url(void) {
    int argc = 0;
    LPWSTR *argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    DWORD env_len;
    wchar_t env_url[1024];
    if (argc > 1 && argv != NULL) {
        copy_wide(g_app.api_url, (int)(sizeof(g_app.api_url) / sizeof(g_app.api_url[0])), argv[1]);
    } else {
        env_len = GetEnvironmentVariableW(L"CODEX_MONITOR_API_URL", env_url, (DWORD)(sizeof(env_url) / sizeof(env_url[0])));
        if (env_len > 0 && env_len < sizeof(env_url) / sizeof(env_url[0])) {
            copy_wide(g_app.api_url, (int)(sizeof(g_app.api_url) / sizeof(g_app.api_url[0])), env_url);
        } else {
            copy_wide(g_app.api_url, (int)(sizeof(g_app.api_url) / sizeof(g_app.api_url[0])), DEFAULT_API_URL);
        }
    }
    if (argv != NULL) {
        LocalFree(argv);
    }
    if (wcsncmp(g_app.api_url, L"http://", 7) != 0 && wcsncmp(g_app.api_url, L"https://", 8) != 0) {
        wchar_t with_scheme[1024];
        _snwprintf(with_scheme, sizeof(with_scheme) / sizeof(with_scheme[0]) - 1, L"http://%ls", g_app.api_url);
        with_scheme[sizeof(with_scheme) / sizeof(with_scheme[0]) - 1] = L'\0';
        copy_wide(g_app.api_url, (int)(sizeof(g_app.api_url) / sizeof(g_app.api_url[0])), with_scheme);
    }
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous_instance, PWSTR command_line, int show_command) {
    WNDCLASSW wc;
    HWND hwnd;
    RECT work_area;
    MSG message;
    (void)previous_instance;
    (void)command_line;
    (void)show_command;

    ZeroMemory(&g_app, sizeof(g_app));
    resolve_api_url();
    ZeroMemory(&wc, sizeof(wc));
    wc.lpfnWndProc = window_proc;
    wc.hInstance = instance;
    wc.lpszClassName = APP_CLASS_NAME;
    wc.hCursor = LoadCursorW(NULL, IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
    if (!RegisterClassW(&wc)) {
        return 1;
    }
    SystemParametersInfoW(SPI_GETWORKAREA, 0, &work_area, 0);
    hwnd = CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW, APP_CLASS_NAME, L"Codex Monitor",
        WS_POPUP, work_area.right - MIN_PANEL_WIDTH - 24, work_area.top + 80,
        MIN_PANEL_WIDTH, MIN_PANEL_HEIGHT, NULL, NULL, instance, NULL);
    if (hwnd == NULL) {
        return 1;
    }
    ShowWindow(hwnd, SW_SHOWNOACTIVATE);
    UpdateWindow(hwnd);
    while (GetMessageW(&message, NULL, 0, 0) > 0) {
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
    return (int)message.wParam;
}
