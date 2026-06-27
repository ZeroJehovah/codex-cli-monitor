#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <windowsx.h>
#include <commctrl.h>
#include <shellapi.h>
#include <winhttp.h>
#include <ctype.h>
#include <float.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#define APP_CLASS_NAME L"CodexMonitorWidget"
#define DEFAULT_API_URL L"http://localhost:8765/api/sessions"
#define REFRESH_TIMER_ID 1
#define ANIMATION_TIMER_ID 2
#define REFRESH_INTERVAL_MS 1500
#define ANIMATION_INTERVAL_MS 16
#define RUNNING_PULSE_PERIOD_MS 1600
#define WM_FETCH_DONE (WM_APP + 1)
#define MENU_EXIT_ID 1001
#define MENU_ABOUT_ID 1002
#define MENU_SIZE_BASE_ID 1100
#define MAX_SESSIONS 128
#define DOT_SIZE 14
#define DOT_GAP 8
#define DOT_EDGE_SAMPLES 4
#define PADDING_X 10
#define PADDING_Y 1
#define ROW_HEIGHT 26
#define DIRECTORY_TEXT_PADDING 8
#define COLUMN_GAP 12
#define MIN_PANEL_WIDTH 48
#define MIN_PANEL_HEIGHT 32
#define DEFAULT_DISPLAY_FONT_POINTS 9
#define SETTINGS_REGISTRY_PATH L"Software\\CodexMonitorWidget"
#define SETTINGS_VALUE_ANCHOR_RIGHT L"AnchorRight"
#define SETTINGS_VALUE_ANCHOR_BOTTOM L"AnchorBottom"
#define SETTINGS_VALUE_OFFSET_X L"OffsetX"
#define SETTINGS_VALUE_OFFSET_Y L"OffsetY"
#define SETTINGS_VALUE_DISPLAY_SIZE L"DisplaySize"
#define PROJECT_GITHUB_URL L"https://github.com/ZeroJehovah/api-alive"

typedef struct Session {
    int pid;
    double started_at;
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
    int original_order;
} DirectoryRow;

typedef struct AppState {
    HWND hwnd;
    HWND tooltip;
    HFONT font;
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
    int context_menu_open;
    int animation_timer_active;
    int anchor_right;
    int anchor_bottom;
    int placement_offset_x;
    int placement_offset_y;
    int display_font_points;
    POINT drag_start;
    RECT drag_window;
    LONG fetching;
    char last_error[256];
} AppState;

static const char STATUS_IDLE[] = "\xe6\x9c\xaa\xe8\xbf\x90\xe8\xa1\x8c";
static const char STATUS_RUNNING[] = "\xe8\xbf\x90\xe8\xa1\x8c\xe4\xb8\xad";
static const char STATUS_SUCCESS[] = "\xe6\x88\x90\xe5\x8a\x9f";
static const char STATUS_FAILED[] = "\xe5\xa4\xb1\xe8\xb4\xa5";
static const int DISPLAY_FONT_SIZES[] = {8, 9, 10, 11, 12, 14, 16};

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
    size_t length;
    if (target_count <= 0) {
        return;
    }
    if (source == NULL) {
        target[0] = '\0';
        return;
    }
    length = strlen(source);
    if (length >= (size_t)target_count) {
        length = (size_t)target_count - 1;
    }
    memcpy(target, source, length);
    target[length] = '\0';
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

static int display_size_count(void) {
    return (int)(sizeof(DISPLAY_FONT_SIZES) / sizeof(DISPLAY_FONT_SIZES[0]));
}

static int is_supported_display_font_points(int points) {
    int index;
    for (index = 0; index < display_size_count(); index++) {
        if (DISPLAY_FONT_SIZES[index] == points) {
            return 1;
        }
    }
    return 0;
}

static int normalized_display_font_points(int points) {
    if (is_supported_display_font_points(points)) {
        return points;
    }
    return DEFAULT_DISPLAY_FONT_POINTS;
}

static int scale_px(int value) {
    int points = normalized_display_font_points(g_app.display_font_points);
    int scaled = (value * points + DEFAULT_DISPLAY_FONT_POINTS / 2) / DEFAULT_DISPLAY_FONT_POINTS;
    if (scaled < 1) {
        return 1;
    }
    return scaled;
}

static int ui_dot_size(void) {
    return scale_px(DOT_SIZE);
}

