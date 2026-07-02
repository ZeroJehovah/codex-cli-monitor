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

    def test_json_output_uses_display_status_for_session_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
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
        self.assertEqual(payload["sessions"][0]["status"], "成功")
        self.assertEqual(
            payload["sessions"][0]["inferred_status"]["status"],
            "waiting_user_likely",
        )

    def test_text_output_includes_explicit_session_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            proc.mkdir()
            home.mkdir()
            (proc / "uptime").write_text("200.00 0.00\n", encoding="utf-8")
            stdout = StringIO()

            with redirect_stdout(stdout):
                result = main(
                    [
                        "--sample-window",
                        "0",
                        "--proc-root",
                        str(proc),
                        "--codex-home",
                        str(home),
                    ]
                )

        text = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("Open Codex sessions: 0", text)

    def test_text_output_uses_display_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            stdout = StringIO()

            with redirect_stdout(stdout):
                result = main(
                    [
                        "--sample-window",
                        "0",
                        "--proc-root",
                        str(proc),
                        "--codex-home",
                        str(home),
                    ]
                )

        text = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("成功", text)
        self.assertNotIn("waiting_user_likely", text)


def _write_common_proc(proc: Path) -> None:
    (proc / "uptime").write_text("200.00 0.00\n", encoding="utf-8")


def _write_process(
    proc: Path,
    pid: int,
    comm: str,
    state: str,
    ppid: int,
    cmdline: list[str],
    cwd: str,
) -> None:
    pid_dir = proc / str(pid)
    (pid_dir / "fd").mkdir(parents=True)
    (pid_dir / "stat").write_text(_stat_line(pid, comm, state, ppid), encoding="utf-8")
    (pid_dir / "cmdline").write_bytes(b"\0".join(item.encode() for item in cmdline) + b"\0")
    (pid_dir / "cwd").symlink_to(cwd)
    (pid_dir / "exe").symlink_to(f"/usr/bin/{cmdline[0]}")
    (pid_dir / "fd" / "0").symlink_to("/dev/pts/3")


def _stat_line(pid: int, comm: str, state: str, ppid: int) -> str:
    fields = [
        state,
        str(ppid),
        "0",
        "0",
        "34816",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "5",
        "7",
        "0",
        "0",
        "20",
        "0",
        "1",
        "0",
        "100",
    ]
    return f"{pid} ({comm}) {' '.join(fields)}\n"


if __name__ == "__main__":
    unittest.main()
