from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import websockets
from websockets.server import WebSocketServerProtocol

from .aggregation import RemoteSnapshot, ServerIdentity, build_sessions_payload
from .models import CodexSession


logger = logging.getLogger(__name__)


class StateChangeNotifier:
    """WebSocket broadcast manager for real-time state updates."""

    def __init__(self) -> None:
        self.clients: set[WebSocketServerProtocol] = set()
        self.last_state_hash: int | None = None
        self._lock = threading.Lock()

    def register(self, websocket: WebSocketServerProtocol) -> None:
        with self._lock:
            self.clients.add(websocket)
            logger.info(f"WebSocket client connected, total clients: {len(self.clients)}")

    def unregister(self, websocket: WebSocketServerProtocol) -> None:
        with self._lock:
            self.clients.discard(websocket)
            logger.info(f"WebSocket client disconnected, total clients: {len(self.clients)}")

    async def broadcast(
        self,
        sessions: tuple[CodexSession, ...],
        identity: ServerIdentity,
        remote_snapshots: tuple[RemoteSnapshot, ...] = (),
    ) -> None:
        """Broadcast state to all connected clients if state changed."""
        state_hash = self._compute_state_hash(sessions, remote_snapshots)
        if state_hash == self.last_state_hash:
            return

        self.last_state_hash = state_hash
        payload = build_sessions_payload(
            sessions,
            observed_at=time.time(),
            identity=identity,
            remote_snapshots=remote_snapshots,
        )
        message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        with self._lock:
            clients = set(self.clients)

        if not clients:
            return

        # Broadcast to all clients, remove dead connections
        dead_clients = set()
        for client in clients:
            try:
                await client.send(message)
            except Exception as e:
                logger.warning(f"Failed to send to client: {e}")
                dead_clients.add(client)

        if dead_clients:
            with self._lock:
                self.clients -= dead_clients

    def _compute_state_hash(
        self,
        sessions: tuple[CodexSession, ...],
        remote_snapshots: tuple[RemoteSnapshot, ...],
    ) -> int:
        """Compute a hash representing the current state."""
        # Hash based on: number of sessions, their PIDs, statuses, and working directories
        local_sig = tuple(
            (s.root.pid, s.display_status, s.root.cwd, s.waiting_reason)
            for s in sessions
        )
        remote_sig = tuple(
            (snap.identity.server_id, len(snap.sessions))
            for snap in remote_snapshots
        )
        return hash((local_sig, remote_sig))


class WebSocketStateServer:
    """WebSocket server for broadcasting real-time state changes."""

    def __init__(
        self,
        host: str,
        port: int,
        state_provider: Callable[[], tuple[tuple[CodexSession, ...], ServerIdentity, tuple[RemoteSnapshot, ...]]],
        api_token: str | None = None,
        broadcast_interval: float = 0.1,
    ) -> None:
        self.host = host
        self.port = port
        self.state_provider = state_provider
        self.api_token = api_token
        self.broadcast_interval = broadcast_interval
        self.notifier = StateChangeNotifier()
        self._stop_event = asyncio.Event()
        self._server_task: asyncio.Task | None = None
        self._broadcast_task: asyncio.Task | None = None

    async def handler(self, websocket: WebSocketServerProtocol) -> None:
        """Handle WebSocket connection."""
        # Optional token authentication
        if self.api_token:
            try:
                auth_message = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                auth_data = json.loads(auth_message)
                if auth_data.get("token") != self.api_token:
                    await websocket.send(json.dumps({"error": "unauthorized"}))
                    await websocket.close(1008, "Unauthorized")
                    return
                await websocket.send(json.dumps({"ok": True}))
            except (asyncio.TimeoutError, json.JSONDecodeError, KeyError):
                await websocket.send(json.dumps({"error": "invalid_auth"}))
                await websocket.close(1002, "Invalid authentication")
                return

        self.notifier.register(websocket)
        try:
            # Send initial state immediately
            sessions, identity, remote_snapshots = self.state_provider()
            initial_payload = build_sessions_payload(
                sessions,
                observed_at=time.time(),
                identity=identity,
                remote_snapshots=remote_snapshots,
            )
            await websocket.send(json.dumps(initial_payload, ensure_ascii=False))

            # Keep connection alive with ping/pong
            while not self._stop_event.is_set():
                try:
                    # Wait for client messages (mostly pings) with timeout
                    message = await asyncio.wait_for(websocket.recv(), timeout=30.0)
                    # Echo pings or handle other messages
                    if message == "ping":
                        await websocket.send("pong")
                except asyncio.TimeoutError:
                    # Send ping to keep connection alive
                    await websocket.ping()
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.error(f"WebSocket handler error: {e}")
        finally:
            self.notifier.unregister(websocket)

    async def broadcast_loop(self) -> None:
        """Periodically check state and broadcast changes."""
        while not self._stop_event.is_set():
            try:
                sessions, identity, remote_snapshots = self.state_provider()
                await self.notifier.broadcast(sessions, identity, remote_snapshots)
            except Exception as e:
                logger.error(f"Broadcast loop error: {e}")

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.broadcast_interval,
                )
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        """Start the WebSocket server and broadcast loop."""
        logger.info(f"Starting WebSocket server on {self.host}:{self.port}")
        async with websockets.serve(self.handler, self.host, self.port):
            self._broadcast_task = asyncio.create_task(self.broadcast_loop())
            await self._stop_event.wait()

    def stop(self) -> None:
        """Stop the WebSocket server."""
        self._stop_event.set()


async def run_websocket_server(
    host: str,
    port: int,
    state_provider: Callable[[], tuple[tuple[CodexSession, ...], ServerIdentity, tuple[RemoteSnapshot, ...]]],
    api_token: str | None = None,
    broadcast_interval: float = 0.1,
) -> None:
    """Run WebSocket server in asyncio event loop (blocking)."""
    server = WebSocketStateServer(host, port, state_provider, api_token, broadcast_interval)
    await server.start()
