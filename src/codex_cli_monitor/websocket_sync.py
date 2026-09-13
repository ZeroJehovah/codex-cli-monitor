"""Synchronous WebSocket implementation for HTTP server integration."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
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
    
    def _read_frame(self, timeout: float | None = None) -> bytes | None:
        """Read one WebSocket frame from client (masked)."""
        old_timeout = self.sock.gettimeout()
        try:
            if timeout is not None:
                self.sock.settimeout(timeout)
            
            # Read first 2 bytes
            header = self.sock.recv(2)
            if len(header) < 2:
                return None
            
            fin_opcode = header[0]
            mask_len = header[1]
            
            opcode = fin_opcode & 0x0F
            masked = bool(mask_len & 0x80)
            payload_len = mask_len & 0x7F
            
            # Close frame
            if opcode == 0x8:
                return None
            
            # Extended payload length
            if payload_len == 126:
                ext = self.sock.recv(2)
                payload_len = struct.unpack('!H', ext)[0]
            elif payload_len == 127:
                ext = self.sock.recv(8)
                payload_len = struct.unpack('!Q', ext)[0]
            
            # Masking key (client to server)
            mask_key = self.sock.recv(4) if masked else None
            
            # Payload
            payload = bytearray()
            while len(payload) < payload_len:
                chunk = self.sock.recv(payload_len - len(payload))
                if not chunk:
                    return None
                payload.extend(chunk)
            
            # Unmask
            if masked and mask_key:
                payload = bytearray(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            
            return bytes(payload)
        except socket.timeout:
            return None
        except Exception:
            return None
        finally:
            self.sock.settimeout(old_timeout)
    
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
        self.sock.settimeout(30.0)
        try:
            while not self.closed:
                frame = self._read_frame(timeout=30.0)
                if frame is None:
                    break
                
                # Handle ping
                try:
                    msg = frame.decode('utf-8')
                    if msg == 'ping':
                        self.send_text('pong')
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self.close()


class WebSocketBroadcaster:
    """Thread-safe broadcaster for WebSocket clients."""
    
    def __init__(self) -> None:
        self.clients: set[SyncWebSocketClient] = set()
        self.last_state_hash: int | None = None
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
        remote_snapshots: tuple[RemoteSnapshot, ...] = (),
    ) -> None:
        """Broadcast state to all clients if changed."""
        state_hash = self._compute_state_hash(sessions, remote_snapshots)
        if state_hash == self.last_state_hash:
            return
        
        self.last_state_hash = state_hash
        
        from .aggregation import build_sessions_payload
        payload = build_sessions_payload(
            sessions,
            identity,
            remote_snapshots,
            time.time(),
        )
        message = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        
        with self._lock:
            clients = set(self.clients)
        
        dead = []
        for client in clients:
            try:
                if not client.closed:
                    client.send_text(message)
            except Exception:
                dead.append(client)
        
        if dead:
            with self._lock:
                self.clients -= set(dead)
    
    def _compute_state_hash(
        self,
        sessions: tuple[CodexSession, ...],
        remote_snapshots: tuple[RemoteSnapshot, ...],
    ) -> int:
        local_sig = tuple(
            (s.root.pid, s.display_status, s.root.cwd, s.waiting_reason)
            for s in sessions
        )
        remote_sig = tuple(
            (snap.identity.server_id, len(snap.sessions))
            for snap in remote_snapshots
        )
        return hash((local_sig, remote_sig))
