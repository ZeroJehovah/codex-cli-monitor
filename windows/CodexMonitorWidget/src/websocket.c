#include "websocket.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#define MAX_WS_MESSAGE_BYTES (4U * 1024U * 1024U)

typedef struct {
    HWND hwnd;
    wchar_t host[256];
    wchar_t path[768];
    wchar_t token[512];
    INTERNET_PORT port;
    int use_ssl;
} WebSocketThreadData;

static DWORD WINAPI websocket_thread_proc(LPVOID param);
static int parse_websocket_url(
    const wchar_t *url,
    wchar_t *host,
    size_t host_count,
    INTERNET_PORT *port,
    wchar_t *path,
    size_t path_count,
    int *use_ssl
);
static HINTERNET websocket_handshake(HINTERNET request, const wchar_t *token);
static void websocket_receive_loop(HINTERNET websocket, HWND hwnd);

static int append_json_escaped_utf8(
    char *output,
    size_t output_size,
    size_t *length,
    const char *value
) {
    const unsigned char *cursor = (const unsigned char *)value;
    while (*cursor != '\0') {
        const char *escape = NULL;
        char escaped[7];
        size_t escaped_length;
        if (*cursor == '\\') {
            escape = "\\\\";
        } else if (*cursor == '"') {
            escape = "\\\"";
        } else if (*cursor == '\b') {
            escape = "\\b";
        } else if (*cursor == '\f') {
            escape = "\\f";
        } else if (*cursor == '\n') {
            escape = "\\n";
        } else if (*cursor == '\r') {
            escape = "\\r";
        } else if (*cursor == '\t') {
            escape = "\\t";
        } else if (*cursor < 0x20) {
            snprintf(escaped, sizeof(escaped), "\\u%04x", *cursor);
            escape = escaped;
        }
        escaped_length = escape != NULL ? strlen(escape) : 1;
        if (escaped_length >= output_size || *length > output_size - escaped_length - 1) {
            return 0;
        }
        if (escape != NULL) {
            memcpy(output + *length, escape, escaped_length);
        } else {
            output[*length] = (char)*cursor;
        }
        *length += escaped_length;
        cursor++;
    }
    output[*length] = '\0';
    return 1;
}

int websocket_connect_async(HWND hwnd, const wchar_t *url, const wchar_t *token) {
    WebSocketThreadData *data;
    HANDLE thread;

    data = (WebSocketThreadData *)calloc(1, sizeof(*data));
    if (data == NULL) {
        return 0;
    }
    data->hwnd = hwnd;
    wcsncpy(data->token, token != NULL ? token : L"", 511);
    data->token[511] = L'\0';
    if (!parse_websocket_url(
            url,
            data->host,
            sizeof(data->host) / sizeof(data->host[0]),
            &data->port,
            data->path,
            sizeof(data->path) / sizeof(data->path[0]),
            &data->use_ssl)) {
        free(data);
        return 0;
    }
    thread = CreateThread(NULL, 0, websocket_thread_proc, data, 0, NULL);
    if (thread == NULL) {
        free(data);
        return 0;
    }
    CloseHandle(thread);
    return 1;
}

