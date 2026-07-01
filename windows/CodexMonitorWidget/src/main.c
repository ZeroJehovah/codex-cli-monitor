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
#define SINGLE_INSTANCE_MUTEX_NAME L"Local\\ZeroJehovah.CodexMonitorWidget.SingleInstance"
#define REFRESH_TIMER_ID 1
#define ANIMATION_TIMER_ID 2
#define REFRESH_INTERVAL_MS 500
#define EMPTY_RESULT_CONFIRMATIONS 1
#define ANIMATION_INTERVAL_MS 16
#define RUNNING_PULSE_PERIOD_MS 1200
#define EDGE_TUCK_ANIMATION_DURATION_MS 260
#define EDGE_TUCK_PROGRESS_MAX 1000
#define EDGE_TUCK_ATTACH_TOLERANCE 1
#define WM_FETCH_DONE (WM_APP + 1)
#define MENU_EXIT_ID 1001
#define MENU_ABOUT_ID 1002
#define MENU_EDGE_TUCK_ID 1003
#define MENU_SIZE_BASE_ID 1100
#define MAX_SESSIONS 128
#define DOT_SIZE 14
#define DOT_GAP 8
#define DOT_EDGE_SAMPLES 4
#define RUNNING_DOT_SOFT_EDGE 1
#define STATIC_DOT_SOFT_EDGE 2
#define RUNNING_SHADOW_SPREAD 6
#define ROW_HEIGHT 26
#define PANEL_PADDING_Y 1
#define MIN_PANEL_WIDTH 48
#define MIN_PANEL_HEIGHT 32
#define DEFAULT_DISPLAY_FONT_POINTS 9
#define SETTINGS_REGISTRY_PATH L"Software\\CodexMonitorWidget"
#define SETTINGS_VALUE_ANCHOR_RIGHT L"AnchorRight"
#define SETTINGS_VALUE_ANCHOR_BOTTOM L"AnchorBottom"
#define SETTINGS_VALUE_OFFSET_X L"OffsetX"
#define SETTINGS_VALUE_OFFSET_Y L"OffsetY"
#define SETTINGS_VALUE_DISPLAY_SIZE L"DisplaySize"
#define SETTINGS_VALUE_EDGE_TUCK_ENABLED L"EdgeTuckEnabled"
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

typedef struct GlyphVerticalMetrics {
    int black_box_y;
    int origin_y;
} GlyphVerticalMetrics;

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
    int mouse_inside;
    int mouse_tracking;
    int edge_tuck_target_collapsed;
    int edge_tuck_progress;
    int edge_tuck_enabled;
    int edge_tuck_side;
    DWORD edge_tuck_last_tick;
    int anchor_right;
    int anchor_bottom;
    int placement_offset_x;
    int placement_offset_y;
    int display_font_points;
    int display_wheel_delta;
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
static void update_animation_timer(void);
static void update_tool_rect(void);
static void resize_panel(void);
static void update_window_region(int actual_width, int visible_width, int height);
static void cancel_edge_tuck_delay(void);
static void schedule_edge_tuck_delay(void);
static void set_edge_tuck_target(int collapsed);
static void sync_edge_tuck_after_layout_change(void);
static int edge_tuck_side(void);
static int panel_width(void);
static int actual_panel_width(void);
static int directory_column_left(void);
static int rect_width(const RECT *rect);
static void finish_drag_move(void);
static void settle_dragged_window(void);

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

