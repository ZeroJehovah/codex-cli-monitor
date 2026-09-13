#ifndef WEBSOCKET_H
#define WEBSOCKET_H

#include <windows.h>
#include <winhttp.h>

// Custom window messages for WebSocket events.
#define WM_WEBSOCKET_CONNECTED (WM_APP + 3)
#define WM_WEBSOCKET_MESSAGE (WM_APP + 4)
#define WM_WEBSOCKET_CLOSED (WM_APP + 5)

// Connect and authenticate on a worker thread. The thread handle is closed
// internally; the return value only reports whether it could be started.
int websocket_connect_async(HWND hwnd, const wchar_t *url, const wchar_t *token);

#endif // WEBSOCKET_H
