from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_cli_monitor.codex_state import scan_codex_state


class CodexStateTests(unittest.TestCase):
    def test_scan_codex_state_reads_metadata_without_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            shell_snapshot = home / "shell_snapshots" / "abc.sh"
            session.parent.mkdir(parents=True)
            shell_snapshot.parent.mkdir(parents=True)
            session.write_text('{"secret":"not returned"}\n', encoding="utf-8")
            shell_snapshot.write_text("echo hi\n", encoding="utf-8")
            (home / "state_5.sqlite-wal").write_bytes(b"sqlite")

            summary = scan_codex_state(home, max_files=10)

        payload = summary.to_dict()
        self.assertEqual(summary.codex_home, tmp)
        self.assertEqual(summary.scan_errors, ())
        self.assertIn("session_jsonl", {item.kind for item in summary.newest_files})
        self.assertIn("shell_snapshot", {item.kind for item in summary.newest_files})
        self.assertNotIn("secret", repr(payload))

    def test_missing_codex_home_reports_conservative_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"

            summary = scan_codex_state(missing)

        self.assertEqual(summary.newest_files, ())
        self.assertIn("does not exist", summary.scan_errors[0])


if __name__ == "__main__":
    unittest.main()