static int ui_dot_gap(void) {
    return scale_px(DOT_GAP);
}

static int ui_dot_halo_expand(int pulse) {
    int base = scale_px(2);
    int extra = scale_px(2);
    return base + (pulse * extra + 50) / 100;
}

static int ui_padding_x(void) {
    return scale_px(PADDING_X);
}

static int ui_padding_y(void) {
    return scale_px(PADDING_Y);
}

static int ui_row_height(void) {
    int height = scale_px(ROW_HEIGHT);
    int min_height = ui_dot_size() + scale_px(4) * 2 + 2;
    if (height < min_height) {
        return min_height;
    }
    return height;
}

static int ui_directory_text_padding(void) {
    return scale_px(DIRECTORY_TEXT_PADDING);
}

static int ui_column_gap(void) {
    return scale_px(COLUMN_GAP);
}

static int ui_min_panel_width(void) {
    return scale_px(MIN_PANEL_WIDTH);
}

static int ui_min_panel_height(void) {
    return scale_px(MIN_PANEL_HEIGHT);
}

static HFONT create_display_font(int points) {
    HDC hdc = GetDC(NULL);
    int dpi_y = 96;
    HFONT font;
    if (hdc != NULL) {
        dpi_y = GetDeviceCaps(hdc, LOGPIXELSY);
        ReleaseDC(NULL, hdc);
    }
    font = CreateFontW(-MulDiv(points, dpi_y, 72), 0, 0, 0, FW_NORMAL,
        FALSE, FALSE, FALSE, DEFAULT_CHARSET, OUT_DEFAULT_PRECIS,
        CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY, DEFAULT_PITCH | FF_DONTCARE,
        L"Segoe UI");
    return font;
}

static HFONT widget_font(void) {
    if (g_app.font != NULL) {
        return g_app.font;
    }
    return (HFONT)GetStockObject(DEFAULT_GUI_FONT);
}

static void update_display_font(void) {
    HFONT font;
    g_app.display_font_points = normalized_display_font_points(g_app.display_font_points);
    font = create_display_font(g_app.display_font_points);
    if (font == NULL) {
        return;
    }
    if (g_app.font != NULL) {
        DeleteObject(g_app.font);
    }
    g_app.font = font;
}

static int row_dot_width(int count) {
    if (count <= 0) {
        return 0;
    }
    return count * ui_dot_size() + (count - 1) * ui_dot_gap();
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
    return ui_padding_x() + g_app.directory_column_width + ui_column_gap();
}

static int panel_width(void) {
    int width;
    if (g_app.row_count <= 0) {
        return ui_min_panel_width();
    }
    width = ui_padding_x() * 2 + g_app.directory_column_width + ui_column_gap() + row_dot_width(max_row_session_count());
    if (width < ui_min_panel_width()) {
        return ui_min_panel_width();
    }
    return width;
}

static int panel_height(void) {
    int height;
    if (g_app.row_count <= 0) {
        return ui_min_panel_height();
    }
    height = ui_padding_y() * 2 + g_app.row_count * ui_row_height();
    if (height < ui_min_panel_height()) {
        return ui_min_panel_height();
    }
    return height;
}

static int rect_width(const RECT *rect) {
    return rect->right - rect->left;
}

static int rect_height(const RECT *rect) {
    return rect->bottom - rect->top;
}

static void get_primary_work_area(RECT *work_area) {
    if (!SystemParametersInfoW(SPI_GETWORKAREA, 0, work_area, 0)) {
        work_area->left = 0;
        work_area->top = 0;
        work_area->right = GetSystemMetrics(SM_CXSCREEN);
        work_area->bottom = GetSystemMetrics(SM_CYSCREEN);
    }
}

static void get_work_area_for_rect(const RECT *rect, RECT *work_area) {
    HMONITOR monitor;
    MONITORINFO info;
    if (rect == NULL) {
        get_primary_work_area(work_area);
        return;
    }
    monitor = MonitorFromRect(rect, MONITOR_DEFAULTTONEAREST);
    ZeroMemory(&info, sizeof(info));
    info.cbSize = sizeof(info);
    if (monitor != NULL && GetMonitorInfoW(monitor, &info)) {
        *work_area = info.rcWork;
        return;
    }
    get_primary_work_area(work_area);
}

