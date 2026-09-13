"""Bridge between synchronous HTTP handler and async WebSocket notifier."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import socket
import struct
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .websocket_server import StateChangeNotifier
    from .aggregation import RemoteSnapshot, ServerIdentity
    from .models import CodexSession

logger = logging.getLogger(__name__)


class SyncWebSocketClient:
    """Synchronous WebSocket client wrapper for HTTP-upgraded connections."""
    
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.send_queue: queue.Queue[bytes | None] = queue.Queue()
        self.closed = False
    
    def send_text(self, text: str) -> None:
        """Queue text frame for sending."""
        if not self.closed:
            self.send_queue.put(text.encode("utf-8"))
    
    def close(self) -> None:
        """Signal close."""
        self.closed = True
        self.send_queue.put(None)
    
    def _send_frame(self, payload: bytes) -> bool:
        """Send WebSocket text frame. Returns False on error."""
        try:
            frame = bytearray([0x81])  # FIN=1, opcode=1 (text)
            
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
            self.sock.sendall(bytes(frame))
            return True
        except Exception as e:
            logger.debug(f"Send frame error: {e}")
            return False
    
    def _read_frame(self, timeout: float = 30.0) -> bytes | None:
        """Read one WebSocket frame. Returns None on close/error."""
        try:
            self.sock.settimeout(timeout)
            header = self.sock.recv(2)
            if len(header) < 2:
                return None
            
            fin_opcode = header[0]
            masked_len = header[1]
            
            opcode = fin_opcode & 0x0F
            if opcode == 0x08:  # Close
                return None
            
            is_masked = (masked_len & 0x80) != 0
            payload_len = masked_len & 0x7F
            
            if payload_len == 126:
                ext_len = self.sock.recv(2)
                if len(ext_len) < 2:
                    return None
                payload_len = struct.unpack("!H", ext_len)[0]
            elif payload_len == 127:
                ext_len = self.sock.recv(8)
                if len(ext_len) < 8:
                    return None
                payload_len = struct.unpack("!Q", ext_len)[0]
            
            mask = None
            if is_masked:
                mask = self.sock.recv(4)
                if len(mask) < 4:
                    return None
            
            payload = b""
            while len(payload) < payload_len:
                chunk = self.sock.recv(min(4096, payload_len - len(payload)))
                if not chunk:
                    return None
                payload += chunk
            
            if is_masked and mask:
                payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
            
            return payload
        except socket.timeout:
            return b"__timeout__"
        except Exception:
            return None
    
    def run_send_loop(self) -> None:
        """Run send loop (call in sender thread)."""
        while True:
            try:
                payload = self.send_queue.get(timeout=1.0)
                if payload is None:
                    break
                if not self._send_frame(payload):
                    break
            except queue.Empty:
                continue
        
        try:
            self.sock.close()
        except Exception:
            pass
    
    def run_recv_loop(self) -> None:
        """Run receive loop for keepalive (call in receiver thread)."""
        while not self.closed:
            frame = self._read_frame(timeout=30.0)
            if frame is None:
                self.close()
                break
            if frame == b"__timeout__":
                continue
            if frame == b"ping":
                self.send_text("pong")


class WebSocketBroadcaster:
    """Bridge between async StateChangeNotifier and sync WebSocket clients."""
    
    def __init__(self):
        self.clients: set[SyncWebSocketClient] = set()
        self._lock = threading.Lock()
    
    def register(self, client: SyncWebSocketClient) -> None:
        with self._lock:
            self.clients.add(client)
            logger.info(f"WebSocket client registered, total: {len(self.clients)}")
    
    def unregister(self, client: SyncWebSocketClient) -> None:
        with self._lock:
            self.clients.discard(client)
            logger.info(f"WebSocket client unregistered, total: {len(self.clients)}")
    
    def broadcast(
        self,
        sessions: tuple[CodexSession, ...],
        identity: ServerIdentity,
        remote_snapshots: tuple[RemoteSnapshot, ...],
    ) -> None:
        """Broadcast state to all connected clients."""
        from .aggregation import build_sessions_payload
        
        payload = build_sessions_payload(
            sessions,
            observed_at=time.time(),
            identity=identity,
            remote_snapshots=remote_snapshots,
        )
        message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        
        with self._lock:
            clients = list(self.clients)
        
        dead_clients = []
        for client in clients:
            try:
                client.send_text(message)
            except Exception:
                dead_clients.append(client)
        
        if dead_clients:
            with self._lock:
                for client in dead_clients:
                    self.clients.discard(client)
