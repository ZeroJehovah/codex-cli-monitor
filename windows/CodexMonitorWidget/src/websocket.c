#include "websocket.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// WebSocket frame opcodes
#define WS_OPCODE_TEXT 0x1
#define WS_OPCODE_BINARY 0x2
#define WS_OPCODE_CLOSE 0x8
#define WS_OPCODE_PING 0x9
#define WS_OPCODE_PONG 0xA

// WebSocket connection thread data
typedef struct {
    HWND hwnd;
    wchar_t url[1024];
    wchar_t host[256];
    wchar_t path[768];
    int port;
    int use_ssl;
} WebSocketThreadData;

static DWORD WINAPI websocket_thread_proc(LPVOID param);
static int parse_websocket_url(const wchar_t *url, wchar_t *host, int *port, wchar_t *path, int *use_ssl);
static int websocket_handshake(HINTERNET hRequest, HWND hwnd);
static void websocket_receive_loop(HINTERNET hRequest, HWND hwnd);

HANDLE websocket_connect_async(HWND hwnd, const wchar_t *url) {
    WebSocketThreadData *data = (WebSocketThreadData *)malloc(sizeof(WebSocketThreadData));
    if (data == NULL) {
        return NULL;
    }
    
    data->hwnd = hwnd;
    wcsncpy(data->url, url, 1023);
    data->url[1023] = L'\0';
    
    if (!parse_websocket_url(url, data->host, &data->port, data->path, &data->use_ssl)) {
        free(data);
        return NULL;
    }
    
    HANDLE thread = CreateThread(NULL, 0, websocket_thread_proc, data, 0, NULL);
    if (thread == NULL) {
        free(data);
        return NULL;
    }
    
    return thread;
}

static int parse_websocket_url(const wchar_t *url, wchar_t *host, int *port, wchar_t *path, int *use_ssl) {
    // Parse ws://host:port/path or wss://host:port/path
    const wchar_t *p = url;
    
    if (wcsncmp(p, L"ws://", 5) == 0) {
        *use_ssl = 0;
        *port = 80;
        p += 5;
    } else if (wcsncmp(p, L"wss://", 6) == 0) {
        *use_ssl = 1;
        *port = 443;
        p += 6;
    } else {
        return 0;
    }
    
    // Extract host
    const wchar_t *slash = wcschr(p, L'/');
    const wchar_t *colon = wcschr(p, L':');
    
    if (colon != NULL && (slash == NULL || colon < slash)) {
        // Port specified
        size_t host_len = colon - p;
        if (host_len >= 255) {
            return 0;
        }
        wcsncpy(host, p, host_len);
        host[host_len] = L'\0';
        
        *port = _wtoi(colon + 1);
        p = slash ? slash : (colon + wcslen(colon));
    } else {
        // No port specified
        size_t host_len = slash ? (slash - p) : wcslen(p);
        if (host_len >= 255) {
            return 0;
        }
        wcsncpy(host, p, host_len);
        host[host_len] = L'\0';
        p = slash ? slash : (p + host_len);
    }
    
    // Extract path
    if (*p == L'\0') {
        wcscpy(path, L"/");
    } else {
        wcsncpy(path, p, 767);
        path[767] = L'\0';
    }
    
    return 1;
}

