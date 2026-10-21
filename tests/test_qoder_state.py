from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_cli_monitor.models import ProcessInfo
from codex_cli_monitor.qoder_state import (
    STATUS_FAILURE,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_WAITING,
    default_qoder_home,
    find_open_segments,
    qoder_home_candidates,
    qoder_is_interactive,
    qoder_session_state,
    qoder_state_health,
    read_qoder_segment_state,
    reset_caches,
)

SESSION_ID = "543b24ff-c716-440b-83e8-1ccaf2762337"
TURN_ID = "78be3945-1efb-4625-b12b-7ca6afdb2447"
CWD = "/work/qoder"


def _process(
    cmdline: tuple[str, ...] = ("qoder",),
    *,
    pid: int = 4321,
    tty_nr: int | None = 34816,
    tty: str | None = "/dev/pts/3",
) -> ProcessInfo:
    return ProcessInfo(
        pid=pid,
        ppid=1,
        comm="qoder",
        state="S",
        cmdline=cmdline,
        cwd=CWD,
        exe="/home/user/.qoder-cn/bin/qoderclicn/qoderclicn-1.1.75",
        tty=tty,
        tty_nr=tty_nr,
        elapsed_seconds=120.0,
        cpu_seconds=1.0,
        started_at=1_700_000_000.0,
        start_ticks=987654,
    )


def _segment_path(home: Path, session_id: str = SESSION_ID) -> Path:
    return (
        home
        / "logs"
        / "sessions"
        / "-work-qoder"
        / session_id
        / "segments"
        / "2026-10-21T12-00-00-000+00-00-run-p4321.jsonl"
    )


def _write_events(path: Path, events: list[dict], *, mode: str = "w") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def _event(
    event_type: str,
    *,
    turn_id: str | None = TURN_ID,
    data: dict | None = None,
    tool_call_id: str | None = None,
    ts: str = "2026-10-21T12:00:00+00:00",
) -> dict:
    event = {"type": event_type, "ts": ts, "level": "info"}
    if turn_id is not None:
        event["turn_id"] = turn_id
    if tool_call_id is not None:
        event["tool_call_id"] = tool_call_id
    event["data"] = data or {}
    return event


def _open_segment(
    proc: Path,
    segment: Path,
    *,
    pid: int = 4321,
    fd: int = 5,
    flags: str | None = "01",
) -> None:
    fd_dir = proc / str(pid) / "fd"
    fd_dir.mkdir(parents=True, exist_ok=True)
    (fd_dir / str(fd)).symlink_to(segment)
    if flags is not None:
        fdinfo = proc / str(pid) / "fdinfo"
        fdinfo.mkdir(parents=True, exist_ok=True)
        (fdinfo / str(fd)).write_text(f"pos:\t0\nflags:\t{flags}\n", encoding="utf-8")


class QoderStateTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_caches()

    def tearDown(self) -> None:
        reset_caches()

    def test_no_open_segment_is_not_displayed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "qoder-home"
            segment = _segment_path(home)
            _write_events(
                segment,
                [_event("turn.started", data={"is_subagent": False})],
            )
            self.assertIsNone(
                qoder_session_state(_process(), home=home, proc_root=root / "proc")
            )

    def test_startup_events_without_a_turn_are_not_displayed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "qoder-home"
            segment = _segment_path(home)
            _write_events(
                segment,
                [
                    _event("session.phase.started", turn_id=None, data={"phase": "qoder_runtime.ready"}),
                    _event("session.phase.finished", turn_id=None, data={"phase": "mcp_context.refresh"}),
                    _event("hook.finished", data={"hook_event_name": "SessionStart"}),
                ],
            )
            _open_segment(root / "proc", segment)
            self.assertIsNone(
                qoder_session_state(_process(), home=home, proc_root=root / "proc")
            )

    def test_main_turn_displays_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "qoder-home"
            segment = _segment_path(home)
            _write_events(
                segment,
                [
                    _event("turn.started", data={"is_subagent": False}),
                    _event("model.request.started"),
                ],
            )
            _open_segment(root / "proc", segment)

            state = qoder_session_state(_process(), home=home, proc_root=root / "proc")

        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.status, STATUS_RUNNING)
        self.assertTrue(state.turn_active)
        self.assertEqual(state.session_id, SESSION_ID)
        self.assertEqual(state.turn_id, TURN_ID)

    def test_permission_request_displays_waiting_until_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "qoder-home"
            segment = _segment_path(home)
            _write_events(
                segment,
                [
                    _event("turn.started", data={"is_subagent": False}),
                    _event(
                        "permission.requested",
                        tool_call_id="call-1",
                        data={"tool_name": "Bash", "ask_message": "ignored"},
                    ),
                ],
            )
            _open_segment(root / "proc", segment)

            waiting = qoder_session_state(
                _process(), home=home, proc_root=root / "proc"
            )
            self.assertIsNotNone(waiting)
            assert waiting is not None
            self.assertEqual(waiting.status, STATUS_WAITING)
            self.assertEqual(waiting.waiting_reason, "Bash")

            _write_events(
                segment,
                [_event("permission.resolved", tool_call_id="call-1")],
                mode="a",
            )
            running = qoder_session_state(
                _process(), home=home, proc_root=root / "proc"
            )

        self.assertIsNotNone(running)
        assert running is not None
        self.assertEqual(running.status, STATUS_RUNNING)
        self.assertIsNone(running.waiting_reason)

    def test_question_tool_displays_waiting_until_it_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "qoder-home"
            segment = _segment_path(home)
            _write_events(
                segment,
                [
                    _event("turn.started", data={"is_subagent": False}),
                    _event(
                        "tool.requested",
                        tool_call_id="call-ask",
                        data={"tool_name": "AskUserQuestion"},
                    ),
                ],
            )
            _open_segment(root / "proc", segment)

            waiting = qoder_session_state(
                _process(), home=home, proc_root=root / "proc"
            )
            self.assertIsNotNone(waiting)
            assert waiting is not None
            self.assertEqual(waiting.status, STATUS_WAITING)
            self.assertEqual(waiting.waiting_reason, "AskUserQuestion")

            _write_events(
                segment,
                [
                    _event(
                        "tool.execution.finished",
                        tool_call_id="call-ask",
                        data={"tool_name": "AskUserQuestion"},
                    ),
                ],
                mode="a",
            )
            running = qoder_session_state(
                _process(), home=home, proc_root=root / "proc"
            )

        self.assertIsNotNone(running)
        assert running is not None
        self.assertEqual(running.status, STATUS_RUNNING)

    def test_plan_mode_question_displays_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "qoder-home"
            segment = _segment_path(home)
            _write_events(
                segment,
                [
                    _event("turn.started", data={"is_subagent": False}),
                    _event(
                        "tool.requested",
                        tool_call_id="call-plan",
                        data={"tool_name": "ExitPlanMode"},
                    ),
                ],
            )
            _open_segment(root / "proc", segment)

            state = qoder_session_state(
                _process(), home=home, proc_root=root / "proc"
            )

        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.status, STATUS_WAITING)
        self.assertEqual(state.waiting_reason, "ExitPlanMode")

    def test_completed_turn_displays_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "qoder-home"
            segment = _segment_path(home)
            _write_events(
                segment,
                [
                    _event("turn.started", data={"is_subagent": False}),
                    _event("turn.finished", data={"reason": "end_turn"}),
                ],
            )
            _open_segment(root / "proc", segment)

            state = qoder_session_state(
                _process(), home=home, proc_root=root / "proc"
            )

        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.status, STATUS_SUCCESS)
        self.assertFalse(state.turn_active)

    def test_error_finish_displays_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "qoder-home"
            segment = _segment_path(home)
            _write_events(
                segment,
                [
                    _event("turn.started", data={"is_subagent": False}),
                    _event(
                        "turn.finished",
                        data={"reason": "error", "error": {"message": "ignored"}},
                    ),
                ],
            )
            _open_segment(root / "proc", segment)

            state = qoder_session_state(
                _process(), home=home, proc_root=root / "proc"
            )

        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.status, STATUS_FAILURE)
        self.assertTrue(state.failed_event)

    def test_new_turn_clears_a_previous_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "qoder-home"
            segment = _segment_path(home)
            _write_events(
                segment,
                [
                    _event("turn.started", data={"is_subagent": False}),
                    _event("turn.finished", data={"reason": "error"}),
                    _event(
                        "turn.started",
                        turn_id="next-turn",
                        data={"is_subagent": False},
                    ),
                ],
            )
            _open_segment(root / "proc", segment)

            state = qoder_session_state(
                _process(), home=home, proc_root=root / "proc"
            )

        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.status, STATUS_RUNNING)
        self.assertEqual(state.turn_id, "next-turn")

    def test_subagent_turns_do_not_change_the_main_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "qoder-home"
            segment = _segment_path(home)
            _write_events(
                segment,
                [
                    _event("turn.started", data={"is_subagent": False}),
                    _event(
                        "turn.started",
                        turn_id="subagent-turn",
                        data={"is_subagent": True},
                    ),
                    _event(
                        "turn.finished",
                        turn_id="subagent-turn",
                        data={"reason": "error"},
                    ),
                ],
            )
            _open_segment(root / "proc", segment)

            state = qoder_session_state(
                _process(), home=home, proc_root=root / "proc"
            )

        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.status, STATUS_RUNNING)
        self.assertFalse(state.failed_event)

    def test_incremental_read_sees_appended_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "qoder-home"
            segment = _segment_path(home)
            _write_events(
                segment,
                [_event("turn.started", data={"is_subagent": False})],
            )
            _open_segment(root / "proc", segment)

            first = qoder_session_state(
                _process(), home=home, proc_root=root / "proc"
            )
            _write_events(
                segment,
                [
                    _event(
                        "permission.requested",
                        tool_call_id="call-2",
                        data={"tool_name": "WebFetch"},
                    )
                ],
                mode="a",
            )
            second = qoder_session_state(
                _process(), home=home, proc_root=root / "proc"
            )

        self.assertEqual(first.status, STATUS_RUNNING)
        self.assertEqual(second.status, STATUS_WAITING)
        self.assertEqual(second.waiting_reason, "WebFetch")

    def test_print_mode_is_not_displayed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "qoder-home"
            segment = _segment_path(home)
            _write_events(
                segment,
                [
                    _event("turn.started", data={"is_subagent": False}),
                    _event("turn.finished", data={"reason": "end_turn"}),
                ],
            )
            _open_segment(root / "proc", segment)

            state = qoder_session_state(
                _process(("qoder", "-p", "hello")),
                home=home,
                proc_root=root / "proc",
            )

        self.assertIsNone(state)

    def test_headless_process_is_not_displayed(self) -> None:
        self.assertFalse(
            qoder_is_interactive(_process(tty_nr=None, tty=None))
        )
        self.assertFalse(
            qoder_is_interactive(_process(tty_nr=0, tty=None))
        )
        self.assertTrue(
            qoder_is_interactive(_process(("qoder", "-i", "hello")))
        )
        self.assertFalse(
            qoder_is_interactive(
                _process(("qoder", "--remote-control", "worker-1"))
            )
        )

    def test_read_only_inherited_segment_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "qoder-home"
            proc = root / "proc"
            old_segment = _segment_path(home, "aaaaaaaa-0000-0000-0000-000000000000")
            current_segment = _segment_path(home)
            _write_events(
                old_segment,
                [
                    _event("turn.started", data={"is_subagent": False}),
                    _event("turn.finished", data={"reason": "end_turn"}),
                ],
            )
            _write_events(
                current_segment,
                [_event("turn.started", data={"is_subagent": False})],
            )
            _open_segment(proc, old_segment, fd=4, flags="00")
            _open_segment(proc, current_segment, fd=5, flags="01")

            segments = find_open_segments(
                _process(), proc_root=proc, homes=(home,)
            )
            state = qoder_session_state(
                _process(), home=home, proc_root=proc
            )

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].session_id, SESSION_ID)
        self.assertEqual(state.status, STATUS_RUNNING)

    def test_health_reports_bounded_directory_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "qoder-home"
            _write_events(
                _segment_path(home),
                [_event("turn.started", data={"is_subagent": False})],
            )
            health = qoder_state_health(home)

        self.assertEqual(
            set(health),
            {
                "home",
                "home_exists",
                "logs_dir_exists",
                "sessions_dir_exists",
                "session_directories",
                "segments",
            },
        )
        self.assertTrue(health["sessions_dir_exists"])
        self.assertEqual(health["session_directories"], 1)
        self.assertEqual(health["segments"], 1)

    def test_home_resolution_honors_cn_environment_and_config_dir(self) -> None:
        with patch.dict(os.environ, {"QODER_CONFIG_DIR": "/custom/qoder"}):
            self.assertEqual(default_qoder_home(), Path("/custom/qoder"))
            self.assertEqual(
                qoder_home_candidates(_process()),
                (Path("/custom/qoder"),),
            )
        self.assertEqual(
            qoder_home_candidates(
                _process(("qoder", "--config-dir", "/flag/qoder")),
                env={},
            ),
            (Path("/flag/qoder"),),
        )


class QoderParserCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_caches()

    def test_permission_resolved_without_id_releases_oldest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            segment = Path(tmp) / "segment.jsonl"
            _write_events(
                segment,
                [
                    _event("turn.started", data={"is_subagent": False}),
                    _event(
                        "permission.requested",
                        tool_call_id=None,
                        data={"tool_name": "Bash"},
                    ),
                    _event("permission.resolved", tool_call_id=None),
                ],
            )
            state = read_qoder_segment_state(segment)
        self.assertEqual(state.status, STATUS_RUNNING)

    def test_model_resume_clears_question_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            segment = Path(tmp) / "segment.jsonl"
            _write_events(
                segment,
                [
                    _event("turn.started", data={"is_subagent": False}),
                    _event(
                        "tool.requested",
                        tool_call_id="call-ask",
                        data={"tool_name": "AskUserQuestion"},
                    ),
                    _event("model.request.started"),
                ],
            )
            state = read_qoder_segment_state(segment)
        self.assertEqual(state.status, STATUS_RUNNING)


if __name__ == "__main__":
    unittest.main()
