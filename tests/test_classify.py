from __future__ import annotations

import unittest

from codex_cli_monitor.classify import infer_status, is_codex_process
from codex_cli_monitor.models import ProcessInfo


class ClassifyTests(unittest.TestCase):
    def test_long_lived_mcp_support_process_can_still_look_waiting(self) -> None:
        root = _process(100, "codex", state="S", tty="/dev/pts/1")
        mcp = _process(
            101,
            "node",
            cmdline=("chrome-devtools-mcp",),
            ppid=100,
            state="S",
        )

        inference = infer_status(root, (mcp,), (), sample_window=0)

        self.assertEqual(inference.status, "waiting_user_likely")

    def test_shell_child_is_tool_running(self) -> None:
        root = _process(100, "codex", state="S", tty="/dev/pts/1")
        shell = _process(
            101,
            "sh",
            cmdline=("sh", "-c", "pytest"),
            ppid=100,
            state="S",
        )

        inference = infer_status(root, (shell,), (), sample_window=0)

        self.assertEqual(inference.status, "tool_running_likely")

    def test_codex_process_allows_deleted_exe_suffix(self) -> None:
        process = _process(
            100,
            "MainThread",
            cmdline=(),
            ppid=1,
            state="S",
            tty="/dev/pts/1",
        )
        process = ProcessInfo(
            pid=process.pid,
            ppid=process.ppid,
            comm=process.comm,
            state=process.state,
            cmdline=process.cmdline,
            cwd=process.cwd,
            exe="/tmp/.codex/bin/codex (deleted)",
            tty=process.tty,
            tty_nr=process.tty_nr,
            elapsed_seconds=process.elapsed_seconds,
            cpu_seconds=process.cpu_seconds,
        )

        self.assertTrue(is_codex_process(process))


def _process(
    pid: int,
    comm: str,
    cmdline: tuple[str, ...] | None = None,
    ppid: int = 1,
    state: str = "S",
    tty: str | None = None,
) -> ProcessInfo:
    return ProcessInfo(
        pid=pid,
        ppid=ppid,
        comm=comm,
        state=state,
        cmdline=cmdline or (comm,),
        cwd="/work/a",
        exe=f"/usr/bin/{comm}",
        tty=tty,
        tty_nr=34816 if tty else 0,
        elapsed_seconds=10,
        cpu_seconds=1,
    )


if __name__ == "__main__":
    unittest.main()
