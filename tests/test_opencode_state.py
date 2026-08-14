from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_cli_monitor.opencode_state import (
    STATUS_FAILURE,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    default_opencode_data_dir,
    default_opencode_hook_log_path,
    opencode_db_path,
    scan_opencode_state,
)
from codex_cli_monitor.opencode_hook_state import (
    append_opencode_hook_event,
    load_opencode_hook_events,
    opencode_hook_log_health,
)


def _build_db(path: Path, sessions: list[dict], messages: list[dict]) -> None:
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                workspace_id TEXT,
                parent_id TEXT,
                slug TEXT NOT NULL,
                directory TEXT NOT NULL,
                path TEXT,
                title TEXT NOT NULL,
                version TEXT NOT NULL,
                share_url TEXT,
                summary_additions INTEGER,
                summary_deletions INTEGER,
                summary_files INTEGER,
                summary_diffs TEXT,
                metadata TEXT,
                cost REAL DEFAULT 0 NOT NULL,
                tokens_input INTEGER DEFAULT 0 NOT NULL,
                tokens_output INTEGER DEFAULT 0 NOT NULL,
                tokens_reasoning INTEGER DEFAULT 0 NOT NULL,
                tokens_cache_read INTEGER DEFAULT 0 NOT NULL,
                tokens_cache_write INTEGER DEFAULT 0 NOT NULL,
                revert TEXT,
                permission TEXT,
                agent TEXT,
                model TEXT,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                time_compacting INTEGER,
                time_archived INTEGER
            )
            """
        )
        con.execute(
            """
            CREATE TABLE message (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                data TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE part (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                data TEXT NOT NULL
            )
            """
        )
        for item in sessions:
            con.execute(
                "INSERT INTO session (id, project_id, slug, directory, title, "
                "version, time_created, time_updated) VALUES (?,?,?,?,?,?,?,?)",
                (
                    item["id"],
                    item.get("project_id", "global"),
                    item.get("slug", "s"),
                    item["directory"],
                    item.get("title", "t"),
                    "1.0.0",
                    item["time_created"],
                    item["time_updated"],
                ),
            )
        for item in messages:
            con.execute(
                "INSERT INTO message (id, session_id, time_created, time_updated, data) "
                "VALUES (?,?,?,?,?)",
                (
                    item["id"],
                    item["session_id"],
                    item["time_created"],
                    item["time_updated"],
                    json.dumps(item["data"], sort_keys=True),
                ),
            )
        con.commit()
    finally:
        con.close()


class OpenCodeStateTests(unittest.TestCase):
    def _scan(self, sessions, messages, ids=()):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / ".local" / "share" / "opencode"
            data_dir.mkdir(parents=True)
            db = data_dir / "opencode.db"
            _build_db(db, sessions, messages)
            return scan_opencode_state(data_dir, ids=ids)

    def test_default_prefers_env(self) -> None:
        env = {"OPENCODE_DATA": "/custom/data", "OPENCODE_HOME": "/home"}
        self.assertEqual(
            default_opencode_data_dir(env),
            Path("/custom/data"),
        )

    def test_db_path_under_data_dir(self) -> None:
        self.assertEqual(
            opencode_db_path(Path("/x/opencode")),
            Path("/x/opencode/opencode.db"),
        )

    def test_running_session_with_streaming_assistant(self) -> None:
        now_ms = 1786681500000
        states = self._scan(
            [
                {
                    "id": "s1",
                    "directory": "/work",
                    "time_created": now_ms - 60_000,
                    "time_updated": now_ms - 1_000,
                }
            ],
            [
                {
                    "id": "m1",
                    "session_id": "s1",
                    "time_created": now_ms - 60_000,
                    "time_updated": now_ms - 60_000,
                    "data": {
                        "role": "user",
                        "time": {"created": now_ms - 60_000},
                    },
                },
                {
                    "id": "m2",
                    "session_id": "s1",
                    "time_created": now_ms - 50_000,
                    "time_updated": now_ms - 1_000,
                    "data": {
                        "role": "assistant",
                        "time": {"created": now_ms - 50_000},
                    },
                },
            ],
        )
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0].status, STATUS_RUNNING)
        self.assertTrue(states[0].turn_active)

    def test_completed_success_session(self) -> None:
        now_ms = 1786681500000
        states = self._scan(
            [
                {
                    "id": "s2",
                    "directory": "/work2",
                    "time_created": now_ms - 120_000,
                    "time_updated": now_ms - 10_000,
                }
            ],
            [
                {
                    "id": "m1",
                    "session_id": "s2",
                    "time_created": now_ms - 120_000,
                    "time_updated": now_ms - 120_000,
                    "data": {"role": "user", "time": {"created": now_ms - 120_000}},
                },
                {
                    "id": "m2",
                    "session_id": "s2",
                    "time_created": now_ms - 100_000,
                    "time_updated": now_ms - 10_000,
                    "data": {
                        "role": "assistant",
                        "time": {"created": now_ms - 100_000, "completed": now_ms - 10_000},
                        "finish": "stop",
                    },
                },
            ],
        )
        self.assertEqual(states[0].status, STATUS_SUCCESS)
        self.assertFalse(states[0].turn_active)
        self.assertTrue(states[0].terminal_event)
        self.assertFalse(states[0].failed_event)

    def test_running_tool_marks_active(self) -> None:
        now_ms = 1786681500000
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            db = data_dir / "opencode.db"
            con = sqlite3.connect(str(db))
            try:
                con.execute(
                    "CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, "
                    "slug TEXT, directory TEXT, title TEXT, version TEXT, "
                    "time_created INTEGER, time_updated INTEGER)"
                )
                con.execute(
                    "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
                    "time_created INTEGER, time_updated INTEGER, data TEXT)"
                )
                con.execute(
                    "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, "
                    "session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)"
                )
                con.execute(
                    "INSERT INTO session VALUES "
                    "('s1','g','s','/work','t','1.0.0',1000,2000)"
                )
                con.execute(
                    "INSERT INTO message VALUES "
                    "('m1','s1',1000,1000,'{\"role\":\"user\",\"time\":{\"created\":1000}}'),"
                    "('m2','s1',1500,1500,'{\"role\":\"assistant\",\"time\":{\"created\":1500,\"completed\":2000},\"finish\":\"tool-calls\"}')"
                )
                con.execute(
                    "INSERT INTO part VALUES "
                    "('p1','m2','s1',1600,1800,'{\"type\":\"tool\",\"state\":{\"status\":\"running\"}}')"
                )
                con.commit()
            finally:
                con.close()
            states = scan_opencode_state(data_dir)
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0].status, STATUS_RUNNING)

    def test_failed_finish_marks_failure(self) -> None:
        now_ms = 1786681500000
        states = self._scan(
            [
                {
                    "id": "s3",
                    "directory": "/work3",
                    "time_created": now_ms - 100_000,
                    "time_updated": now_ms - 5_000,
                }
            ],
            [
                {
                    "id": "m1",
                    "session_id": "s3",
                    "time_created": now_ms - 100_000,
                    "time_updated": now_ms - 100_000,
                    "data": {"role": "user", "time": {"created": now_ms - 100_000}},
                },
                {
                    "id": "m2",
                    "session_id": "s3",
                    "time_created": now_ms - 80_000,
                    "time_updated": now_ms - 5_000,
                    "data": {
                        "role": "assistant",
                        "time": {"created": now_ms - 80_000, "completed": now_ms - 5_000},
                        "finish": "error",
                    },
                },
            ],
        )
        self.assertEqual(states[0].status, STATUS_FAILURE)
        self.assertTrue(states[0].failed_event)

    def test_wal_write_invalidates_cache(self) -> None:
        """In WAL mode the main db file may not change on write; cache must
        include the WAL file so state transitions are picked up promptly.

        This reproduces the production scenario where the OpenCode CLI
        process owns the write connection and keeps it open.  Writes land
        in the WAL file; the main db file's mtime/size do not change
        until a checkpoint runs.  The monitor opens the database
        read-only and must see the WAL-backed writes immediately.
        """
        now_ms = 1786681500000
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            db = data_dir / "opencode.db"
            write_con = sqlite3.connect(str(db), isolation_level=None)
            try:
                write_con.execute("PRAGMA journal_mode=WAL")
                write_con.execute("PRAGMA wal_autocheckpoint=0")
                write_con.execute(
                    "CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, "
                    "slug TEXT, directory TEXT, title TEXT, version TEXT, "
                    "time_created INTEGER, time_updated INTEGER)"
                )
                write_con.execute(
                    "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
                    "time_created INTEGER, time_updated INTEGER, data TEXT)"
                )
                write_con.execute(
                    "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, "
                    "session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)"
                )
                write_con.execute("BEGIN")
                write_con.execute(
                    "INSERT INTO session VALUES "
                    "('s1','g','s','/work','t','1.0.0',1000,1000)"
                )
                write_con.execute(
                    "INSERT INTO message VALUES "
                    "('m1','s1',1000,1000,'{\"role\":\"user\",\"time\":{\"created\":1000}}')"
                )
                write_con.execute("COMMIT")
            except Exception:
                write_con.close()
                raise

            wal = db.with_name(db.name + "-wal")

            # First scan: session is running (no completed assistant message).
            states = scan_opencode_state(data_dir)
            self.assertEqual(states[0].status, STATUS_RUNNING)

            old_db_stat = db.stat()
            old_wal_stat = wal.stat() if wal.exists() else None

            # Write the completion message.  The write connection stays
            # open, so the WAL retains the new pages and no checkpoint
            # merges them into the main db file.
            write_con.execute("BEGIN")
            write_con.execute(
                "INSERT INTO message VALUES "
                "('m2','s1',1500,2000,"
                "'{\"role\":\"assistant\",\"time\":{\"created\":1500,\"completed\":2000},"
                "\"finish\":\"stop\"}')"
            )
            write_con.execute("UPDATE session SET time_updated=2000 WHERE id='s1'")
            write_con.execute("COMMIT")

            new_db_stat = db.stat()
            new_wal_stat = wal.stat() if wal.exists() else None

            # The main db file must not have grown (no checkpoint); only
            # the WAL file changed.  This is the condition that made the
            # old cache return stale 运行中 forever.
            self.assertEqual(
                new_db_stat.st_size,
                old_db_stat.st_size,
                "main db file should not have been checkpointed",
            )
            self.assertTrue(
                new_wal_stat is not None and old_wal_stat is not None,
                "WAL file should exist throughout",
            )
            self.assertGreaterEqual(
                new_wal_stat.st_size,
                old_wal_stat.st_size,
                "WAL file should have grown",
            )

            # The cache must invalidate on the WAL change and return the
            # new success status.
            states = scan_opencode_state(data_dir)
            self.assertEqual(states[0].status, STATUS_SUCCESS)
            self.assertFalse(states[0].turn_active)
            self.assertTrue(states[0].terminal_event)
            self.assertFalse(states[0].failed_event)
            write_con.close()

    def test_restricted_ids_selects_session(self) -> None:
        now_ms = 1786681500000
        states = self._scan(
            [
                {
                    "id": "s1",
                    "directory": "/a",
                    "time_created": now_ms - 10_000,
                    "time_updated": now_ms - 5_000,
                },
                {
                    "id": "s2",
                    "directory": "/b",
                    "time_created": now_ms - 20_000,
                    "time_updated": now_ms - 8_000,
                },
            ],
            [
                {
                    "id": "m1",
                    "session_id": "s1",
                    "time_created": now_ms - 10_000,
                    "time_updated": now_ms - 10_000,
                    "data": {"role": "user", "time": {"created": now_ms - 10_000}},
                },
            ],
            ids=("s2",),
        )
        session_ids = {item.session_id for item in states}
        self.assertIn("s2", session_ids)  # requested id is kept visible
        self.assertGreaterEqual(len(states), 1)


class OpenCodeHookStateTests(unittest.TestCase):
    def test_append_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "hooks.jsonl"
            ok = append_opencode_hook_event(
                "user_prompt_submit",
                cwd="/work",
                ppid=1234,
                path=log,
                hook_payload={"session_id": "ses_1"},
            )
            self.assertTrue(ok)
            events = load_opencode_hook_events(log)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].event, "user_prompt_submit")
            self.assertEqual(events[0].cwd, "/work")
            self.assertEqual(events[0].ppid, 1234)
            self.assertEqual(events[0].session_id, "ses_1")

    def test_health_exposes_path_and_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "hooks.jsonl"
            append_opencode_hook_event("stop", cwd="/x", ppid=1, path=log)
            health = opencode_hook_log_health(log)
            self.assertTrue(health["exists"])
            self.assertEqual(health["event_count"], 1)

    def test_tolerates_corrupt_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "hooks.jsonl"
            log.write_bytes(b"not-json\n")
            append_opencode_hook_event("stop", cwd="/x", ppid=1, path=log)
            events = load_opencode_hook_events(log)
            self.assertEqual(len(events), 1)

    def test_default_hook_log_path_uses_xdg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"XDG_STATE_HOME": str(Path(tmp) / "state")}
            self.assertEqual(
                default_opencode_hook_log_path(env),
                Path(tmp) / "state" / "opencode-cli-monitor" / "hooks.jsonl",
            )


if __name__ == "__main__":
    unittest.main()