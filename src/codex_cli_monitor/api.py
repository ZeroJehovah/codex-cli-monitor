from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .models import CodexSession
from .monitor import discover_sessions


DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8765


@dataclass(frozen=True)
class ApiConfig:
    proc_root: Path = Path("/proc")
    sample_window: float = 0.25
    shim_log: Path | None = None
    codex_home: Path | None = None
    hook_log: Path | None = None
    sleep: Callable[[float], None] = time.sleep


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def build_sessions_payload(
    sessions: tuple[CodexSession, ...],
    observed_at: float | None = None,
) -> dict:
    observed_at = time.time() if observed_at is None else observed_at
    return {
        "observed_at": observed_at,
        "observed_at_iso": _timestamp_iso(observed_at),
        "session_count": len(sessions),
        "sessions": [_session_payload(session) for session in sessions],
    }


def make_api_handler(config: ApiConfig) -> type[BaseHTTPRequestHandler]:
    class CodexMonitorApiHandler(BaseHTTPRequestHandler):
        server_version = "codex-cli-monitor"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/api/sessions", "/api/status"}:
                self._handle_sessions()
                return
            if parsed.path == "/healthz":
                self._send_json({"ok": True})
                return
            if parsed.path == "/":
                self._send_json(
                    {
                        "name": "codex-cli-monitor",
                        "endpoints": ["/api/sessions", "/api/status", "/healthz"],
                    }
                )
                return
            self._send_json(
                {"error": "not_found", "path": parsed.path},
                status=HTTPStatus.NOT_FOUND,
            )

        def log_message(self, format: str, *args: object) -> None:
            return

        def _handle_sessions(self) -> None:
            try:
                sessions = discover_sessions(
                    proc_root=config.proc_root,
                    sample_window=config.sample_window,
                    shim_log=config.shim_log,
                    codex_home=config.codex_home,
                    hook_log=config.hook_log,
                    sleep=config.sleep,
                )
                payload = build_sessions_payload(sessions)
            except Exception as error:  # pragma: no cover - defensive API boundary
                self._send_json(
                    {"error": "scan_failed", "detail": str(error)},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            self._send_json(payload)

        def _send_json(
            self,
            payload: dict,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

    return CodexMonitorApiHandler


def serve_api(
    host: str = DEFAULT_API_HOST,
    port: int = DEFAULT_API_PORT,
    config: ApiConfig | None = None,
) -> None:
    server = ReusableThreadingHTTPServer(
        (host, port),
        make_api_handler(config or ApiConfig()),
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _session_payload(session: CodexSession) -> dict:
    root = session.root
    return {
        "pid": root.pid,
        "ppid": root.ppid,
        "status": session.display_status,
        "directory": root.cwd,
        "started_at": root.started_at,
        "started_at_iso": _timestamp_iso(root.started_at),
        "elapsed_seconds": root.elapsed_seconds,
        "tty": root.tty,
        "command": root.command_name,
        "inferred_status": session.inference.to_dict(),
        "state_activity": session.state_activity.to_dict()
        if session.state_activity is not None
        else None,
        "hook_state": session.hook_state.to_dict()
        if session.hook_state is not None
        else None,
    }


def _timestamp_iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