static int parse_websocket_url(
    const wchar_t *url,
    wchar_t *host,
    size_t host_count,
    INTERNET_PORT *port,
    wchar_t *path,
    size_t path_count,
    int *use_ssl
) {
    const wchar_t *cursor;
    const wchar_t *slash;
    const wchar_t *colon;
    size_t host_len;
    size_t port_len;
    wchar_t port_text[8];
    wchar_t *end_port;
    long parsed_port;

    if (wcsncmp(url, L"ws://", 5) == 0) {
        *use_ssl = 0;
        *port = 80;
        cursor = url + 5;
    } else if (wcsncmp(url, L"wss://", 6) == 0) {
        *use_ssl = 1;
        *port = 443;
        cursor = url + 6;
    } else {
        return 0;
    }

    slash = wcschr(cursor, L'/');
    colon = wcschr(cursor, L':');
    if (colon != NULL && (slash == NULL || colon < slash)) {
        host_len = (size_t)(colon - cursor);
        if (host_len == 0 || host_len >= host_count) {
            return 0;
        }
        wcsncpy(host, cursor, host_len);
        host[host_len] = L'\0';
        port_len = slash != NULL ? (size_t)(slash - (colon + 1)) : wcslen(colon + 1);
        if (port_len == 0 || port_len >= sizeof(port_text) / sizeof(port_text[0])) {
            return 0;
        }
        wcsncpy(port_text, colon + 1, port_len);
        port_text[port_len] = L'\0';
        parsed_port = wcstol(port_text, &end_port, 10);
        if (end_port == port_text || *end_port != L'\0' ||
            parsed_port < 1 || parsed_port > 65535) {
            return 0;
        }
        *port = (INTERNET_PORT)parsed_port;
        cursor = slash != NULL ? slash : colon + wcslen(colon);
    } else {
        host_len = slash != NULL ? (size_t)(slash - cursor) : wcslen(cursor);
        if (host_len == 0 || host_len >= host_count) {
            return 0;
        }
        wcsncpy(host, cursor, host_len);
        host[host_len] = L'\0';
        cursor = slash != NULL ? slash : cursor + host_len;
    }

    if (*cursor == L'\0') {
        if (path_count < 2) {
            return 0;
        }
        wcscpy(path, L"/");
    } else {
        if (wcslen(cursor) >= path_count) {
            return 0;
        }
        wcscpy(path, cursor);
    }
    return 1;
}

static DWORD WINAPI websocket_thread_proc(LPVOID param) {
    WebSocketThreadData *data = (WebSocketThreadData *)param;
    HINTERNET session = NULL;
    HINTERNET connection = NULL;
    HINTERNET request = NULL;
    HINTERNET websocket = NULL;

    session = WinHttpOpen(
        L"CodexMonitorWidget/1.0",
        WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
        WINHTTP_NO_PROXY_NAME,
        WINHTTP_NO_PROXY_BYPASS,
        0
    );
    if (session != NULL) {
        connection = WinHttpConnect(session, data->host, data->port, 0);
    }
    if (connection != NULL) {
        DWORD flags = WINHTTP_FLAG_REFRESH;
        if (data->use_ssl) {
            flags |= WINHTTP_FLAG_SECURE;
        }
        request = WinHttpOpenRequest(
            connection,
            L"GET",
            data->path,
            NULL,
            WINHTTP_NO_REFERER,
            WINHTTP_DEFAULT_ACCEPT_TYPES,
            flags
        );
    }
    if (request != NULL) {
        websocket = websocket_handshake(request, data->token);
        WinHttpCloseHandle(request);
        request = NULL;
    }
    if (websocket != NULL) {
        PostMessage(data->hwnd, WM_WEBSOCKET_CONNECTED, 0, 0);
        websocket_receive_loop(websocket, data->hwnd);
        WinHttpCloseHandle(websocket);
    }
    if (connection != NULL) {
        WinHttpCloseHandle(connection);
    }
    if (session != NULL) {
        WinHttpCloseHandle(session);
    }
    PostMessage(data->hwnd, WM_WEBSOCKET_CLOSED, 0, 0);
    free(data);
    return 0;
}

