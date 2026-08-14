from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_cli_monitor.hook_state import append_hook_event
from codex_cli_monitor.monitor import discover_sessions, inspect_runtime
from codex_cli_monitor.terminal_state import MAX_INITIAL_TAIL_BYTES


class MonitorTests(unittest.TestCase):
    def test_new_process_is_monitored_but_not_displayed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp) / "proc"
            proc.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")

            sessions = discover_sessions(proc, sample_window=1, sleep=_fail_sleep)

        self.assertEqual(sessions, ())

    def test_session_start_alone_is_not_displayed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, _home, hook_log = _runtime(tmp)
            append_hook_event(
                "session_start",
                cwd="/work/a",
                ppid=100,
                path=hook_log,
                hook_payload={"session_id": "session-a"},
            )

            sessions = discover_sessions(proc, hook_log=hook_log)

        self.assertEqual(sessions, ())

    def test_prompt_hook_displays_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "运行中")
        self.assertEqual(sessions[0].inference.status, "running_hook")
        self.assertEqual(sessions[0].connections, ())

    def test_stop_hook_displays_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")
            _hook(hook_log, "stop", "session-a", "turn-a")

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "成功")
        self.assertEqual(sessions[0].inference.status, "success_hook")

    def test_codex_exec_process_is_excluded_even_with_prompt_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            # 增加一个 exec 形态的 codex 进程（pid 101），并给它提交提示词 hook
            _write_process(
                proc,
                101,
                "codex",
                "S",
                1,
                ["codex", "exec", "--model", "test-model"],
                "/work/exec",
            )
            append_hook_event(
                "user_prompt_submit",
                cwd="/work/exec",
                ppid=101,
                path=hook_log,
                hook_payload={"session_id": "session-exec", "turn_id": "turn-exec"},
            )

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        # exec codex(101) 即使有匹配的提示词 hook 也被排除；默认 codex(100) 无 hook 不显示
        self.assertEqual(sessions, ())

    def test_normal_codex_process_still_displayed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "运行中")

    def test_turn_complete_error_displays_failure_without_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")
            _write_terminal(
                home,
                "session-a",
                "turn-a",
                "task_complete",
                error={"message": "request failed"},
            )

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "失败")
        self.assertEqual(sessions[0].inference.status, "failure_terminal")
        self.assertTrue(sessions[0].state_activity.failed_event)

    def test_turn_aborted_displays_failure_without_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")
            _write_terminal(home, "session-a", "turn-a", "turn_aborted")

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "失败")

    def test_turn_complete_without_error_displays_success_when_stop_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")
            _write_terminal(home, "session-a", "turn-a", "task_complete", error=None)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "成功")

    def test_diagnostic_error_text_is_not_classified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")
            session = _session_path(home, "session-a")
            session.parent.mkdir(parents=True)
            session.write_text(
                '{"timestamp":"2026-07-29T00:00:00Z","type":"event_msg",'
                '"payload":{"type":"agent_message","message":"API error 500"}}\n',
                encoding="utf-8",
            )

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "运行中")

    def test_new_prompt_is_not_overridden_by_previous_turn_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-old")
            _write_terminal(home, "session-a", "turn-old", "turn_aborted")
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-new")

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "运行中")

    def test_same_pid_active_session_beats_later_old_session_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            base = time.time() - 10
            _hook(
                hook_log,
                "user_prompt_submit",
                "session-old",
                "turn-old",
                timestamp=base,
            )
            _hook(
                hook_log,
                "user_prompt_submit",
                "session-new",
                "turn-new",
                timestamp=base + 1,
            )
            _hook(
                hook_log,
                "stop",
                "session-old",
                "turn-old",
                timestamp=base + 2,
            )

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "运行中")
        self.assertEqual(sessions[0].hook_state.session_id, "session-new")

    def test_goal_continuation_task_started_on_open_session_fd_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            old_session = "019fb158-36bb-7440-9128-2e4e0f7c6168"
            new_session = "019fb176-333f-7071-aa87-1d1837579794"
            _hook(hook_log, "user_prompt_submit", old_session, "turn-old")
            _hook(hook_log, "stop", old_session, "turn-old")
            old_path = _write_terminal(
                home, old_session, "turn-old", "task_complete", error=None
            )
            new_path = _write_terminal(
                home, new_session, "turn-auto", "task_started"
            )
            _extend_with_sparse_gap(
                new_path,
                MAX_INITIAL_TAIL_BYTES + 64 * 1024,
            )
            _bind_open_session(proc, 100, old_path, 14)
            _bind_open_session(proc, 100, new_path, 25)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions[0].display_status, "运行中")
        self.assertEqual(sessions[0].inference.status, "running_terminal")
        self.assertEqual(sessions[0].state_activity.session_id, new_session)
        self.assertIsNone(sessions[0].hook_state)
        self.assertEqual(sessions[0].binding_method, "process_fd_session_id")

    def test_direct_goal_task_started_displays_without_prompt_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            session_id = "019fb176-333f-7071-aa87-1d1837579794"
            path = _write_terminal(home, session_id, "turn-auto", "task_started")
            _bind_open_session(proc, 100, path, 14)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "运行中")
        self.assertEqual(sessions[0].inference.status, "running_terminal")
        self.assertIsNone(sessions[0].hook_state)
        self.assertEqual(sessions[0].binding_method, "process_fd_session_id")

    def test_direct_goal_completion_remains_displayed_without_prompt_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            session_id = "019fb176-333f-7071-aa87-1d1837579794"
            path = _write_terminal(home, session_id, "turn-auto", "task_started")
            _append_terminal(path, "turn-auto", "task_complete", error=None)
            _bind_open_session(proc, 100, path, 14)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].display_status, "成功")
        self.assertEqual(sessions[0].inference.status, "success_terminal")

    def test_process_does_not_inherit_goal_started_before_process_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            session_id = "019fb176-333f-7071-aa87-1d1837579794"
            path = _write_terminal(
                home,
                session_id,
                "turn-old",
                "task_started",
                timestamp=time.time() - 300,
            )
            _bind_open_session(proc, 100, path, 14)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions, ())

    def test_direct_goal_requires_known_process_start_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            (proc / "uptime").unlink()
            session_id = "019fb176-333f-7071-aa87-1d1837579794"
            path = _write_terminal(home, session_id, "turn-auto", "task_started")
            _bind_open_session(proc, 100, path, 14)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions, ())

    def test_same_directory_processes_bind_by_pid_and_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            home = root / "codex-home"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            home.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
            _write_process(proc, 200, "codex", "S", 1, ["codex"], "/work/a")
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a", ppid=100)
            _hook(hook_log, "user_prompt_submit", "session-b", "turn-b", ppid=200)
            _write_terminal(home, "session-a", "turn-a", "task_complete", error=None)
            _write_terminal(home, "session-b", "turn-b", "turn_aborted")

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        by_pid = {session.root.pid: session for session in sessions}
        self.assertEqual(by_pid[100].display_status, "成功")
        self.assertEqual(by_pid[200].display_status, "失败")
        self.assertNotEqual(
            by_pid[100].state_activity.relative_path,
            by_pid[200].state_activity.relative_path,
        )

    def test_process_exit_removes_stale_open_hook_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")
            self.assertEqual(
                len(discover_sessions(proc, codex_home=home, hook_log=hook_log)),
                1,
            )
            for child in tuple(proc.iterdir()):
                if child.name.isdigit():
                    _remove_fake_process(child)

            sessions = discover_sessions(proc, codex_home=home, hook_log=hook_log)

        self.assertEqual(sessions, ())

    def test_detached_or_stopped_process_is_not_displayed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            hook_log = root / "hooks.jsonl"
            proc.mkdir()
            _write_common_proc(proc)
            _write_process(proc, 100, "codex", "T", 1, ["codex"], "/work/a")
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")

            sessions = discover_sessions(proc, hook_log=hook_log)

        self.assertEqual(sessions, ())

    def test_runtime_scan_does_not_sample_cpu_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")
            with patch(
                "codex_cli_monitor.procfs.read_network_connections",
                side_effect=AssertionError("network scan must not run"),
            ):
                sessions = discover_sessions(
                    proc,
                    sample_window=200,
                    codex_home=home,
                    hook_log=hook_log,
                    sleep=_fail_sleep,
                )

        self.assertEqual(len(sessions), 1)

    def test_inspect_runtime_keeps_state_summary_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proc, home, hook_log = _runtime(tmp)
            _hook(hook_log, "user_prompt_submit", "session-a", "turn-a")
            _write_terminal(home, "session-a", "turn-a", "task_complete", error=None)

            sessions, summary = inspect_runtime(
                proc,
                codex_home=home,
                hook_log=hook_log,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(summary.codex_home, str(home))


def _runtime(tmp: str) -> tuple[Path, Path, Path, Path]:
    root = Path(tmp)
    proc = root / "proc"
    home = root / "codex-home"
    hook_log = root / "hooks.jsonl"
    proc.mkdir()
    home.mkdir()
    _write_common_proc(proc)
    _write_process(proc, 100, "codex", "S", 1, ["codex"], "/work/a")
    return root, proc, home, hook_log


def _hook(
    path: Path,
    event: str,
    session_id: str,
    turn_id: str,
    *,
    ppid: int = 100,
    timestamp: float | None = None,
) -> None:
    append_hook_event(
        event,
        cwd="/work/a",
        ppid=ppid,
        timestamp=timestamp,
        path=path,
        hook_payload={"session_id": session_id, "turn_id": turn_id},
    )


def _write_terminal(
    home: Path,
    session_id: str,
    turn_id: str,
    event_type: str,
    *,
    error: object = "absent",
    timestamp: float | None = None,
) -> Path:
    payload: dict[str, object] = {"type": event_type, "turn_id": turn_id}
    if error != "absent":
        payload["error"] = error
    path = _session_path(home, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(timestamp),
        )
        if timestamp is not None
        else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "type": "event_msg",
        "payload": payload,
    }
    import json

    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _append_terminal(
    path: Path,
    turn_id: str,
    event_type: str,
    *,
    error: object = "absent",
) -> None:
    payload: dict[str, object] = {"type": event_type, "turn_id": turn_id}
    if error != "absent":
        payload["error"] = error
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "type": "event_msg",
        "payload": payload,
    }
    import json

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _session_path(home: Path, session_id: str) -> Path:
    return home / "sessions" / "2026" / "07" / "29" / f"rollout-{session_id}.jsonl"


def _bind_open_session(
    proc: Path,
    pid: int,
    session_path: Path,
    fd: int,
) -> None:
    (proc / str(pid) / "fd" / str(fd)).symlink_to(session_path)


def _extend_with_sparse_gap(path: Path, size: int) -> None:
    with path.open("r+b") as handle:
        handle.seek(0, 2)
        handle.seek(size, 1)
        handle.write(b"\n{}\n")


def _fail_sleep(_: float) -> None:
    raise AssertionError("CPU sample sleep must not run")


def _remove_fake_process(path: Path) -> None:
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    path.rmdir()


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
    (pid_dir / "fd").mkdir(parents=True, exist_ok=True)
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