static void clamp_panel_size_to_work_area(const RECT *work_area, int *width, int *height) {
    int work_width = rect_width(work_area);
    int work_height = rect_height(work_area);
    if (work_width < 1) {
        work_width = 1;
    }
    if (work_height < 1) {
        work_height = 1;
    }
    if (*width > work_width) {
        *width = work_width;
    }
    if (*height > work_height) {
        *height = work_height;
    }
    if (*width < 1) {
        *width = 1;
    }
    if (*height < 1) {
        *height = 1;
    }
}

static void update_placement_offsets_from_rect(const RECT *rect, const RECT *work_area) {
    if (g_app.anchor_right) {
        g_app.placement_offset_x = work_area->right - rect->right;
    } else {
        g_app.placement_offset_x = rect->left - work_area->left;
    }
    if (g_app.anchor_bottom) {
        g_app.placement_offset_y = work_area->bottom - rect->bottom;
    } else {
        g_app.placement_offset_y = rect->top - work_area->top;
    }
    if (g_app.placement_offset_x < 0) {
        g_app.placement_offset_x = 0;
    }
    if (g_app.placement_offset_y < 0) {
        g_app.placement_offset_y = 0;
    }
}

static void place_rect_from_current_placement(const RECT *work_area, int *width, int *height, RECT *target) {
    clamp_panel_size_to_work_area(work_area, width, height);
    if (g_app.placement_offset_x < 0) {
        g_app.placement_offset_x = 0;
    }
    if (g_app.placement_offset_y < 0) {
        g_app.placement_offset_y = 0;
    }

    if (g_app.anchor_right) {
        target->right = work_area->right - g_app.placement_offset_x;
        target->left = target->right - *width;
    } else {
        target->left = work_area->left + g_app.placement_offset_x;
        target->right = target->left + *width;
    }
    if (target->left < work_area->left) {
        target->left = work_area->left;
        target->right = target->left + *width;
        g_app.anchor_right = 0;
    }
    if (target->right > work_area->right) {
        target->right = work_area->right;
        target->left = target->right - *width;
        g_app.anchor_right = 1;
    }

    if (g_app.anchor_bottom) {
        target->bottom = work_area->bottom - g_app.placement_offset_y;
        target->top = target->bottom - *height;
    } else {
        target->top = work_area->top + g_app.placement_offset_y;
        target->bottom = target->top + *height;
    }
    if (target->top < work_area->top) {
        target->top = work_area->top;
        target->bottom = target->top + *height;
        g_app.anchor_bottom = 0;
    }
    if (target->bottom > work_area->bottom) {
        target->bottom = work_area->bottom;
        target->top = target->bottom - *height;
        g_app.anchor_bottom = 1;
    }

    update_placement_offsets_from_rect(target, work_area);
}

static void place_drag_rect(const RECT *desired, RECT *target, int *width, int *height) {
    RECT work_area;
    *width = rect_width(desired);
    *height = rect_height(desired);
    get_work_area_for_rect(desired, &work_area);
    clamp_panel_size_to_work_area(&work_area, width, height);

    *target = *desired;
    target->right = target->left + *width;
    target->bottom = target->top + *height;
    g_app.anchor_right = 0;
    g_app.anchor_bottom = 0;

    if (target->left < work_area.left) {
        target->left = work_area.left;
        target->right = target->left + *width;
    }
    if (target->right > work_area.right) {
        target->right = work_area.right;
        target->left = target->right - *width;
        g_app.anchor_right = 1;
    }
    if (target->top < work_area.top) {
        target->top = work_area.top;
        target->bottom = target->top + *height;
    }
    if (target->bottom > work_area.bottom) {
        target->bottom = work_area.bottom;
        target->top = target->bottom - *height;
        g_app.anchor_bottom = 1;
    }

    update_placement_offsets_from_rect(target, &work_area);
}

static int read_registry_dword(HKEY key, const wchar_t *name, int *target) {
    DWORD type = 0;
    DWORD data = 0;
    DWORD data_size = sizeof(data);
    if (RegQueryValueExW(key, name, NULL, &type, (LPBYTE)&data, &data_size) != ERROR_SUCCESS ||
        type != REG_DWORD || data_size != sizeof(data)) {
        return 0;
    }
    if (data > 1000000U) {
        return 0;
    }
    *target = (int)data;
    return 1;
}