static HINTERNET websocket_handshake(HINTERNET request, const wchar_t *token) {
    DWORD status_code = 0;
    DWORD status_size = sizeof(status_code);
    HINTERNET websocket;

    if (!WinHttpSetOption(request, WINHTTP_OPTION_UPGRADE_TO_WEB_SOCKET, NULL, 0) ||
        !WinHttpSendRequest(
            request,
            WINHTTP_NO_ADDITIONAL_HEADERS,
            0,
            WINHTTP_NO_REQUEST_DATA,
            0,
            0,
            0) ||
        !WinHttpReceiveResponse(request, NULL) ||
        !WinHttpQueryHeaders(
            request,
            WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
            NULL,
            &status_code,
            &status_size,
            NULL) ||
        status_code != 101) {
        return NULL;
    }
    websocket = WinHttpWebSocketCompleteUpgrade(request, 0);
    if (websocket == NULL) {
        return NULL;
    }
    if (token != NULL && token[0] != L'\0') {
        char token_utf8[2048];
        char auth[2112];
        int token_length = WideCharToMultiByte(
            CP_UTF8,
            WC_ERR_INVALID_CHARS,
            token,
            -1,
            token_utf8,
            sizeof(token_utf8),
            NULL,
            NULL
        );
        size_t auth_length = 0;
        int auth_ok = 1;
        if (token_length <= 0) {
            WinHttpWebSocketClose(
                websocket,
                WINHTTP_WEB_SOCKET_INVALID_DATA_TYPE_CLOSE_STATUS,
                NULL,
                0
            );
            WinHttpCloseHandle(websocket);
            return NULL;
        }
        if (sizeof(auth) < 11) {
            auth_ok = 0;
        } else {
            memcpy(auth, "{\"token\":\"", 10);
            auth_length = 10;
            auth_ok = append_json_escaped_utf8(
                auth,
                sizeof(auth),
                &auth_length,
                token_utf8
            );
            if (auth_ok && auth_length > sizeof(auth) - 3) {
                auth_ok = 0;
            }
            if (auth_ok) {
                memcpy(auth + auth_length, "\"}", 2);
                auth_length += 2;
                auth[auth_length] = '\0';
            }
        }
        if (!auth_ok ||
            WinHttpWebSocketSend(
                websocket,
                WINHTTP_WEB_SOCKET_UTF8_MESSAGE_BUFFER_TYPE,
                auth,
                (DWORD)auth_length) != ERROR_SUCCESS) {
            WinHttpWebSocketClose(
                websocket,
                WINHTTP_WEB_SOCKET_ENDPOINT_TERMINATED_CLOSE_STATUS,
                NULL,
                0
            );
            WinHttpCloseHandle(websocket);
            return NULL;
        }
    }
    return websocket;
}

static void websocket_receive_loop(HINTERNET websocket, HWND hwnd) {
    char buffer[8192];
    char *message = NULL;
    size_t message_length = 0;

    for (;;) {
        DWORD bytes_read = 0;
        WINHTTP_WEB_SOCKET_BUFFER_TYPE buffer_type;
        DWORD error = WinHttpWebSocketReceive(
            websocket,
            buffer,
            sizeof(buffer),
            &bytes_read,
            &buffer_type
        );
        if (error != ERROR_SUCCESS || buffer_type == WINHTTP_WEB_SOCKET_CLOSE_BUFFER_TYPE) {
            break;
        }
        if (buffer_type != WINHTTP_WEB_SOCKET_UTF8_MESSAGE_BUFFER_TYPE &&
            buffer_type != WINHTTP_WEB_SOCKET_UTF8_FRAGMENT_BUFFER_TYPE) {
            continue;
        }
        if (bytes_read > MAX_WS_MESSAGE_BYTES - message_length) {
            break;
        }
        {
            char *grown = (char *)realloc(message, message_length + bytes_read + 1);
            if (grown == NULL) {
                break;
            }
            message = grown;
        }
        memcpy(message + message_length, buffer, bytes_read);
        message_length += bytes_read;
        message[message_length] = '\0';
        if (buffer_type == WINHTTP_WEB_SOCKET_UTF8_MESSAGE_BUFFER_TYPE) {
            char *copy = (char *)malloc(message_length + 1);
            if (copy != NULL) {
                memcpy(copy, message, message_length + 1);
                PostMessage(hwnd, WM_WEBSOCKET_MESSAGE, 0, (LPARAM)copy);
            }
            free(message);
            message = NULL;
            message_length = 0;
        }
    }
    free(message);
}
