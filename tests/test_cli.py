from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from codex_cli_monitor.cli import main


class CliTests(unittest.TestCase):
    def test_json_output_includes_codex_state_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            proc.mkdir()
            home.mkdir()
            (proc / "uptime").write_text("200.00 0.00\n", encoding="utf-8")
            session = home / "sessions" / "2026" / "06" / "26" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text("{}\n", encoding="utf-8")
            stdout = StringIO()

            with redirect_stdout(stdout):
                result = main(
                    [
                        "--json",
                        "--sample-window",
                        "0",
                        "--proc-root",
                        str(proc),
                        "--codex-home",
                        str(home),
                    ]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["session_count"], 0)
        self.assertEqual(payload["codex_state"]["codex_home"], str(home))
        self.assertEqual(
            payload["codex_state"]["newest_files"][0]["kind"],
            "session_jsonl",
        )


if __name__ == "__main__":
    unittest.main()