static void write_registry_dword(HKEY key, const wchar_t *name, int value) {
    DWORD data = (DWORD)(value < 0 ? 0 : value);
    RegSetValueExW(key, name, 0, REG_DWORD, (const BYTE *)&data, sizeof(data));
}

static void set_default_widget_placement(const RECT *work_area) {
    g_app.anchor_right = 0;
    g_app.anchor_bottom = 0;
    g_app.placement_offset_x = rect_width(work_area) - ui_min_panel_width() - 24;
    g_app.placement_offset_y = 80;
    if (g_app.placement_offset_x < 0) {
        g_app.placement_offset_x = 0;
    }
    if (g_app.placement_offset_y < 0) {
        g_app.placement_offset_y = 0;
    }
}

static void load_widget_placement(void) {
    HKEY key;
    int value;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, SETTINGS_REGISTRY_PATH, 0, KEY_READ, &key) != ERROR_SUCCESS) {
        return;
    }
    if (read_registry_dword(key, SETTINGS_VALUE_ANCHOR_RIGHT, &value)) {
        g_app.anchor_right = value != 0;
    }
    if (read_registry_dword(key, SETTINGS_VALUE_ANCHOR_BOTTOM, &value)) {
        g_app.anchor_bottom = value != 0;
    }
    if (read_registry_dword(key, SETTINGS_VALUE_OFFSET_X, &value)) {
        g_app.placement_offset_x = value;
    }
    if (read_registry_dword(key, SETTINGS_VALUE_OFFSET_Y, &value)) {
        g_app.placement_offset_y = value;
    }
    if (read_registry_dword(key, SETTINGS_VALUE_DISPLAY_SIZE, &value)) {
        g_app.display_font_points = normalized_display_font_points(value);
    }
    RegCloseKey(key);
}

static void save_widget_placement(void) {
    HKEY key;
    if (RegCreateKeyExW(HKEY_CURRENT_USER, SETTINGS_REGISTRY_PATH, 0, NULL, 0,
            KEY_WRITE, NULL, &key, NULL) != ERROR_SUCCESS) {
        return;
    }
    write_registry_dword(key, SETTINGS_VALUE_ANCHOR_RIGHT, g_app.anchor_right);
    write_registry_dword(key, SETTINGS_VALUE_ANCHOR_BOTTOM, g_app.anchor_bottom);
    write_registry_dword(key, SETTINGS_VALUE_OFFSET_X, g_app.placement_offset_x);
    write_registry_dword(key, SETTINGS_VALUE_OFFSET_Y, g_app.placement_offset_y);
    write_registry_dword(key, SETTINGS_VALUE_DISPLAY_SIZE, g_app.display_font_points);
    RegCloseKey(key);
}

static RECT dot_rect(int row_index, int dot_index) {
    RECT rect;
    int dot_size = ui_dot_size();
    rect.left = dot_column_left() + dot_index * (dot_size + ui_dot_gap());
    rect.top = ui_padding_y() + row_index * ui_row_height() + (ui_row_height() - dot_size) / 2;
    rect.right = rect.left + dot_size;
    rect.bottom = rect.top + dot_size;
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
    old_font = SelectObject(hdc, widget_font());
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
    g_app.directory_column_width = max_width + ui_directory_text_padding();
}

static int compare_session_indexes(int left_index, int right_index) {
    Session *left = &g_app.sessions[left_index];
    Session *right = &g_app.sessions[right_index];
    if (left->started_at > 0.0 && right->started_at > 0.0) {
        if (left->started_at < right->started_at) {
            return -1;
        }
        if (left->started_at > right->started_at) {
            return 1;
        }
    } else if (left->started_at > 0.0) {
        return -1;
    } else if (right->started_at > 0.0) {
        return 1;
    } else if (left->started_at_iso[0] != '\0' && right->started_at_iso[0] != '\0') {
        int iso_compare = strcmp(left->started_at_iso, right->started_at_iso);
        if (iso_compare != 0) {
            return iso_compare;
        }
    } else if (left->started_at_iso[0] != '\0') {
        return -1;
    } else if (right->started_at_iso[0] != '\0') {
        return 1;
    }
    if (left_index < right_index) {
        return -1;
    }
    if (left_index > right_index) {
        return 1;
    }
    return 0;
}

