from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codex_cli_monitor.paseo_state import (
    _read_proc_env_value,
    _read_selected_json_fields,
    paseo_sessions,
    reset_paseo_cache,
)
from codex_cli_monitor.procfs import read_processes
from tests.test_monitor import _write_common_proc, _write_process


class PaseoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(reset_paseo_cache)
        self.home = Path(self.tmp.name) / "paseo"
        self.proc = Path(self.tmp.name) / "proc"
        self.proc.mkdir()
        _write_common_proc(self.proc)
        _write_process(self.proc, 200, "Paseo Daemon", "S", 1, ["Paseo Daemon"], "/home/test")
        _write_process(
            self.proc, 201, "codex", "S", 200,
            ["codex", "app-server", "--enable", "goals"], "/home/test",
            environment={"PASEO_AGENT_ID": "agent-a", "PASEO_AGENT_CWD": "/work/task"},
        )
        self.processes = read_processes(self.proc)
        self.path = self.home / "agents" / "work-task" / "agent-a.json"
        self.path.parent.mkdir(parents=True)
        self.record = {
            "id": "agent-a", "cwd": "/work/task",
            "createdAt": "2026-11-24T12:19:10.117Z", "lastStatus": "running",
        }
        self.path.write_text(json.dumps(self.record), encoding="utf-8")
        self.env_patch = patch.dict(os.environ, {"PASEO_HOME": str(self.home)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        reset_paseo_cache()

    def test_finished_or_mismatched_records_cannot_create_running_rows(self) -> None:
        for change in (
            {"lastStatus": "idle"}, {"lastStatus": "error"},
            {"lastStatus": "closed"}, {"id": "agent-other"},
            {"cwd": "/work/other"}, {"createdAt": "invalid"},
        ):
            with self.subTest(change=change):
                reset_paseo_cache()
                self.path.write_text(json.dumps({**self.record, **change}), encoding="utf-8")
                self.assertEqual(paseo_sessions(self.processes, self.proc), ())

    def test_exact_pid_exit_clears_row_inside_cache_window(self) -> None:
        self.assertEqual(len(paseo_sessions(self.processes, self.proc)), 1)
        self.processes.pop(201)
        self.assertEqual(paseo_sessions(self.processes, self.proc), ())

    def test_pid_reuse_creates_different_instance(self) -> None:
        first = paseo_sessions(self.processes, self.proc)[0]
        self.processes[201] = replace(self.processes[201], start_ticks=12345)
        second = paseo_sessions(self.processes, self.proc)[0]
        self.assertNotEqual(first.instance_id, second.instance_id)

    def test_unrelated_parent_cannot_claim_a_paseo_task(self) -> None:
        self.processes[201] = replace(self.processes[201], ppid=1)
        self.assertEqual(paseo_sessions(self.processes, self.proc), ())

    def test_dead_engine_is_not_running(self) -> None:
        self.processes[201] = replace(self.processes[201], state="Z")
        self.assertEqual(paseo_sessions(self.processes, self.proc), ())

    def test_home_change_cannot_reuse_other_paseo_rows(self) -> None:
        self.assertEqual(len(paseo_sessions(self.processes, self.proc)), 1)
        with patch.dict(os.environ, {"PASEO_HOME": str(self.home / "other")}):
            self.assertEqual(paseo_sessions(self.processes, self.proc), ())

    def test_unread_attention_is_not_an_approval_prompt(self) -> None:
        self.path.write_text(json.dumps({
            **self.record, "requiresAttention": True,
            "attentionReason": "finished",
        }), encoding="utf-8")
        session = paseo_sessions(self.processes, self.proc)[0]
        self.assertEqual(session.display_status, "运行中")
        self.assertIsNone(session.waiting_reason)

    def test_private_values_are_not_decoded(self) -> None:
        # Invalid UTF-8 in a skipped value would fail if the complete record
        # or its title/config were decoded instead of just structural fields.
        raw = b'{"title":"\xff","config":{"systemPrompt":"\xfe"},"id":"a","lastStatus":"running"}'
        self.assertEqual(
            _read_selected_json_fields(raw, {"id", "lastStatus"}),
            {"id": "a", "lastStatus": "running"},
        )

    def test_duplicate_structural_keys_fail_closed(self) -> None:
        self.assertIsNone(_read_selected_json_fields(
            b'{"id":"a","id":"b"}', {"id"},
        ))

    def test_environment_values_cross_chunk_boundaries_without_exposing_other_keys(self) -> None:
        path = Path(self.tmp.name) / "environment"
        path.write_bytes(
            b"SECRET=" + b"x" * 4080 + b"\0PASEO_AGENT_ID=agent-a\0"
            + b"OTHER=ignored\0PASEO_AGENT_CWD=/work/task\0"
        )
        self.assertEqual(_read_proc_env_value(path, "PASEO_AGENT_ID"), "agent-a")
        self.assertEqual(_read_proc_env_value(path, "PASEO_AGENT_CWD"), "/work/task")
        self.assertIsNone(_read_proc_env_value(path, "PASEO_NOT_PRESENT"))


if __name__ == "__main__":
    unittest.main()
