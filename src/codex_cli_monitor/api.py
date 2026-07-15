from __future__ import annotations

import hmac
import json
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.parse import urlparse

from .aggregation import (
    RemoteSnapshot,
    RemoteSnapshotStore,
    ServerIdentity,
    SnapshotValidationError,
    build_collector_snapshot,
    build_sessions_payload as _build_sessions_payload,
    resolve_server_identity,
    snapshot_server_id,
)
from .collector import CollectorPusher
from .models import CodexSession
from .monitor import discover_sessions


DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8765
DEFAULT_REMOTE_TTL_SECONDS = 5.0
DEFAULT_LOCAL_CACHE_SECONDS = 0.25
DEFAULT_COLLECTOR_INTERVAL_SECONDS = 0.5
MAX_SNAPSHOT_BODY_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ApiConfig:
    proc_root: Path = Path("/proc")
    sample_window: float = 0.25
    shim_log: Path | None = None
    codex_home: Path | None = None
    hook_log: Path | None = None
    sleep: Callable[[float], None] = time.sleep
    aggregate: bool = False
    server_id: str | None = None
    server_name: str | None = None
    api_token: str | None = None
    ingest_token: str | None = None
    ingest_tokens: Mapping[str, str] | None = None
    remote_ttl_seconds: float = DEFAULT_REMOTE_TTL_SECONDS
    local_cache_seconds: float = DEFAULT_LOCAL_CACHE_SECONDS
    collector_url: str | None = None
    collector_token: str | None = None
    collector_interval_seconds: float = DEFAULT_COLLECTOR_INTERVAL_SECONDS


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class LocalSessionProvider:
    def __init__(self, config: ApiConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._sessions: tuple[CodexSession, ...] | None = None
        self._observed_at: float | None = None
        self._refreshed_monotonic: float | None = None

    def get(self) -> tuple[tuple[CodexSession, ...], float]:
        now_monotonic = time.monotonic()
        with self._lock:
            if (
                self._sessions is not None
                and self._observed_at is not None
                and self._refreshed_monotonic is not None
                and now_monotonic - self._refreshed_monotonic
                < self.config.local_cache_seconds
            ):
                return self._sessions, self._observed_at
            sessions = discover_sessions(
                proc_root=self.config.proc_root,
                sample_window=self.config.sample_window,
                shim_log=self.config.shim_log,
                codex_home=self.config.codex_home,
                hook_log=self.config.hook_log,
                sleep=self.config.sleep,
            )
            observed_at = time.time()
            self._sessions = sessions
            self._observed_at = observed_at
            self._refreshed_monotonic = time.monotonic()
            return sessions, observed_at


def build_sessions_payload(
    sessions: tuple[CodexSession, ...],
    observed_at: float | None = None,
    identity: ServerIdentity | None = None,
    remote_snapshots: Iterable[RemoteSnapshot] = (),
) -> dict:
    return _build_sessions_payload(
        sessions,
        identity or resolve_server_identity(),
        remote_snapshots=remote_snapshots,
        observed_at=observed_at,
    )


def make_api_handler(
    config: ApiConfig,
    identity: ServerIdentity | None = None,
    provider: LocalSessionProvider | None = None,
    remote_store: RemoteSnapshotStore | None = None,
) -> type[BaseHTTPRequestHandler]:
    identity = identity or resolve_server_identity(
        config.server_id,
        config.server_name,
        config.proc_root,
    )
    provider = provider or LocalSessionProvider(config)

    class CodexMonitorApiHandler(BaseHTTPRequestHandler):
        server_version = "codex-cli-monitor"

        def do_OPTIONS(self) -> None:
            self.send_response(HTTPStatus.NO_CONTENT.value)
            self._send_cors_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/api/sessions", "/api/status", "/api/servers"}:
                if not self._authorize(config.api_token):
                    return
                self._handle_sessions(servers_only=parsed.path == "/api/servers")
                return
            if parsed.path == "/healthz":
                self._send_json(
                    {
                        "ok": True,
                        "mode": "aggregator" if config.aggregate else "collector",
                        "server": identity.to_dict(),
                    }
                )
                return
            if parsed.path == "/":
                self._send_json(
                    {
                        "name": "codex-cli-monitor",
                        "mode": "aggregator" if config.aggregate else "collector",
                        "endpoints": [
                            "/api/sessions",
                            "/api/status",
                            "/api/servers",
                            "/healthz",
                        ],
                    }
                )
                return
            self._send_json(
                {"error": "not_found", "path": parsed.path},
                status=HTTPStatus.NOT_FOUND,
            )

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/api/collector/snapshot" or remote_store is None:
                self._send_json(
                    {"error": "not_found", "path": parsed.path},
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            if not config.ingest_token and not config.ingest_tokens:
                self._send_json(
                    {"error": "ingest_not_configured"},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            self._handle_snapshot(remote_store)

        def log_message(self, format: str, *args: object) -> None:
            return

        def _handle_sessions(self, servers_only: bool = False) -> None:
            try:
                sessions, _ = provider.get()
                response_at = time.time()
                remote_snapshots = (
                    remote_store.active(response_at) if remote_store is not None else ()
                )
                payload = build_sessions_payload(
                    sessions,
                    observed_at=response_at,
                    identity=identity,
                    remote_snapshots=remote_snapshots,
                )
            except Exception as error:  # pragma: no cover - defensive API boundary
                self._send_json(
                    {"error": "scan_failed", "detail": str(error)},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            if servers_only:
                payload = {
                    "observed_at": payload["observed_at"],
                    "observed_at_iso": payload["observed_at_iso"],
                    "server_count": payload["server_count"],
                    "servers": payload["servers"],
                }
            self._send_json(payload)

        def _handle_snapshot(self, store: RemoteSnapshotStore) -> None:
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length or "0")
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_SNAPSHOT_BODY_BYTES:
                self._send_json(
                    {"error": "invalid_content_length"},
                    status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                    if length > MAX_SNAPSHOT_BODY_BYTES
                    else HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise SnapshotValidationError("snapshot must be an object")
                server_id = snapshot_server_id(payload)
                expected_token = (
                    config.ingest_tokens.get(server_id)
                    if config.ingest_tokens is not None
                    else None
                ) or config.ingest_token
                if not expected_token:
                    self._send_json(
                        {"error": "unauthorized"},
                        status=HTTPStatus.UNAUTHORIZED,
                        extra_headers={"WWW-Authenticate": "Bearer"},
                    )
                    return
                if not self._authorize(expected_token):
                    return
                if server_id == identity.server_id:
                    raise SnapshotValidationError(
                        "remote snapshot server id conflicts with aggregator"
                    )
                snapshot = store.ingest(payload)
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                SnapshotValidationError,
            ) as error:
                self._send_json(
                    {"error": "invalid_snapshot", "detail": str(error)},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            self._send_json(
                {
                    "ok": True,
                    "server_id": snapshot.identity.server_id,
                    "received_at": snapshot.received_at,
                },
                status=HTTPStatus.ACCEPTED,
            )

        def _authorize(self, expected_token: str | None) -> bool:
            if not expected_token:
                return True
            authorization = self.headers.get("Authorization", "")
            prefix = "Bearer "
            supplied = authorization[len(prefix) :] if authorization.startswith(prefix) else ""
            if supplied and hmac.compare_digest(supplied, expected_token):
                return True
            self._send_json(
                {"error": "unauthorized"},
                status=HTTPStatus.UNAUTHORIZED,
                extra_headers={"WWW-Authenticate": "Bearer"},
            )
            return False

        def _send_cors_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Authorization, Content-Type",
            )

        def _send_json(
            self,
            payload: dict,
            status: HTTPStatus = HTTPStatus.OK,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._send_cors_headers()
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

    return CodexMonitorApiHandler


def serve_api(
    host: str = DEFAULT_API_HOST,
    port: int = DEFAULT_API_PORT,
    config: ApiConfig | None = None,
) -> None:
    config = config or ApiConfig()
    identity = resolve_server_identity(
        config.server_id,
        config.server_name,
        config.proc_root,
    )
    provider = LocalSessionProvider(config)
    remote_store = (
        RemoteSnapshotStore(config.remote_ttl_seconds) if config.aggregate else None
    )
    server = ReusableThreadingHTTPServer(
        (host, port),
        make_api_handler(config, identity, provider, remote_store),
    )
    collector_stop = threading.Event()
    collector_thread: threading.Thread | None = None
    if config.collector_url is not None:
        def collector_snapshot() -> dict:
            sessions, observed_at = provider.get()
            return build_collector_snapshot(sessions, identity, observed_at)

        pusher = CollectorPusher(
            config.collector_url,
            config.collector_token or "",
            collector_snapshot,
            interval_seconds=config.collector_interval_seconds,
        )
        collector_thread = threading.Thread(
            target=pusher.run,
            args=(collector_stop,),
            name="codex-monitor-collector",
            daemon=True,
        )
        collector_thread.start()
    try:
        server.serve_forever()
    finally:
        collector_stop.set()
        server.server_close()
        if collector_thread is not None:
            collector_thread.join(timeout=2.0)