static void sort_row_sessions(DirectoryRow *row) {
    int index;
    for (index = 1; index < row->session_count; index++) {
        int value = row->session_indexes[index];
        int insert = index - 1;
        while (insert >= 0 && compare_session_indexes(value, row->session_indexes[insert]) < 0) {
            row->session_indexes[insert + 1] = row->session_indexes[insert];
            insert--;
        }
        row->session_indexes[insert + 1] = value;
    }
}

static double row_started_at_sort_key(const DirectoryRow *row) {
    int index;
    double earliest = DBL_MAX;
    for (index = 0; index < row->session_count; index++) {
        double started_at = g_app.sessions[row->session_indexes[index]].started_at;
        if (started_at > 0.0 && started_at < earliest) {
            earliest = started_at;
        }
    }
    return earliest;
}

static const char *row_started_at_iso_sort_key(const DirectoryRow *row) {
    int index;
    const char *earliest = NULL;
    for (index = 0; index < row->session_count; index++) {
        const char *started_at_iso = g_app.sessions[row->session_indexes[index]].started_at_iso;
        if (started_at_iso[0] != '\0' && (earliest == NULL || strcmp(started_at_iso, earliest) < 0)) {
            earliest = started_at_iso;
        }
    }
    return earliest;
}

static int compare_directory_rows(const DirectoryRow *left, const DirectoryRow *right) {
    double left_started_at = row_started_at_sort_key(left);
    double right_started_at = row_started_at_sort_key(right);
    if (left_started_at < DBL_MAX && right_started_at < DBL_MAX) {
        if (left_started_at < right_started_at) {
            return -1;
        }
        if (left_started_at > right_started_at) {
            return 1;
        }
    } else if (left_started_at < DBL_MAX) {
        return -1;
    } else if (right_started_at < DBL_MAX) {
        return 1;
    } else {
        const char *left_iso = row_started_at_iso_sort_key(left);
        const char *right_iso = row_started_at_iso_sort_key(right);
        if (left_iso != NULL && right_iso != NULL) {
            int iso_compare = strcmp(left_iso, right_iso);
            if (iso_compare != 0) {
                return iso_compare;
            }
        } else if (left_iso != NULL) {
            return -1;
        } else if (right_iso != NULL) {
            return 1;
        }
    }
    if (left->original_order < right->original_order) {
        return -1;
    }
    if (left->original_order > right->original_order) {
        return 1;
    }
    return strcmp(left->directory, right->directory);
}

static void sort_directory_rows(void) {
    int row;
    for (row = 0; row < g_app.row_count; row++) {
        sort_row_sessions(&g_app.rows[row]);
    }
    for (row = 1; row < g_app.row_count; row++) {
        DirectoryRow value = g_app.rows[row];
        int insert = row - 1;
        while (insert >= 0 && compare_directory_rows(&value, &g_app.rows[insert]) < 0) {
            g_app.rows[insert + 1] = g_app.rows[insert];
            insert--;
        }
        g_app.rows[insert + 1] = value;
    }
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
            g_app.rows[target_row].original_order = target_row;
        }
        if (g_app.rows[target_row].session_count < MAX_SESSIONS) {
            g_app.rows[target_row].session_indexes[g_app.rows[target_row].session_count++] = index;
        }
    }
    sort_directory_rows();
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

static double parse_json_double(const char *value, const char *end) {
    char buffer[64];
    int length = 0;
    const char *p = value;
    if (p == NULL) {
        return 0.0;
    }
    while (p < end &&
        (isdigit((unsigned char)*p) || *p == '-' || *p == '+' || *p == '.' || *p == 'e' || *p == 'E') &&
        length < (int)sizeof(buffer) - 1) {
        buffer[length++] = *p++;
    }
    buffer[length] = '\0';
    return strtod(buffer, NULL);
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
    session->started_at = parse_json_double(find_top_level_key(start, end, "started_at"), end);
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
    RECT work_area;
    RECT target;
    int width = panel_width();
    int height = panel_height();
    int old_anchor_right = g_app.anchor_right;
    int old_anchor_bottom = g_app.anchor_bottom;
    int old_offset_x = g_app.placement_offset_x;
    int old_offset_y = g_app.placement_offset_y;
    HWND insert_after = HWND_TOPMOST;
    UINT flags = SWP_NOACTIVATE;
    GetWindowRect(g_app.hwnd, &rect);
    get_work_area_for_rect(&rect, &work_area);
    place_rect_from_current_placement(&work_area, &width, &height, &target);
    if (g_app.context_menu_open) {
        insert_after = NULL;
        flags |= SWP_NOZORDER;
    }
    SetWindowPos(g_app.hwnd, insert_after, target.left, target.top, width, height, flags);
    if (old_anchor_right != g_app.anchor_right ||
        old_anchor_bottom != g_app.anchor_bottom ||
        old_offset_x != g_app.placement_offset_x ||
        old_offset_y != g_app.placement_offset_y) {
        save_widget_placement();
    }
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

static void restore_widget_topmost(HWND hwnd) {
    if (!IsWindow(hwnd)) {
        return;
    }
    SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER);
}

