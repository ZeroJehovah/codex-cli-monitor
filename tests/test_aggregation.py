from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

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
            {"pid", "status", "directory", "started_at"},
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


def _session(pid: int) -> CodexSession:
    return CodexSession(
        root=ProcessInfo(
            pid=pid,
            ppid=1,
            comm="codex",
            state="S",
            cmdline=("codex",),
            cwd="/work/project",
            exe="/usr/bin/codex",
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
    )


if __name__ == "__main__":
    unittest.main()