static DWORD WINAPI websocket_thread_proc(LPVOID param) {
    WebSocketThreadData *data = (WebSocketThreadData *)param;
    HWND hwnd = data->hwnd;
    
    // Open session
    HINTERNET hSession = WinHttpOpen(
        L"CodexMonitorWidget/1.0",
        WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
        WINHTTP_NO_PROXY_NAME,
        WINHTTP_NO_PROXY_BYPASS,
        0
    );
    
    if (hSession == NULL) {
        PostMessage(hwnd, WM_WEBSOCKET_CLOSED, 0, 0);
        free(data);
        return 1;
    }
    
    // Connect
    HINTERNET hConnect = WinHttpConnect(
        hSession,
        data->host,
        data->port,
        0
    );
    
    if (hConnect == NULL) {
        WinHttpCloseHandle(hSession);
        PostMessage(hwnd, WM_WEBSOCKET_CLOSED, 0, 0);
        free(data);
        return 1;
    }
    
    // Open request
    DWORD flags = WINHTTP_FLAG_REFRESH;
    if (data->use_ssl) {
        flags |= WINHTTP_FLAG_SECURE;
    }
    
    HINTERNET hRequest = WinHttpOpenRequest(
        hConnect,
        L"GET",
        data->path,
        NULL,
        WINHTTP_NO_REFERER,
        WINHTTP_DEFAULT_ACCEPT_TYPES,
        flags
    );
    
    if (hRequest == NULL) {
        WinHttpCloseHandle(hConnect);
        WinHttpCloseHandle(hSession);
        PostMessage(hwnd, WM_WEBSOCKET_CLOSED, 0, 0);
        free(data);
        return 1;
    }
    
    // Perform WebSocket handshake
    if (!websocket_handshake(hRequest, hwnd)) {
        WinHttpCloseHandle(hRequest);
        WinHttpCloseHandle(hConnect);
        WinHttpCloseHandle(hSession);
        PostMessage(hwnd, WM_WEBSOCKET_CLOSED, 0, 0);
        free(data);
        return 1;
    }
    
    PostMessage(hwnd, WM_WEBSOCKET_CONNECTED, 0, 0);
    
    // Receive loop
    websocket_receive_loop(hRequest, hwnd);
    
    // Cleanup
    WinHttpCloseHandle(hRequest);
    WinHttpCloseHandle(hConnect);
    WinHttpCloseHandle(hSession);
    PostMessage(hwnd, WM_WEBSOCKET_CLOSED, 0, 0);
    free(data);
    
    return 0;
}

static int websocket_handshake(HINTERNET hRequest, HWND hwnd) {
    // Set WebSocket upgrade headers
    WinHttpSetOption(hRequest, WINHTTP_OPTION_UPGRADE_TO_WEB_SOCKET, NULL, 0);
    
    if (!WinHttpSendRequest(hRequest, WINHTTP_NO_ADDITIONAL_HEADERS, 0, WINHTTP_NO_REQUEST_DATA, 0, 0, 0)) {
        return 0;
    }
    
    if (!WinHttpReceiveResponse(hRequest, NULL)) {
        return 0;
    }
    
    DWORD status_code = 0;
    DWORD size = sizeof(status_code);
    if (!WinHttpQueryHeaders(hRequest, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, NULL, &status_code, &size, NULL)) {
        return 0;
    }
    
    if (status_code != 101) {
        return 0;
    }
    
    return 1;
}

static void websocket_receive_loop(HINTERNET hRequest, HWND hwnd) {
    char buffer[8192];
    DWORD bytes_read = 0;
    WINHTTP_WEB_SOCKET_BUFFER_TYPE buffer_type;
    
    while (1) {
        DWORD error = WinHttpWebSocketReceive(
            hRequest,
            buffer,
            sizeof(buffer) - 1,
            &bytes_read,
            &buffer_type
        );
        
        if (error != ERROR_SUCCESS) {
            break;
        }
        
        if (buffer_type == WINHTTP_WEB_SOCKET_CLOSE_BUFFER_TYPE) {
            break;
        }
        
        if (buffer_type == WINHTTP_WEB_SOCKET_UTF8_MESSAGE_BUFFER_TYPE ||
            buffer_type == WINHTTP_WEB_SOCKET_UTF8_FRAGMENT_BUFFER_TYPE) {
            buffer[bytes_read] = '\0';
            
            // Allocate message buffer
            char *msg = (char *)malloc(bytes_read + 1);
            if (msg != NULL) {
                memcpy(msg, buffer, bytes_read + 1);
                PostMessage(hwnd, WM_WEBSOCKET_MESSAGE, 0, (LPARAM)msg);
            }
        }
    }
}

void websocket_send_ping(HINTERNET hWebSocket) {
    if (hWebSocket == NULL) {
        return;
    }
    
    WinHttpWebSocketSend(
        hWebSocket,
        WINHTTP_WEB_SOCKET_PING_BUFFER_TYPE,
        NULL,
        0
    );
}

void websocket_close(HINTERNET hWebSocket) {
    if (hWebSocket == NULL) {
        return;
    }
    
    WinHttpWebSocketClose(hWebSocket, WINHTTP_WEB_SOCKET_SUCCESS_CLOSE_STATUS, NULL, 0);
}