static UINT display_size_command_id(int index) {
    return MENU_SIZE_BASE_ID + (UINT)index;
}

static int display_size_from_command(UINT command, int *points) {
    int index;
    if (command < MENU_SIZE_BASE_ID) {
        return 0;
    }
    index = (int)(command - MENU_SIZE_BASE_ID);
    if (index < 0 || index >= display_size_count()) {
        return 0;
    }
    *points = DISPLAY_FONT_SIZES[index];
    return 1;
}

static void append_display_size_menu(HMENU menu) {
    HMENU size_menu = CreatePopupMenu();
    int index;
    if (size_menu == NULL) {
        return;
    }
    for (index = 0; index < display_size_count(); index++) {
        wchar_t label[32];
        UINT flags = MF_STRING;
        _snwprintf(label, sizeof(label) / sizeof(label[0]) - 1, L"%d pt", DISPLAY_FONT_SIZES[index]);
        label[sizeof(label) / sizeof(label[0]) - 1] = L'\0';
        if (DISPLAY_FONT_SIZES[index] == g_app.display_font_points) {
            flags |= MF_CHECKED;
        }
        AppendMenuW(size_menu, flags, display_size_command_id(index), label);
    }
    if (!AppendMenuW(menu, MF_POPUP, (UINT_PTR)size_menu, L"\x663e\x793a\x5927\x5c0f")) {
        DestroyMenu(size_menu);
    }
}

static void apply_display_font_points(int points) {
    points = normalized_display_font_points(points);
    if (points == g_app.display_font_points) {
        return;
    }
    g_app.display_font_points = points;
    update_display_font();
    update_directory_column_width();
    refresh_widget_view();
    save_widget_placement();
}

static void open_about_page(HWND hwnd) {
    ShellExecuteW(hwnd, L"open", PROJECT_GITHUB_URL, NULL, NULL, SW_SHOWNORMAL);
}

static void show_context_menu(HWND hwnd, POINT point) {
    HMENU menu = CreatePopupMenu();
    UINT command;
    int selected_points;
    if (g_app.context_menu_open) {
        return;
    }
    if (menu == NULL) {
        return;
    }
    append_display_size_menu(menu);
    AppendMenuW(menu, MF_SEPARATOR, 0, NULL);
    AppendMenuW(menu, MF_STRING, MENU_ABOUT_ID, L"\x5173\x4e8e");
    AppendMenuW(menu, MF_STRING, MENU_EXIT_ID, L"\x9000\x51fa");

    if (g_app.tooltip != NULL) {
        SendMessageW(g_app.tooltip, TTM_POP, 0, 0);
    }
    g_app.context_menu_open = 1;
    SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER);
    SetForegroundWindow(hwnd);
    command = TrackPopupMenuEx(menu,
        TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY | TPM_WORKAREA,
        point.x, point.y, hwnd, NULL);
    DestroyMenu(menu);
    g_app.context_menu_open = 0;
    PostMessageW(hwnd, WM_NULL, 0, 0);
    if (command == MENU_EXIT_ID) {
        DestroyWindow(hwnd);
        return;
    }
    if (display_size_from_command(command, &selected_points)) {
        apply_display_font_points(selected_points);
        restore_widget_topmost(hwnd);
        return;
    }
    if (command == MENU_ABOUT_ID) {
        open_about_page(hwnd);
        restore_widget_topmost(hwnd);
        return;
    }
    restore_widget_topmost(hwnd);
}

static int blend_component(int foreground, int background, int alpha) {
    return (foreground * alpha + background * (255 - alpha) + 127) / 255;
}

