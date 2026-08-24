from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler

from codex_cli_monitor.aggregation import (
    RemoteSnapshotStore,
    ServerIdentity,
    SnapshotValidationError,
    build_collector_snapshot,
    build_sessions_payload,
    resolve_server_identity,
)
from codex_cli_monitor.api import ApiConfig, LocalSessionProvider, make_api_handler
from codex_cli_monitor.collector import CollectorPusher, normalize_aggregator_url
from codex_cli_monitor.models import CodexSession, Inference, ProcessInfo


class AggregationTests(unittest.TestCase):
    def test_remote_snapshot_ttl_defaults_to_thirty_seconds(self) -> None:
        self.assertEqual(ApiConfig().remote_ttl_seconds, 30.0)
        self.assertEqual(RemoteSnapshotStore().ttl_seconds, 30.0)

    def test_resolve_server_identity_reads_boot_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            boot_id = proc / "sys" / "kernel" / "random" / "boot_id"
            boot_id.parent.mkdir(parents=True)
            boot_id.write_text("boot-123\n", encoding="utf-8")

            identity = resolve_server_identity("server-a", "Server A", proc)

        self.assertEqual(identity.server_id, "server-a")
        self.assertEqual(identity.server_name, "Server A")
        self.assertEqual(identity.boot_id, "boot-123")

    def test_remote_snapshot_expires_by_receive_time(self) -> None:
        identity = ServerIdentity("server-a", "Server A", "boot-a")
        snapshot = build_collector_snapshot((_session(100),), identity, observed_at=10.0)
        store = RemoteSnapshotStore(ttl_seconds=5.0)

        self.assertEqual(
            set(snapshot["sessions"][0]),
            {"pid", "status", "cli_type", "directory", "started_at"},
        )

        store.ingest(snapshot, received_at=100.0)

        self.assertEqual(len(store.active(now=104.9)), 1)
        self.assertEqual(store.active(now=105.1), ())

    def test_remote_snapshot_rejects_invalid_status(self) -> None:
        identity = ServerIdentity("server-a", "Server A", "boot-a")
        snapshot = build_collector_snapshot((_session(100),), identity, observed_at=10.0)
        snapshot["sessions"][0]["status"] = "unknown"

        with self.assertRaises(SnapshotValidationError):
            RemoteSnapshotStore().ingest(snapshot, received_at=11.0)

    def test_remote_snapshot_preserves_known_cli_types(self) -> None:
        identity = ServerIdentity("server-a", "Server A", "boot-a")
        for cli_type in ("codex", "opencode", "claude"):
            with self.subTest(cli_type=cli_type):
                snapshot = build_collector_snapshot(
                    (_session(100, cli_type=cli_type),),
                    identity,
                    observed_at=10.0,
                )
                ingested = RemoteSnapshotStore().ingest(snapshot, received_at=11.0)
                self.assertEqual(ingested.sessions[0]["cli_type"], cli_type)

    def test_remote_snapshot_falls_back_to_codex_for_unknown_cli_types(self) -> None:
        identity = ServerIdentity("server-a", "Server A", "boot-a")
        snapshot = build_collector_snapshot((_session(100),), identity, observed_at=10.0)
        snapshot["sessions"][0]["cli_type"] = "some-future-cli"

        ingested = RemoteSnapshotStore().ingest(snapshot, received_at=11.0)

        self.assertEqual(ingested.sessions[0]["cli_type"], "codex")

    def test_combined_payload_keeps_same_pid_separate_by_server(self) -> None:
        local_identity = ServerIdentity("local", "Local", "boot-local")
        remote_identity = ServerIdentity("remote", "Remote", "boot-remote")
        store = RemoteSnapshotStore(ttl_seconds=5.0)
        store.ingest(
            build_collector_snapshot((_session(100),), remote_identity, observed_at=20.0),
            received_at=20.0,
        )

        payload = build_sessions_payload(
            (_session(100),),
            local_identity,
            store.active(now=20.0),
            observed_at=20.0,
        )

        self.assertEqual(payload["server_count"], 2)
        self.assertEqual(payload["session_count"], 2)
        self.assertEqual(
            {item["server_id"] for item in payload["sessions"]},
            {"local", "remote"},
        )
        self.assertEqual(len({item["session_key"] for item in payload["sessions"]}), 2)

    def test_aggregator_api_requires_tokens_and_accepts_collector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp) / "proc"
            proc.mkdir()
            (proc / "uptime").write_text("100.0 0.0\n", encoding="utf-8")
            config = ApiConfig(
                proc_root=proc,
                sample_window=0,
                aggregate=True,
                server_id="vps",
                server_name="VPS",
                api_token="read-secret",
                ingest_tokens={"server-a": "write-secret"},
            )
            identity = ServerIdentity("vps", "VPS", "boot-vps")
            store = RemoteSnapshotStore(ttl_seconds=5.0)
            handler = make_api_handler(
                config,
                identity,
                LocalSessionProvider(config),
                store,
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                snapshot = build_collector_snapshot(
                    (_session(200),),
                    ServerIdentity("server-a", "Server A", "boot-a"),
                    observed_at=30.0,
                )
                body = json.dumps(snapshot).encode("utf-8")

                connection = HTTPConnection("127.0.0.1", port)
                connection.request(
                    "POST",
                    "/api/collector/snapshot",
                    body=body,
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(connection.getresponse().status, 401)
                connection.close()

                pusher = CollectorPusher(
                    f"http://127.0.0.1:{port}",
                    "write-secret",
                    lambda: snapshot,
                )
                pusher.post_once()
                status = pusher.status_snapshot()
                self.assertTrue(status["proxy_bypassed"])
                self.assertTrue(status["healthy"])
                self.assertEqual(status["attempt_count"], 1)
                self.assertEqual(status["success_count"], 1)
                self.assertEqual(status["failure_count"], 0)
                self.assertEqual(status["consecutive_failures"], 0)
                self.assertIsNotNone(status["last_success_at_iso"])

                proxy_handlers = [
                    handler
                    for handler in pusher._opener.handlers
                    if isinstance(handler, ProxyHandler)
                ]
                self.assertEqual(proxy_handlers, [])

                rejected_pusher = CollectorPusher(
                    f"http://127.0.0.1:{port}",
                    "wrong-secret",
                    lambda: snapshot,
                )
                with self.assertRaisesRegex(RuntimeError, "HTTP 401.*unauthorized"):
                    rejected_pusher.post_once()
                rejected_status = rejected_pusher.status_snapshot()
                self.assertFalse(rejected_status["healthy"])
                self.assertEqual(rejected_status["attempt_count"], 1)
                self.assertEqual(rejected_status["failure_count"], 1)
                self.assertEqual(rejected_status["consecutive_failures"], 1)
                self.assertIn("HTTP 401", rejected_status["last_error"])
                self.assertNotIn("wrong-secret", json.dumps(rejected_status))

                unknown_snapshot = dict(snapshot)
                unknown_snapshot["server"] = {
                    "id": "server-b",
                    "name": "Server B",
                    "boot_id": "boot-b",
                }
                unknown_body = json.dumps(unknown_snapshot).encode("utf-8")
                connection = HTTPConnection("127.0.0.1", port)
                connection.request(
                    "POST",
                    "/api/collector/snapshot",
                    body=unknown_body,
                    headers={
                        "Authorization": "Bearer write-secret",
                        "Content-Type": "application/json",
                    },
                )
                self.assertEqual(connection.getresponse().status, 401)
                connection.close()

                connection = HTTPConnection("127.0.0.1", port)
                connection.request("GET", "/api/sessions")
                self.assertEqual(connection.getresponse().status, 401)
                connection.close()

                connection = HTTPConnection("127.0.0.1", port)
                connection.request(
                    "GET",
                    "/api/sessions",
                    headers={"Authorization": "Bearer read-secret"},
                )
                response = connection.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["server_count"], 2)
                self.assertEqual(payload["sessions"][0]["server_id"], "server-a")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2.0)

    def test_normalize_aggregator_url_appends_snapshot_path(self) -> None:
        self.assertEqual(
            normalize_aggregator_url("https://monitor.example.com"),
            "https://monitor.example.com/api/collector/snapshot",
        )
        self.assertEqual(
            normalize_aggregator_url(
                "https://monitor.example.com/custom/snapshot"
            ),
            "https://monitor.example.com/custom/snapshot",
        )

    def test_collector_status_does_not_expose_url_credentials_or_query(self) -> None:
        pusher = CollectorPusher(
            "https://user:password@monitor.example.com?access_token=secret",
            "write-secret",
            lambda: {},
        )

        status = pusher.status_snapshot()

        self.assertEqual(
            status["url"],
            "https://monitor.example.com/api/collector/snapshot",
        )
        self.assertNotIn("password", json.dumps(status))
        self.assertNotIn("access_token", json.dumps(status))

    def test_health_endpoint_includes_non_secret_collector_status(self) -> None:
        status = {
            "url": "https://codex-monitor.aiof.top/api/collector/snapshot",
            "proxy_bypassed": True,
            "healthy": False,
            "attempt_count": 2,
            "success_count": 1,
            "failure_count": 1,
            "consecutive_failures": 1,
            "last_error": "timeout",
        }
        config = ApiConfig(
            server_id="server-a",
            server_name="Server A",
            hook_log=Path("/definitely-missing/codex-monitor-hooks.jsonl"),
        )
        handler = make_api_handler(
            config,
            ServerIdentity("server-a", "Server A", "boot-a"),
            collector_status_provider=lambda: status,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = HTTPConnection("127.0.0.1", server.server_address[1])
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["collector"], status)
        self.assertEqual(payload["hooks"]["runtime"]["event_mode"], "default")
        self.assertFalse(payload["hooks"]["runtime"]["exists"])
        self.assertIn("installation", payload["hooks"])
        self.assertIn("signal_state", payload["hooks"])
        self.assertEqual(payload["hooks"]["installation"]["trust_state"], "unknown")
        self.assertEqual(
            set(payload["claude_state"]),
            {
                "home",
                "home_exists",
                "sessions_dir_exists",
                "projects_dir_exists",
                "registered_sessions",
            },
        )
        self.assertNotIn("token", json.dumps(payload).lower())

    def test_collector_logs_timestamped_failure_and_recovery(self) -> None:
        stop_event = threading.Event()
        opener = _FailThenSucceedOpener(stop_event)
        pusher = CollectorPusher(
            "https://codex-monitor.aiof.top",
            "write-secret",
            lambda: {"schema_version": 1},
            interval_seconds=0.001,
            opener=opener,
        )
        output = io.StringIO()

        with redirect_stderr(output):
            pusher.run(stop_event)

        log = output.getvalue()
        self.assertRegex(log, r"\d{4}-\d{2}-\d{2}T.*Z INFO collector push started")
        self.assertIn("ERROR collector push failed consecutive=1", log)
        self.assertIn("INFO collector push recovered after=1", log)
        status = pusher.status_snapshot()
        self.assertTrue(status["healthy"])
        self.assertEqual(status["attempt_count"], 2)
        self.assertEqual(status["success_count"], 1)
        self.assertEqual(status["failure_count"], 1)
        self.assertEqual(status["consecutive_failures"], 0)


def _session(pid: int, cli_type: str = "codex") -> CodexSession:
    return CodexSession(
        root=ProcessInfo(
            pid=pid,
            ppid=1,
            comm=cli_type,
            state="S",
            cmdline=(cli_type,),
            cwd="/work/project",
            exe=f"/usr/bin/{cli_type}",
            tty="/dev/pts/1",
            tty_nr=1,
            elapsed_seconds=5.0,
            cpu_seconds=1.0,
            started_at=10.0,
        ),
        descendants=(),
        connections=(),
        inference=Inference("waiting_user_likely", 0.9, ()),
        display_status="成功",
        cli_type=cli_type,
    )


class _FakeResponse:
    status = 202

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return b"{}"


class _FailThenSucceedOpener:
    def __init__(self, stop_event: threading.Event) -> None:
        self.stop_event = stop_event
        self.calls = 0

    def open(self, *args: object, **kwargs: object) -> _FakeResponse:
        self.calls += 1
        if self.calls == 1:
            raise URLError("offline")
        self.stop_event.set()
        return _FakeResponse()


if __name__ == "__main__":
    unittest.main()
