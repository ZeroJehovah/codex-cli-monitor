#ifndef WEBSOCKET_H
#define WEBSOCKET_H

#include <windows.h>
#include <winhttp.h>

// Custom window messages for WebSocket events
#define WM_WEBSOCKET_CONNECTED (WM_APP + 3)
#define WM_WEBSOCKET_MESSAGE (WM_APP + 4)
#define WM_WEBSOCKET_CLOSED (WM_APP + 5)

// Connect to WebSocket server asynchronously
// Returns thread handle or NULL on failure
HANDLE websocket_connect_async(HWND hwnd, const wchar_t *url);

// Send ping frame
void websocket_send_ping(HINTERNET hWebSocket);

// Close WebSocket connection
void websocket_close(HINTERNET hWebSocket);

#endif // WEBSOCKET_H