static COLORREF blend_color(COLORREF foreground, COLORREF background, int alpha) {
    int red = blend_component(GetRValue(foreground), GetRValue(background), alpha);
    int green = blend_component(GetGValue(foreground), GetGValue(background), alpha);
    int blue = blend_component(GetBValue(foreground), GetBValue(background), alpha);
    return RGB(red, green, blue);
}

static void fill_dot(HDC hdc, const RECT *rect, COLORREF color, COLORREF background) {
    int width = rect->right - rect->left;
    int height = rect->bottom - rect->top;
    int diameter = width < height ? width : height;
    double center_x;
    double center_y;
    double radius;
    double radius_squared;
    int total_samples = DOT_EDGE_SAMPLES * DOT_EDGE_SAMPLES;
    int x;
    int y;
    if (width <= 0 || height <= 0 || diameter <= 0) {
        return;
    }
    center_x = rect->left + width / 2.0;
    center_y = rect->top + height / 2.0;
    radius = diameter / 2.0;
    radius_squared = radius * radius;
    for (y = rect->top; y < rect->bottom; y++) {
        for (x = rect->left; x < rect->right; x++) {
            int inside_samples = 0;
            int sample_y;
            for (sample_y = 0; sample_y < DOT_EDGE_SAMPLES; sample_y++) {
                int sample_x;
                double py = y + (sample_y + 0.5) / DOT_EDGE_SAMPLES;
                double dy = py - center_y;
                for (sample_x = 0; sample_x < DOT_EDGE_SAMPLES; sample_x++) {
                    double px = x + (sample_x + 0.5) / DOT_EDGE_SAMPLES;
                    double dx = px - center_x;
                    if (dx * dx + dy * dy <= radius_squared) {
                        inside_samples++;
                    }
                }
            }
            if (inside_samples == total_samples) {
                SetPixelV(hdc, x, y, color);
            } else if (inside_samples > 0) {
                int alpha = (inside_samples * 255 + total_samples / 2) / total_samples;
                SetPixelV(hdc, x, y, blend_color(color, background, alpha));
            }
        }
    }
}

static void draw_status_dot(HDC hdc, const RECT *rect, const char *status, COLORREF row_background) {
    if (is_running_status(status)) {
        int pulse = running_pulse_level();
        int expand = ui_dot_halo_expand(pulse);
        int inset = pulse >= 100 ? 0 : scale_px(1);
        RECT halo = *rect;
        RECT core = *rect;
        COLORREF halo_color = RGB(22 + pulse * 10 / 100, 50 + pulse * 35 / 100, 120 + pulse * 65 / 100);
        COLORREF core_color = RGB(37 + pulse * 38 / 100, 99 + pulse * 56 / 100, 235 + pulse * 20 / 100);
        InflateRect(&halo, expand, expand);
        InflateRect(&core, -inset, -inset);
        fill_dot(hdc, &halo, halo_color, row_background);
        fill_dot(hdc, &core, core_color, halo_color);
        return;
    }
    fill_dot(hdc, rect, status_color(status), row_background);
}

static void paint_widget(HWND hwnd, HDC hdc) {
    RECT client;
    HBRUSH background;
    HBRUSH row_background;
    HPEN border;
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
    SetBkMode(hdc, TRANSPARENT);
    SetTextColor(hdc, RGB(225, 225, 225));
    old_font = SelectObject(hdc, widget_font());
    for (row = 0; row < g_app.row_count; row++) {
        RECT row_rect;
        RECT text_rect;
        char display_name[512];
        wchar_t display_name_wide[512];
        int dot;
        COLORREF row_color = RGB(34, 34, 34);
        row_rect.left = 1;
        row_rect.top = ui_padding_y() + row * ui_row_height();
        row_rect.right = client.right - 1;
        row_rect.bottom = row_rect.top + ui_row_height();
        if (row % 2 == 1) {
            row_color = RGB(39, 39, 39);
            row_background = CreateSolidBrush(row_color);
            FillRect(hdc, &row_rect, row_background);
            DeleteObject(row_background);
        }
        directory_display_name(g_app.rows[row].directory, display_name, sizeof(display_name));
        utf8_to_wide(display_name, display_name_wide, (int)(sizeof(display_name_wide) / sizeof(display_name_wide[0])));
        text_rect.left = ui_padding_x();
        text_rect.top = row_rect.top;
        text_rect.right = ui_padding_x() + g_app.directory_column_width;
        text_rect.bottom = row_rect.bottom;
        DrawTextW(hdc, display_name_wide, -1, &text_rect,
            DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX);
        for (dot = 0; dot < g_app.rows[row].session_count; dot++) {
            int session_index = g_app.rows[row].session_indexes[dot];
            RECT rect = dot_rect(row, dot);
            draw_status_dot(hdc, &rect, g_app.sessions[session_index].status, row_color);
        }
    }
    SelectObject(hdc, old_font);
}

