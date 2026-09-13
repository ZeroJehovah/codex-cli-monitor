"""Synchronous WebSocket implementation for HTTP server integration."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import queue
import socket
import struct
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .aggregation import RemoteSnapshot, ServerIdentity
    from .models import CodexSession

logger = logging.getLogger(__name__)


class SyncWebSocketClient:
    """Synchronous WebSocket client wrapper for raw socket."""
    
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.send_queue: queue.Queue[bytes | None] = queue.Queue()
        self.closed = False
        self._lock = threading.Lock()
    
    def send_text(self, text: str) -> None:
        """Queue text frame for sending."""
        if self.closed:
            return
        frame = self._encode_frame(text.encode('utf-8'), opcode=0x1)
        self.send_queue.put(frame)
    
    def close(self) -> None:
        """Close WebSocket connection."""
        with self._lock:
            if self.closed:
                return
            self.closed = True
        self.send_queue.put(None)  # Signal send loop to exit
        try:
            self.sock.close()
        except Exception:
            pass

    def send_control(self, opcode: int, payload: bytes = b"") -> None:
        """Queue a control frame such as ping or pong."""
        if len(payload) > 125:
            raise ValueError("WebSocket control frame payload is too large")
        if self.closed:
            return
        self.send_queue.put(self._encode_frame(payload, opcode=opcode))

    def send_pong(self, payload: bytes = b"") -> None:
        self.send_control(0xA, payload)
    
    def _encode_frame(self, payload: bytes, opcode: int = 0x1) -> bytes:
        """Encode WebSocket frame (server to client, no masking)."""
        frame = bytearray()
        frame.append(0x80 | opcode)  # FIN + opcode
        
        length = len(payload)
        if length < 126:
            frame.append(length)
        elif length < 65536:
            frame.append(126)
            frame.extend(struct.pack('!H', length))
        else:
            frame.append(127)
            frame.extend(struct.pack('!Q', length))
        
        frame.extend(payload)
        return bytes(frame)
    
    def _read_exact(self, length: int) -> bytes | None:
        data = bytearray()
        while len(data) < length:
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                return None
            data.extend(chunk)
        return bytes(data)

    def _read_frame(self, timeout: float | None = None) -> tuple[int, bytes] | None:
        """Read one WebSocket frame from client (masked)."""
        old_timeout = self.sock.gettimeout()
        try:
            if timeout is not None:
                self.sock.settimeout(timeout)

            header = self._read_exact(2)
            if header is None:
                return None
            fin_opcode = header[0]
            mask_len = header[1]
            fin = bool(fin_opcode & 0x80)
            opcode = fin_opcode & 0x0F
            masked = bool(mask_len & 0x80)
            payload_len = mask_len & 0x7F

            if payload_len == 126:
                ext = self._read_exact(2)
                if ext is None:
                    return None
                payload_len = struct.unpack('!H', ext)[0]
            elif payload_len == 127:
                ext = self._read_exact(8)
                if ext is None:
                    return None
                payload_len = struct.unpack('!Q', ext)[0]
            if payload_len > 1024 * 1024:
                return None
            mask_key = self._read_exact(4) if masked else None
            if masked and mask_key is None:
                return None
            raw_payload = self._read_exact(payload_len)
            if raw_payload is None:
                return None
            payload = bytearray(raw_payload)
            if masked and mask_key:
                payload = bytearray(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            if not masked and opcode < 0x8:
                # RFC 6455 requires client-to-server frames to be masked.
                return None
            if opcode >= 0x8 and (not fin or payload_len > 125):
                return None
            return opcode, bytes(payload)
        except socket.timeout:
            return None
        except Exception:
            return None
        finally:
            try:
                self.sock.settimeout(old_timeout)
            except Exception:
                pass
    
    def run_send_loop(self) -> None:
        """Send loop running in background thread."""
        try:
            while True:
                frame = self.send_queue.get()
                if frame is None:  # Close signal
                    break
                try:
                    self.sock.sendall(frame)
                except Exception:
                    break
        except Exception:
            pass
        finally:
            self.close()
    
    def run_recv_loop(self) -> None:
        """Receive loop (blocking until close)."""
        self.sock.settimeout(None)
        try:
            while not self.closed:
                frame = self._read_frame()
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:
                    self.send_control(0x8, payload[:125])
                    break
                if opcode == 0x9:
                    self.send_pong(payload)
        except Exception:
            pass
        finally:
            self.close()


class WebSocketBroadcaster:
    """Thread-safe broadcaster for WebSocket clients."""
    
    def __init__(self) -> None:
        self.clients: set[SyncWebSocketClient] = set()
        self._pending_sync: set[SyncWebSocketClient] = set()
        self.last_state_hash: int | None = None
        self._lock = threading.Lock()
    
    def register(
        self,
        client: SyncWebSocketClient,
        initial_message: str | None = None,
    ) -> None:
        with self._lock:
            self.clients.add(client)
            # Queue the initial snapshot while holding the same lock used by
            # broadcast().  This prevents a state change from being broadcast
            # between the initial snapshot and client registration, which
            # could otherwise leave a newly connected client permanently
            # behind until a later state change.
            if initial_message is not None:
                client.send_text(initial_message)
                self._pending_sync.add(client)
            logger.info(f"WebSocket client registered, total: {len(self.clients)}")
    
    def unregister(self, client: SyncWebSocketClient) -> None:
        with self._lock:
            self.clients.discard(client)
            self._pending_sync.discard(client)
            logger.info(f"WebSocket client unregistered, total: {len(self.clients)}")
    
    def broadcast(
        self,
        sessions: tuple[CodexSession, ...],
        identity: ServerIdentity,
        remote_snapshots: tuple[RemoteSnapshot, ...] = (),
    ) -> None:
        """Broadcast state to all clients if changed."""
        state_hash = self._compute_state_hash(sessions, remote_snapshots)
        with self._lock:
            state_changed = state_hash != self.last_state_hash
            self.last_state_hash = state_hash
            pending = set(self._pending_sync)
            self._pending_sync.clear()
            clients = set(self.clients)

        # A state update can race with the handler's initial snapshot before
        # it registers the client.  Even when the global state hash has not
        # changed since then, send the current state once to every newly
        # registered client so it cannot remain on that stale initial frame.
        if not state_changed and not pending:
            return
        
        from .aggregation import build_sessions_payload
        payload = build_sessions_payload(
            sessions,
            identity,
            remote_snapshots,
            time.time(),
        )
        message = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        
        dead = []
        for client in clients:
            try:
                if (state_changed or client in pending) and not client.closed:
                    client.send_text(message)
            except Exception:
                dead.append(client)
        
        if dead:
            with self._lock:
                self.clients -= set(dead)

    def ping(self) -> None:
        """Keep idle connections alive through intermediaries."""
        with self._lock:
            clients = tuple(self.clients)
        for client in clients:
            try:
                if not client.closed:
                    client.send_control(0x9)
            except Exception:
                self.unregister(client)
    
    def _compute_state_hash(
        self,
        sessions: tuple[CodexSession, ...],
        remote_snapshots: tuple[RemoteSnapshot, ...],
    ) -> int:
        local_sig = tuple(sorted(
            (s.root.pid, s.root.started_at, s.display_status, s.root.cwd,
             s.waiting_reason, getattr(s, "cli_type", "codex"))
            for s in sessions
        ))
        remote_sig = tuple(sorted(
            (
                snap.identity.server_id,
                snap.identity.server_name,
                tuple(sorted(
                    (
                        item.get("session_key"),
                        item.get("pid"),
                        item.get("started_at"),
                        item.get("status"),
                        item.get("directory"),
                        item.get("waiting_reason"),
                        item.get("cli_type", "codex"),
                    )
                    for item in snap.sessions
                )),
            )
            for snap in remote_snapshots
        ))
        return hash((local_sig, remote_sig))
