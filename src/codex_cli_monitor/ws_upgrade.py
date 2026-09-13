"""WebSocket upgrade handler for HTTP server integration."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import selectors
import socket
import struct
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler
    from .websocket_server import StateChangeNotifier

logger = logging.getLogger(__name__)


def compute_accept_key(sec_websocket_key: str) -> str:
    """Compute Sec-WebSocket-Accept per RFC 6455."""
    magic = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    sha1 = hashlib.sha1(sec_websocket_key.encode() + magic).digest()
    return base64.b64encode(sha1).decode()


def handle_websocket_upgrade(
    handler: BaseHTTPRequestHandler,
    notifier: StateChangeNotifier,
    api_token: str | None,
) -> None:
    """
    Handle WebSocket upgrade request on /ws path.
    
    Performs HTTP 101 upgrade handshake, then spawns thread for WebSocket handling.
    """
    sec_key = handler.headers.get("Sec-WebSocket-Key")
    if not sec_key:
        handler.send_response(400)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps({"error": "missing_sec_websocket_key"}).encode())
        return
    
    # Send 101 Switching Protocols
    accept_key = compute_accept_key(sec_key)
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept_key)
    handler.end_headers()
    
    # Detach socket from HTTP handler
    sock = handler.request
    handler.connection = None  # Prevent HTTP handler from closing socket
    
    # Spawn WebSocket handler thread
    ws_thread = threading.Thread(
        target=_websocket_handler_sync,
        args=(sock, notifier, api_token),
        daemon=True,
        name=f"ws-handler-{id(sock)}",
    )
    ws_thread.start()


def _websocket_handler_sync(
    sock: socket.socket,
    notifier: StateChangeNotifier,
    api_token: str | None,
) -> None:
    """Synchronous WebSocket frame handler (runs in dedicated thread)."""
    try:
        sock.settimeout(5.0)
        
        # Authentication if token required
        if api_token:
            try:
                frame = _read_frame(sock)
                if frame is None:
                    return
                auth_data = json.loads(frame.decode("utf-8"))
                if auth_data.get("token") != api_token:
                    _send_frame(sock, json.dumps({"error": "unauthorized"}).encode())
                    return
                _send_frame(sock, json.dumps({"ok": True}).encode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                _send_frame(sock, json.dumps({"error": "invalid_auth"}).encode())
                return
        
        # Send initial state (reuse notifier's broadcast mechanism)
        # TODO: Get initial state from state provider
        
        # Register with notifier (need async bridge)
        # For now, simple ping/pong loop
        sock.settimeout(30.0)
        while True:
            frame = _read_frame(sock)
            if frame is None:
                break
            if frame == b"ping":
                _send_frame(sock, b"pong")
    except Exception as e:
        logger.debug(f"WebSocket handler error: {e}")
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _read_frame(sock: socket.socket) -> bytes | None:
    """Read one WebSocket frame (simplified - text frames only)."""
    try:
        # Read frame header (2 bytes minimum)
        header = sock.recv(2)
        if len(header) < 2:
            return None
        
        fin_opcode = header[0]
        masked_len = header[1]
        
        opcode = fin_opcode & 0x0F
        if opcode == 0x08:  # Close frame
            return None
        
        is_masked = (masked_len & 0x80) != 0
        payload_len = masked_len & 0x7F
        
        # Extended payload length
        if payload_len == 126:
            ext_len = sock.recv(2)
            if len(ext_len) < 2:
                return None
            payload_len = struct.unpack("!H", ext_len)[0]
        elif payload_len == 127:
            ext_len = sock.recv(8)
            if len(ext_len) < 8:
                return None
            payload_len = struct.unpack("!Q", ext_len)[0]
        
        # Masking key
        mask = None
        if is_masked:
            mask = sock.recv(4)
            if len(mask) < 4:
                return None
        
        # Payload
        payload = b""
        while len(payload) < payload_len:
            chunk = sock.recv(payload_len - len(payload))
            if not chunk:
                return None
            payload += chunk
        
        # Unmask
        if is_masked and mask:
            payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
        
        return payload
    except Exception:
        return None


def _send_frame(sock: socket.socket, payload: bytes) -> None:
    """Send one WebSocket text frame."""
    try:
        frame = bytearray()
        frame.append(0x81)  # FIN=1, opcode=1 (text)
        
        payload_len = len(payload)
        if payload_len <= 125:
            frame.append(payload_len)
        elif payload_len <= 65535:
            frame.append(126)
            frame.extend(struct.pack("!H", payload_len))
        else:
            frame.append(127)
            frame.extend(struct.pack("!Q", payload_len))
        
        frame.extend(payload)
        sock.sendall(bytes(frame))
    except Exception:
        pass