static int display_size_index_for_points(int points) {
    int index;
    points = normalized_display_font_points(points);
    for (index = 0; index < display_size_count(); index++) {
        if (DISPLAY_FONT_SIZES[index] == points) {
            return index;
        }
    }
    return 0;
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

static int ui_running_dot_soft_edge(void) {
    return scale_px(RUNNING_DOT_SOFT_EDGE);
}

static int ui_static_dot_soft_edge(void) {
    return scale_px(STATIC_DOT_SOFT_EDGE);
}

static int ui_running_shadow_spread(void) {
    return scale_px(RUNNING_SHADOW_SPREAD);
}

static int ui_dot_effect_padding(void) {
    return ui_running_shadow_spread();
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

static void identity_mat2(MAT2 *matrix) {
    ZeroMemory(matrix, sizeof(*matrix));
    matrix->eM11.value = 1;
    matrix->eM22.value = 1;
}

static int glyph_vertical_metrics(HDC hdc, GlyphVerticalMetrics *metrics) {
    MAT2 matrix;
    GLYPHMETRICS glyph;
    TEXTMETRICW text_metrics;
    DWORD result;
    if (metrics == NULL) {
        return 0;
    }
    identity_mat2(&matrix);
    result = GetGlyphOutlineW(hdc, L'o', GGO_METRICS, &glyph, 0, NULL, &matrix);
    if (result != GDI_ERROR && glyph.gmBlackBoxY > 0) {
        metrics->black_box_y = (int)glyph.gmBlackBoxY;
        metrics->origin_y = glyph.gmptGlyphOrigin.y;
        return 1;
    }
    if (GetTextMetricsW(hdc, &text_metrics)) {
        metrics->black_box_y = text_metrics.tmHeight;
        metrics->origin_y = text_metrics.tmAscent;
        return 1;
    }
    metrics->black_box_y = scale_px(15);
    metrics->origin_y = metrics->black_box_y;
    return 0;
}

static int ui_font_height(void) {
    HDC hdc;
    HGDIOBJ old_font;
    TEXTMETRICW metrics;
    int height = scale_px(15);
    hdc = GetDC(g_app.hwnd != NULL ? g_app.hwnd : NULL);
    if (hdc == NULL) {
        return height;
    }
    old_font = SelectObject(hdc, widget_font());
    if (GetTextMetricsW(hdc, &metrics)) {
        height = metrics.tmHeight;
    }
    SelectObject(hdc, old_font);
    ReleaseDC(g_app.hwnd != NULL ? g_app.hwnd : NULL, hdc);
    if (height < 1) {
        return 1;
    }
    return height;
}

static int ui_row_height(void) {
    int height = scale_px(ROW_HEIGHT);
    int content_margin = ui_dot_effect_padding();
    int min_dot_height = ui_dot_size() + content_margin * 2;
    int min_text_height = ui_font_height() + content_margin * 2;
    if (height < min_dot_height) {
        height = min_dot_height;
    }
    if (height < min_text_height) {
        height = min_text_height;
    }
    return height;
}

static int ui_panel_padding_y(void) {
    return scale_px(PANEL_PADDING_Y);
}

static int ui_row_top(int row_index) {
    return ui_panel_padding_y() + row_index * ui_row_height();
}

static int ui_dot_right_margin(void) {
    int margin = ui_panel_padding_y() + (ui_row_height() - ui_dot_size()) / 2;
    if (margin < 1) {
        return 1;
    }
    return margin;
}

static int ui_directory_left_margin(void) {
    return ui_dot_right_margin();
}

static int ui_text_dot_gap(void) {
    return ui_dot_right_margin();
}

static int edge_tuck_target_progress(void) {
    return g_app.edge_tuck_target_collapsed ? EDGE_TUCK_PROGRESS_MAX : 0;
}

static int edge_tuck_animating(void) {
    return g_app.edge_tuck_progress != edge_tuck_target_progress();
}

static int edge_tuck_eased_progress(void) {
    int progress = g_app.edge_tuck_progress;
    if (progress <= 0) {
        return 0;
    }
    if (progress >= EDGE_TUCK_PROGRESS_MAX) {
        return EDGE_TUCK_PROGRESS_MAX;
    }
    return (progress * progress * (3 * EDGE_TUCK_PROGRESS_MAX - 2 * progress) +
        (EDGE_TUCK_PROGRESS_MAX * EDGE_TUCK_PROGRESS_MAX) / 2) /
        (EDGE_TUCK_PROGRESS_MAX * EDGE_TUCK_PROGRESS_MAX);
}

static int interpolate_by_edge_tuck(int expanded, int collapsed) {
    int progress = edge_tuck_eased_progress();
    return (expanded * (EDGE_TUCK_PROGRESS_MAX - progress) +
        collapsed * progress + EDGE_TUCK_PROGRESS_MAX / 2) /
        EDGE_TUCK_PROGRESS_MAX;
}

static int current_directory_column_width(void) {
    return interpolate_by_edge_tuck(g_app.directory_column_width, 0);
}

static int current_text_dot_gap(void) {
    return interpolate_by_edge_tuck(ui_text_dot_gap(), 0);
}

static int directory_text_alpha(void) {
    int progress = edge_tuck_eased_progress();
    return 255 * (EDGE_TUCK_PROGRESS_MAX - progress) / EDGE_TUCK_PROGRESS_MAX;
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
    return directory_column_left() + current_directory_column_width() + current_text_dot_gap();
}

static int current_content_width(int max_sessions_in_row) {
    if (max_sessions_in_row <= 0) {
        return 0;
    }
    return ui_directory_left_margin() + current_directory_column_width() + current_text_dot_gap() +
        row_dot_width(max_sessions_in_row) + ui_dot_right_margin();
}

static int expanded_panel_width(void) {
    int width;
    int max_sessions_in_row;
    if (g_app.row_count <= 0) {
        return ui_min_panel_width();
    }
    max_sessions_in_row = max_row_session_count();
    width = ui_directory_left_margin() + g_app.directory_column_width + ui_text_dot_gap() +
        row_dot_width(max_sessions_in_row) + ui_dot_right_margin();
    if (width < ui_min_panel_width()) {
        return ui_min_panel_width();
    }
    return width;
}

static int content_origin_x(void) {
    int max_sessions_in_row;
    int width;
    int content_width;
    if (g_app.row_count <= 0) {
        return 0;
    }
    max_sessions_in_row = max_row_session_count();
    content_width = current_content_width(max_sessions_in_row);
    width = actual_panel_width();
    if (content_width >= width) {
        return 0;
    }
    if (edge_tuck_side() > 0) {
        return width - content_width;
    }
    return 0;
}

static int directory_column_left(void) {
    return content_origin_x() + ui_directory_left_margin();
}

static int current_min_panel_width(int max_sessions_in_row) {
    int collapsed_width;
    if (max_sessions_in_row <= 0) {
        return ui_min_panel_width();
    }
    collapsed_width = ui_directory_left_margin() +
        row_dot_width(max_sessions_in_row) + ui_dot_right_margin();
    if (collapsed_width < 1) {
        collapsed_width = 1;
    }
    return interpolate_by_edge_tuck(ui_min_panel_width(), collapsed_width);
}

static int panel_width(void) {
    int width;
    int max_sessions_in_row;
    if (g_app.row_count <= 0) {
        return ui_min_panel_width();
    }
    max_sessions_in_row = max_row_session_count();
    width = current_content_width(max_sessions_in_row);
    if (width < current_min_panel_width(max_sessions_in_row)) {
        return current_min_panel_width(max_sessions_in_row);
    }
    return width;
}

static int right_edge_tuck_region_active(void) {
    return edge_tuck_side() > 0 &&
        (g_app.edge_tuck_target_collapsed || g_app.edge_tuck_progress != 0);
}

static int actual_panel_width(void) {
    if (right_edge_tuck_region_active()) {
        return expanded_panel_width();
    }
    return panel_width();
}

static RECT visible_rect_from_rect(const RECT *rect) {
    RECT visible = *rect;
    if (right_edge_tuck_region_active()) {
        int visible_width = panel_width();
        int actual_width = rect_width(rect);
        if (visible_width < actual_width) {
            visible.left = visible.right - visible_width;
        }
    }
    return visible;
}

static int panel_height(void) {
    int height;
    if (g_app.row_count <= 0) {
        return ui_min_panel_height();
    }
    height = ui_panel_padding_y() * 2 + g_app.row_count * ui_row_height();
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
    if (read_registry_dword(key, SETTINGS_VALUE_EDGE_TUCK_ENABLED, &value)) {
        g_app.edge_tuck_enabled = value != 0;
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
    write_registry_dword(key, SETTINGS_VALUE_EDGE_TUCK_ENABLED, g_app.edge_tuck_enabled);
    RegCloseKey(key);
}

static int edge_tuck_side(void) {
    RECT rect;
    RECT work_area;
    if ((g_app.edge_tuck_target_collapsed || g_app.edge_tuck_progress != 0) &&
        g_app.edge_tuck_side != 0) {
        return g_app.edge_tuck_side;
    }
    if (g_app.hwnd == NULL) {
        return 0;
    }
    if (!GetWindowRect(g_app.hwnd, &rect)) {
        return 0;
    }
    get_work_area_for_rect(&rect, &work_area);
    if (rect.left <= work_area.left + EDGE_TUCK_ATTACH_TOLERANCE) {
        return -1;
    }
    if (rect.right >= work_area.right - EDGE_TUCK_ATTACH_TOLERANCE) {
        return 1;
    }
    return 0;
}

static int edge_tuck_available(void) {
    return g_app.edge_tuck_enabled && g_app.row_count > 0 && edge_tuck_side() != 0;
}

static int attach_horizontal_edge_anchor(void) {
    RECT rect;
    RECT work_area;
    int side;
    int width;
    int old_anchor_right;
    int old_offset_x;
    int old_offset_y;
    if (g_app.hwnd == NULL || !GetWindowRect(g_app.hwnd, &rect)) {
        return 0;
    }
    get_work_area_for_rect(&rect, &work_area);
    if (rect.left <= work_area.left + EDGE_TUCK_ATTACH_TOLERANCE) {
        side = -1;
    } else if (rect.right >= work_area.right - EDGE_TUCK_ATTACH_TOLERANCE) {
        side = 1;
    } else {
        return 0;
    }
    width = rect_width(&rect);
    old_anchor_right = g_app.anchor_right;
    old_offset_x = g_app.placement_offset_x;
    old_offset_y = g_app.placement_offset_y;
    if (side < 0) {
        g_app.anchor_right = 0;
        rect.left = work_area.left;
        rect.right = rect.left + width;
    } else {
        g_app.anchor_right = 1;
        rect.right = work_area.right;
        rect.left = rect.right - width;
    }
    g_app.edge_tuck_side = side;
    update_placement_offsets_from_rect(&rect, &work_area);
    if (old_anchor_right != g_app.anchor_right ||
        old_offset_x != g_app.placement_offset_x ||
        old_offset_y != g_app.placement_offset_y) {
        save_widget_placement();
    }
    return side;
}

static void cancel_edge_tuck_delay(void) {
    return;
}

static int cursor_inside_widget(void) {
    POINT point;
    RECT rect;
    RECT visible;
    if (g_app.hwnd == NULL) {
        return 0;
    }
    if (!GetCursorPos(&point) || !GetWindowRect(g_app.hwnd, &rect)) {
        return 0;
    }
    visible = visible_rect_from_rect(&rect);
    return PtInRect(&visible, point);
}

static void schedule_edge_tuck_delay(void) {
    if (g_app.hwnd == NULL ||
        g_app.dragging ||
        g_app.context_menu_open ||
        g_app.mouse_inside ||
        cursor_inside_widget() ||
        !edge_tuck_available()) {
        return;
    }
    set_edge_tuck_target(1);
}

static void set_edge_tuck_target(int collapsed) {
    int normalized = collapsed ? 1 : 0;
    if (normalized && !edge_tuck_available()) {
        normalized = 0;
    }
    if (normalized) {
        attach_horizontal_edge_anchor();
    }
    if (g_app.edge_tuck_target_collapsed == normalized) {
        return;
    }
    g_app.edge_tuck_target_collapsed = normalized;
    g_app.edge_tuck_last_tick = GetTickCount();
    update_animation_timer();
}

static void sync_edge_tuck_after_layout_change(void) {
    int old_progress = g_app.edge_tuck_progress;
    int old_target = g_app.edge_tuck_target_collapsed;
    if (!edge_tuck_available()) {
        cancel_edge_tuck_delay();
        g_app.edge_tuck_target_collapsed = 0;
        g_app.edge_tuck_progress = 0;
        g_app.edge_tuck_side = 0;
        if (old_progress != g_app.edge_tuck_progress ||
            old_target != g_app.edge_tuck_target_collapsed) {
            update_animation_timer();
            resize_panel();
            update_tool_rect();
            InvalidateRect(g_app.hwnd, NULL, FALSE);
        }
        return;
    }
    if (g_app.mouse_inside || g_app.dragging || g_app.context_menu_open) {
        cancel_edge_tuck_delay();
        set_edge_tuck_target(0);
    } else if (!g_app.edge_tuck_target_collapsed && g_app.edge_tuck_progress == 0) {
        schedule_edge_tuck_delay();
    }
}

static void track_mouse_leave(HWND hwnd) {
    TRACKMOUSEEVENT event;
    if (g_app.mouse_tracking) {
        return;
    }
    ZeroMemory(&event, sizeof(event));
    event.cbSize = sizeof(event);
    event.dwFlags = TME_LEAVE;
    event.hwndTrack = hwnd;
    if (TrackMouseEvent(&event)) {
        g_app.mouse_tracking = 1;
    }
}

static RECT dot_rect(int row_index, int dot_index) {
    RECT rect;
    int dot_size = ui_dot_size();
    rect.left = dot_column_left() + dot_index * (dot_size + ui_dot_gap());
    rect.top = ui_row_top(row_index) + (ui_row_height() - dot_size) / 2;
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
    g_app.directory_column_width = max_width;
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
        return RGB(245, 245, 245);
    }
    return RGB(245, 245, 245);
}

static int running_pulse_level(void) {
    DWORD elapsed = GetTickCount() % RUNNING_PULSE_PERIOD_MS;
    DWORD half_period = RUNNING_PULSE_PERIOD_MS / 2;
    int raw;
    if (elapsed > half_period) {
        elapsed = RUNNING_PULSE_PERIOD_MS - elapsed;
    }
    raw = (int)(elapsed * 100 / half_period);
    return (raw * raw * (300 - 2 * raw) + 5000) / 10000;
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

static void update_window_region(int actual_width, int visible_width, int height) {
    HRGN region;
    int region_left;
    if (g_app.hwnd == NULL) {
        return;
    }
    if (visible_width < 1) {
        visible_width = 1;
    }
    if (visible_width > actual_width) {
        visible_width = actual_width;
    }
    if (visible_width >= actual_width) {
        SetWindowRgn(g_app.hwnd, NULL, TRUE);
        return;
    }
    region_left = actual_width - visible_width;
    region = CreateRectRgn(region_left, 0, actual_width, height);
    if (region == NULL) {
        return;
    }
    if (SetWindowRgn(g_app.hwnd, region, TRUE) == 0) {
        DeleteObject(region);
    }
}

static void resize_panel(void) {
    RECT rect;
    RECT work_area;
    RECT target;
    int visible_width = panel_width();
    int width = actual_panel_width();
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
    update_window_region(width, visible_width, height);
    if ((!edge_tuck_animating() &&
            (old_anchor_right != g_app.anchor_right ||
                old_offset_x != g_app.placement_offset_x)) ||
        old_anchor_bottom != g_app.anchor_bottom ||
        old_offset_y != g_app.placement_offset_y) {
        save_widget_placement();
    }
}

static void advance_edge_tuck_animation(void) {
    DWORD now;
    DWORD elapsed;
    int target = edge_tuck_target_progress();
    int direction;
    int delta;
    if (!edge_tuck_animating()) {
        return;
    }
    now = GetTickCount();
    if (g_app.edge_tuck_last_tick == 0) {
        g_app.edge_tuck_last_tick = now;
    }
    elapsed = now - g_app.edge_tuck_last_tick;
    g_app.edge_tuck_last_tick = now;
    if (elapsed > EDGE_TUCK_ANIMATION_DURATION_MS) {
        elapsed = EDGE_TUCK_ANIMATION_DURATION_MS;
    }
    direction = target > g_app.edge_tuck_progress ? 1 : -1;
    delta = (int)(elapsed * EDGE_TUCK_PROGRESS_MAX / EDGE_TUCK_ANIMATION_DURATION_MS);
    if (delta < 1) {
        delta = 1;
    }
    g_app.edge_tuck_progress += direction * delta;
    if ((direction > 0 && g_app.edge_tuck_progress > target) ||
        (direction < 0 && g_app.edge_tuck_progress < target)) {
        g_app.edge_tuck_progress = target;
    }
    if (g_app.edge_tuck_progress == 0 && target == 0) {
        g_app.edge_tuck_side = 0;
    }
    resize_panel();
    update_tool_rect();
}

static void update_tool_rect(void) {
    RECT rect;
    GetClientRect(g_app.hwnd, &rect);
    g_app.tooltip_info.rect = rect;
    SendMessageW(g_app.tooltip, TTM_NEWTOOLRECTW, 0, (LPARAM)&g_app.tooltip_info);
}

static void update_animation_timer(void) {
    if (g_app.dragging) {
        if (g_app.animation_timer_active) {
            KillTimer(g_app.hwnd, ANIMATION_TIMER_ID);
            g_app.animation_timer_active = 0;
        }
        return;
    }
    if (has_running_sessions() || edge_tuck_animating()) {
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
    sync_edge_tuck_after_layout_change();
    resize_panel();
    update_tool_rect();
    update_animation_timer();
    InvalidateRect(g_app.hwnd, NULL, FALSE);
    set_tooltip_for_hover(g_app.hovered_session);
}

static void settle_dragged_window(void) {
    RECT rect;
    RECT work_area;
    RECT target;
    int width;
    int height;
    int old_anchor_right = g_app.anchor_right;
    int old_anchor_bottom = g_app.anchor_bottom;
    int old_offset_x = g_app.placement_offset_x;
    int old_offset_y = g_app.placement_offset_y;
    if (g_app.hwnd == NULL || !GetWindowRect(g_app.hwnd, &rect)) {
        return;
    }
    get_work_area_for_rect(&rect, &work_area);
    width = rect_width(&rect);
    height = rect_height(&rect);
    clamp_panel_size_to_work_area(&work_area, &width, &height);
    target = rect;
    target.right = target.left + width;
    target.bottom = target.top + height;

    g_app.anchor_right = 0;
    g_app.anchor_bottom = 0;
    if (target.right >= work_area.right - EDGE_TUCK_ATTACH_TOLERANCE) {
        target.right = work_area.right;
        target.left = target.right - width;
        g_app.anchor_right = 1;
    }
    if (target.left < work_area.left) {
        target.left = work_area.left;
        target.right = target.left + width;
        g_app.anchor_right = 0;
    }
    if (target.bottom >= work_area.bottom - EDGE_TUCK_ATTACH_TOLERANCE) {
        target.bottom = work_area.bottom;
        target.top = target.bottom - height;
        g_app.anchor_bottom = 1;
    }
    if (target.top < work_area.top) {
        target.top = work_area.top;
        target.bottom = target.top + height;
        g_app.anchor_bottom = 0;
    }

    update_placement_offsets_from_rect(&target, &work_area);
    if (target.left != rect.left || target.top != rect.top ||
        rect_width(&target) != rect_width(&rect) ||
        rect_height(&target) != rect_height(&rect)) {
        SetWindowPos(g_app.hwnd, HWND_TOPMOST, target.left, target.top,
            width, height, SWP_NOACTIVATE);
    } else if (old_anchor_right != g_app.anchor_right ||
        old_anchor_bottom != g_app.anchor_bottom ||
        old_offset_x != g_app.placement_offset_x ||
        old_offset_y != g_app.placement_offset_y) {
        update_tool_rect();
    }
}

static void finish_drag_move(void) {
    if (!g_app.dragging) {
        return;
    }
    g_app.dragging = 0;
    settle_dragged_window();
    save_widget_placement();
    if (g_app.drag_refresh_pending) {
        g_app.drag_refresh_pending = 0;
        refresh_widget_view();
    } else {
        sync_edge_tuck_after_layout_change();
        update_animation_timer();
    }
    start_fetch();
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
    set_edge_tuck_target(0);
    g_app.display_font_points = points;
    update_display_font();
    update_directory_column_width();
    refresh_widget_view();
    save_widget_placement();
}

static void apply_display_font_wheel_delta(int delta) {
    int current_index;
    int target_index;
    if (delta == 0) {
        return;
    }
    g_app.display_wheel_delta += delta;
    current_index = display_size_index_for_points(g_app.display_font_points);
    target_index = current_index;
    while (g_app.display_wheel_delta >= WHEEL_DELTA) {
        if (target_index < display_size_count() - 1) {
            target_index++;
        }
        g_app.display_wheel_delta -= WHEEL_DELTA;
    }
    while (g_app.display_wheel_delta <= -WHEEL_DELTA) {
        if (target_index > 0) {
            target_index--;
        }
        g_app.display_wheel_delta += WHEEL_DELTA;
    }
    if (target_index != current_index) {
        apply_display_font_points(DISPLAY_FONT_SIZES[target_index]);
    }
}

static void open_about_page(HWND hwnd) {
    ShellExecuteW(hwnd, L"open", PROJECT_GITHUB_URL, NULL, NULL, SW_SHOWNORMAL);
}

static void apply_edge_tuck_enabled(int enabled) {
    int normalized = enabled ? 1 : 0;
    if (g_app.edge_tuck_enabled == normalized) {
        return;
    }
    g_app.edge_tuck_enabled = normalized;
    cancel_edge_tuck_delay();
    set_edge_tuck_target(0);
    refresh_widget_view();
    save_widget_placement();
}

static void show_context_menu(HWND hwnd, POINT point) {
    HMENU menu = CreatePopupMenu();
    UINT command;
    int selected_points;
    UINT edge_tuck_flags = MF_STRING;
    if (g_app.context_menu_open) {
        return;
    }
    if (menu == NULL) {
        return;
    }
    append_display_size_menu(menu);
    if (g_app.edge_tuck_enabled) {
        edge_tuck_flags |= MF_CHECKED;
    }
    AppendMenuW(menu, edge_tuck_flags, MENU_EDGE_TUCK_ID, L"\x8d34\x8fb9\x6536\x7eb3");
    AppendMenuW(menu, MF_SEPARATOR, 0, NULL);
    AppendMenuW(menu, MF_STRING, MENU_ABOUT_ID, L"\x5173\x4e8e");
    AppendMenuW(menu, MF_STRING, MENU_EXIT_ID, L"\x9000\x51fa");

    if (g_app.tooltip != NULL) {
        SendMessageW(g_app.tooltip, TTM_POP, 0, 0);
    }
    set_edge_tuck_target(0);
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
    if (!g_app.mouse_inside && !cursor_inside_widget()) {
        schedule_edge_tuck_delay();
    }
    if (command == MENU_EXIT_ID) {
        DestroyWindow(hwnd);
        return;
    }
    if (command == MENU_EDGE_TUCK_ID) {
        apply_edge_tuck_enabled(!g_app.edge_tuck_enabled);
        restore_widget_topmost(hwnd);
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

static int clamp_int(int value, int minimum, int maximum) {
    if (value < minimum) {
        return minimum;
    }
    if (value > maximum) {
        return maximum;
    }
    return value;
}

static RECT centered_square_rect(const RECT *rect, int diameter) {
    RECT result;
    int center_x2 = rect->left + rect->right;
    int center_y2 = rect->top + rect->bottom;
    if (diameter < 1) {
        diameter = 1;
    }
    result.left = (center_x2 - diameter) / 2;
    result.top = (center_y2 - diameter) / 2;
    result.right = result.left + diameter;
    result.bottom = result.top + diameter;
    return result;
}

static void fill_soft_dot(
    HDC hdc,
    const RECT *rect,
    COLORREF color,
    COLORREF fallback_background,
    int max_alpha,
    int edge_blur,
    int composite_over_current
) {
    int width = rect->right - rect->left;
    int height = rect->bottom - rect->top;
    int diameter = width < height ? width : height;
    double center_x;
    double center_y;
    double radius;
    double outer_radius_squared;
    double inner_radius;
    double inner_radius_squared;
    double fade_denominator;
    int total_samples = DOT_EDGE_SAMPLES * DOT_EDGE_SAMPLES;
    int x;
    int y;
    if (width <= 0 || height <= 0 || diameter <= 0) {
        return;
    }
    max_alpha = clamp_int(max_alpha, 0, 255);
    if (max_alpha <= 0) {
        return;
    }
    edge_blur = clamp_int(edge_blur, 0, diameter);
    center_x = rect->left + width / 2.0;
    center_y = rect->top + height / 2.0;
    radius = diameter / 2.0;
    inner_radius = radius - edge_blur;
    if (inner_radius < 0.0) {
        inner_radius = 0.0;
    }
    outer_radius_squared = radius * radius;
    inner_radius_squared = inner_radius * inner_radius;
    fade_denominator = outer_radius_squared - inner_radius_squared;
    if (fade_denominator <= 0.0) {
        fade_denominator = outer_radius_squared;
    }
    for (y = rect->top; y < rect->bottom; y++) {
        for (x = rect->left; x < rect->right; x++) {
            int alpha_sum = 0;
            int sample_y;
            for (sample_y = 0; sample_y < DOT_EDGE_SAMPLES; sample_y++) {
                int sample_x;
                double py = y + (sample_y + 0.5) / DOT_EDGE_SAMPLES;
                double dy = py - center_y;
                for (sample_x = 0; sample_x < DOT_EDGE_SAMPLES; sample_x++) {
                    double px = x + (sample_x + 0.5) / DOT_EDGE_SAMPLES;
                    double dx = px - center_x;
                    double distance_squared = dx * dx + dy * dy;
                    if (distance_squared <= inner_radius_squared) {
                        alpha_sum += max_alpha;
                    } else if (distance_squared <= outer_radius_squared) {
                        double fade = (outer_radius_squared - distance_squared) / fade_denominator;
                        fade = fade * fade * (3.0 - 2.0 * fade);
                        alpha_sum += (int)(max_alpha * fade + 0.5);
                    }
                }
            }
            if (alpha_sum > 0) {
                int alpha = (alpha_sum + total_samples / 2) / total_samples;
                COLORREF background = fallback_background;
                if (composite_over_current) {
                    background = GetPixel(hdc, x, y);
                    if (background == CLR_INVALID) {
                        background = fallback_background;
                    }
                }
                SetPixelV(hdc, x, y, blend_color(color, background, alpha));
            }
        }
    }
}

static void fill_glow_dot(
    HDC hdc,
    const RECT *rect,
    COLORREF color,
    COLORREF fallback_background,
    int max_alpha
) {
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
    max_alpha = clamp_int(max_alpha, 0, 255);
    if (max_alpha <= 0) {
        return;
    }
    center_x = rect->left + width / 2.0;
    center_y = rect->top + height / 2.0;
    radius = diameter / 2.0;
    radius_squared = radius * radius;
    if (radius_squared <= 0.0) {
        return;
    }
    for (y = rect->top; y < rect->bottom; y++) {
        for (x = rect->left; x < rect->right; x++) {
            int alpha_sum = 0;
            int sample_y;
            for (sample_y = 0; sample_y < DOT_EDGE_SAMPLES; sample_y++) {
                int sample_x;
                double py = y + (sample_y + 0.5) / DOT_EDGE_SAMPLES;
                double dy = py - center_y;
                for (sample_x = 0; sample_x < DOT_EDGE_SAMPLES; sample_x++) {
                    double px = x + (sample_x + 0.5) / DOT_EDGE_SAMPLES;
                    double dx = px - center_x;
                    double distance_squared = dx * dx + dy * dy;
                    if (distance_squared <= radius_squared) {
                        double fade = (radius_squared - distance_squared) / radius_squared;
                        fade = fade * fade;
                        alpha_sum += (int)(max_alpha * fade + 0.5);
                    }
                }
            }
            if (alpha_sum > 0) {
                int alpha = (alpha_sum + total_samples / 2) / total_samples;
                COLORREF background = GetPixel(hdc, x, y);
                if (background == CLR_INVALID) {
                    background = fallback_background;
                }
                SetPixelV(hdc, x, y, blend_color(color, background, alpha));
            }
        }
    }
}

static void draw_status_dot(HDC hdc, const RECT *rect, const char *status, COLORREF row_background) {
    if (is_running_status(status)) {
        int pulse = running_pulse_level();
        int dot_size = ui_dot_size();
        int max_shadow_spread = ui_running_shadow_spread();
        int min_shadow_spread = max_shadow_spread / 2;
        int shadow_spread;
        int shadow_alpha = 56 + pulse * 120 / 100;
        int core_margin = scale_px(3);
        int core_growth = scale_px(3);
        int highlight_margin = scale_px(6);
        int highlight_growth = scale_px(4);
        int core_diameter = dot_size - core_margin +
            (pulse * core_growth + 50) / 100;
        int highlight_diameter = dot_size - highlight_margin +
            (pulse * highlight_growth + 50) / 100;
        int core_brightness = 55 + pulse * 150 / 100;
        int highlight_alpha = 72 + pulse * 112 / 100;
        int halo_diameter;
        RECT halo;
        RECT core;
        RECT highlight;
        COLORREF dim_blue = RGB(29, 78, 216);
        COLORREF running_blue = RGB(37, 99, 235);
        COLORREF bright_blue = RGB(96, 165, 250);
        COLORREF core_blue = blend_color(bright_blue, running_blue, core_brightness);
        if (min_shadow_spread < 1) {
            min_shadow_spread = 1;
        }
        if (core_diameter < 1) {
            core_diameter = 1;
        }
        if (highlight_diameter < 1) {
            highlight_diameter = 1;
        }
        shadow_spread = min_shadow_spread +
            (pulse * (max_shadow_spread - min_shadow_spread) + 50) / 100;
        halo_diameter = dot_size + shadow_spread * 2;
        halo = centered_square_rect(rect, halo_diameter);
        core = centered_square_rect(rect, core_diameter);
        highlight = centered_square_rect(rect, highlight_diameter);
        if (shadow_spread > 0) {
            fill_glow_dot(hdc, &halo, running_blue, row_background, shadow_alpha);
        }
        fill_soft_dot(hdc, rect, dim_blue, row_background, 255, ui_running_dot_soft_edge(), 1);
        fill_soft_dot(hdc, &core, core_blue, row_background, 255, ui_running_dot_soft_edge(), 1);
        fill_soft_dot(hdc, &highlight, bright_blue, row_background, highlight_alpha, ui_running_dot_soft_edge(), 1);
        return;
    }
    fill_soft_dot(hdc, rect, status_color(status), row_background, 255, ui_static_dot_soft_edge(), 0);
}

static void draw_directory_text(HDC hdc, const wchar_t *text, const RECT *rect, int row_top) {
    GlyphVerticalMetrics metrics;
    SIZE size;
    int length;
    int x;
    int baseline_y;
    UINT old_align;
    if (text == NULL || text[0] == L'\0' || rect == NULL) {
        return;
    }
    length = (int)wcslen(text);
    if (!GetTextExtentPoint32W(hdc, text, length, &size)) {
        return;
    }
    glyph_vertical_metrics(hdc, &metrics);
    x = rect->right - size.cx;
    if (x < rect->left) {
        x = rect->left;
    }
    baseline_y = row_top + (ui_row_height() - metrics.black_box_y) / 2 + metrics.origin_y;
    old_align = SetTextAlign(hdc, TA_LEFT | TA_BASELINE);
    ExtTextOutW(hdc, x, baseline_y, ETO_CLIPPED, rect, text, length, NULL);
    SetTextAlign(hdc, old_align);
}

static void paint_widget(HWND hwnd, HDC hdc) {
    RECT client;
    RECT visible_client;
    HBRUSH background;
    HBRUSH row_background;
    HPEN border;
    HGDIOBJ old_font;
    HGDIOBJ old_pen;
    int row;
    GetClientRect(hwnd, &client);
    visible_client = visible_rect_from_rect(&client);
    background = CreateSolidBrush(RGB(34, 34, 34));
    FillRect(hdc, &client, background);
    DeleteObject(background);
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
        row_rect.left = 0;
        row_rect.top = ui_row_top(row);
        row_rect.right = client.right;
        row_rect.bottom = row_rect.top + ui_row_height();
        if (row % 2 == 1) {
            row_color = RGB(39, 39, 39);
            row_background = CreateSolidBrush(row_color);
            FillRect(hdc, &row_rect, row_background);
            DeleteObject(row_background);
        }
        directory_display_name(g_app.rows[row].directory, display_name, sizeof(display_name));
        utf8_to_wide(display_name, display_name_wide, (int)(sizeof(display_name_wide) / sizeof(display_name_wide[0])));
        text_rect.left = directory_column_left();
        text_rect.top = row_rect.top;
        text_rect.right = text_rect.left + current_directory_column_width();
        text_rect.bottom = row_rect.bottom;
        if (text_rect.right > text_rect.left && directory_text_alpha() > 0) {
            SetTextColor(hdc, blend_color(RGB(225, 225, 225), row_color, directory_text_alpha()));
            draw_directory_text(hdc, display_name_wide, &text_rect, row_rect.top);
        }
        for (dot = 0; dot < g_app.rows[row].session_count; dot++) {
            int session_index = g_app.rows[row].session_indexes[dot];
            RECT rect = dot_rect(row, dot);
            draw_status_dot(hdc, &rect, g_app.sessions[session_index].status, row_color);
        }
    }
    SelectObject(hdc, old_font);
    border = CreatePen(PS_SOLID, 1, RGB(86, 86, 86));
    old_pen = SelectObject(hdc, border);
    SelectObject(hdc, GetStockObject(NULL_BRUSH));
    Rectangle(hdc, visible_client.left, visible_client.top, visible_client.right, visible_client.bottom);
    SelectObject(hdc, old_pen);
    DeleteObject(border);
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
            if (g_app.dragging) {
                g_app.drag_refresh_pending = 1;
            } else {
                start_fetch();
            }
        } else if (wparam == ANIMATION_TIMER_ID) {
            if (g_app.dragging) {
                update_animation_timer();
                return 0;
            }
            advance_edge_tuck_animation();
            InvalidateRect(hwnd, NULL, FALSE);
            update_animation_timer();
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
                    if (g_app.empty_success_count >= EMPTY_RESULT_CONFIRMATIONS) {
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
        cancel_edge_tuck_delay();
        set_edge_tuck_target(0);
        g_app.dragging = 1;
        g_app.drag_refresh_pending = 0;
        update_animation_timer();
        g_app.hovered_session = -1;
        set_tooltip_for_hover(-1);
        ReleaseCapture();
        SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION, GetMessagePos());
        finish_drag_move();
        return 0;
    case WM_MOUSEMOVE: {
        POINT point;
        int hovered;
        point.x = GET_X_LPARAM(lparam);
        point.y = GET_Y_LPARAM(lparam);
        if (!g_app.mouse_inside) {
            g_app.mouse_inside = 1;
        }
        track_mouse_leave(hwnd);
        cancel_edge_tuck_delay();
        set_edge_tuck_target(0);
        hovered = dot_at_point(point);
        if (hovered != g_app.hovered_session) {
            g_app.hovered_session = hovered;
            set_tooltip_for_hover(hovered);
        }
        return 0;
    }
    case WM_MOUSELEAVE:
        g_app.mouse_inside = 0;
        g_app.mouse_tracking = 0;
        g_app.hovered_session = -1;
        set_tooltip_for_hover(-1);
        schedule_edge_tuck_delay();
        return 0;
    case WM_LBUTTONUP:
        finish_drag_move();
        return 0;
    case WM_RBUTTONUP: {
        POINT point;
        if (g_app.dragging) {
            finish_drag_move();
        }
        cancel_edge_tuck_delay();
        set_edge_tuck_target(0);
        point.x = GET_X_LPARAM(lparam);
        point.y = GET_Y_LPARAM(lparam);
        ClientToScreen(hwnd, &point);
        show_context_menu(hwnd, point);
        return 0;
    }
    case WM_EXITSIZEMOVE:
        finish_drag_move();
        return 0;
    case WM_CONTEXTMENU: {
        POINT point;
        cancel_edge_tuck_delay();
        set_edge_tuck_target(0);
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
        if (LOWORD(wparam) == MENU_EDGE_TUCK_ID) {
            apply_edge_tuck_enabled(!g_app.edge_tuck_enabled);
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
    case WM_MOUSEWHEEL:
        cancel_edge_tuck_delay();
        set_edge_tuck_target(0);
        apply_display_font_wheel_delta(GET_WHEEL_DELTA_WPARAM(wparam));
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
    HANDLE single_instance_mutex;
    DWORD mutex_error;
    RECT work_area;
    RECT initial_rect;
    int initial_width;
    int initial_height;
    MSG message;
    (void)previous_instance;
    (void)command_line;
    (void)show_command;

    SetLastError(ERROR_SUCCESS);
    single_instance_mutex = CreateMutexW(NULL, TRUE, SINGLE_INSTANCE_MUTEX_NAME);
    mutex_error = GetLastError();
    if (single_instance_mutex == NULL) {
        return mutex_error == ERROR_ACCESS_DENIED ? 0 : 1;
    }
    if (mutex_error == ERROR_ALREADY_EXISTS) {
        CloseHandle(single_instance_mutex);
        return 0;
    }

    ZeroMemory(&g_app, sizeof(g_app));
    g_app.display_font_points = DEFAULT_DISPLAY_FONT_POINTS;
    g_app.edge_tuck_enabled = 1;
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
        CloseHandle(single_instance_mutex);
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
        CloseHandle(single_instance_mutex);
        return 1;
    }
    ShowWindow(hwnd, SW_SHOWNOACTIVATE);
    UpdateWindow(hwnd);
    while (GetMessageW(&message, NULL, 0, 0) > 0) {
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
    CloseHandle(single_instance_mutex);
    return (int)message.wParam;
}