static void paint_widget_buffered(HWND hwnd, HDC target_hdc) {
    RECT client;
    HDC memory_hdc;
    HBITMAP bitmap;
    HGDIOBJ old_bitmap;
    GetClientRect(hwnd, &client);
    memory_hdc = CreateCompatibleDC(target_hdc);
    if (memory_hdc == NULL) {
        paint_widget(hwnd, target_hdc);
        return;
    }
    bitmap = CreateCompatibleBitmap(target_hdc, client.right - client.left, client.bottom - client.top);
    if (bitmap == NULL) {
        DeleteDC(memory_hdc);
        paint_widget(hwnd, target_hdc);
        return;
    }
    old_bitmap = SelectObject(memory_hdc, bitmap);
    paint_widget(hwnd, memory_hdc);
    BitBlt(target_hdc, client.left, client.top, client.right - client.left, client.bottom - client.top,
        memory_hdc, 0, 0, SRCCOPY);
    SelectObject(memory_hdc, old_bitmap);
    DeleteObject(bitmap);
    DeleteDC(memory_hdc);
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
    case WM_ERASEBKGND:
        return 1;
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
        paint_widget_buffered(hwnd, hdc);
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
            RECT desired = g_app.drag_window;
            RECT target;
            int width;
            int height;
            ClientToScreen(hwnd, &point);
            int dx = point.x - g_app.drag_start.x;
            int dy = point.y - g_app.drag_start.y;
            OffsetRect(&desired, dx, dy);
            place_drag_rect(&desired, &target, &width, &height);
            SetWindowPos(hwnd, HWND_TOPMOST, target.left, target.top, width, height,
                SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_NOSENDCHANGING);
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
            save_widget_placement();
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
            save_widget_placement();
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
        if (LOWORD(wparam) == MENU_ABOUT_ID) {
            open_about_page(hwnd);
            return 0;
        }
        {
            int selected_points;
            if (display_size_from_command((UINT)LOWORD(wparam), &selected_points)) {
                apply_display_font_points(selected_points);
                return 0;
            }
        }
        return DefWindowProcW(hwnd, message, wparam, lparam);
    case WM_SIZE:
        update_tool_rect();
        return 0;
    case WM_DESTROY:
        save_widget_placement();
        KillTimer(hwnd, REFRESH_TIMER_ID);
        KillTimer(hwnd, ANIMATION_TIMER_ID);
        if (g_app.font != NULL) {
            DeleteObject(g_app.font);
            g_app.font = NULL;
        }
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
    RECT initial_rect;
    int initial_width;
    int initial_height;
    MSG message;
    (void)previous_instance;
    (void)command_line;
    (void)show_command;

    ZeroMemory(&g_app, sizeof(g_app));
    g_app.display_font_points = DEFAULT_DISPLAY_FONT_POINTS;
    resolve_api_url();
    get_primary_work_area(&work_area);
    set_default_widget_placement(&work_area);
    load_widget_placement();
    update_display_font();
    ZeroMemory(&wc, sizeof(wc));
    wc.lpfnWndProc = window_proc;
    wc.hInstance = instance;
    wc.lpszClassName = APP_CLASS_NAME;
    wc.hCursor = LoadCursorW(NULL, IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
    if (!RegisterClassW(&wc)) {
        if (g_app.font != NULL) {
            DeleteObject(g_app.font);
            g_app.font = NULL;
        }
        return 1;
    }
    initial_width = panel_width();
    initial_height = panel_height();
    place_rect_from_current_placement(&work_area, &initial_width, &initial_height, &initial_rect);
    hwnd = CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW, APP_CLASS_NAME, L"Codex Monitor",
        WS_POPUP, initial_rect.left, initial_rect.top,
        initial_width, initial_height, NULL, NULL, instance, NULL);
    if (hwnd == NULL) {
        if (g_app.font != NULL) {
            DeleteObject(g_app.font);
            g_app.font = NULL;
        }
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
