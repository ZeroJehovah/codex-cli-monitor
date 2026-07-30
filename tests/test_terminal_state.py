from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from codex_cli_monitor.hook_state import HookSessionState
from codex_cli_monitor.terminal_state import (
    MAX_INCREMENTAL_READ_BYTES,
    _TAIL_CACHE,
    scan_process_terminal_activities,
    scan_terminal_activity,
)


class TerminalStateTests(unittest.TestCase):
    def test_reads_only_structured_terminal_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _path(home, "session-a")
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "timestamp": _now(),
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": "API error 500",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            activity = scan_terminal_activity(_state("session-a", "turn-a"), home)

        self.assertIsNotNone(activity)
        self.assertFalse(activity.terminal_event)
        self.assertFalse(activity.failed_event)

    def test_incremental_append_updates_cached_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _path(home, "session-a")
            path.parent.mkdir(parents=True)
            _append_terminal(path, "turn-a", "task_complete", error=None)
            state = _state("session-a", "turn-a")

            first = scan_terminal_activity(state, home)
            first_offset = _TAIL_CACHE[str(path)].offset
            _append_terminal(path, "turn-a", "turn_aborted")
            second = scan_terminal_activity(state, home)
            second_offset = _TAIL_CACHE[str(path)].offset
            final_size = path.stat().st_size

        self.assertFalse(first.failed_event)
        self.assertTrue(second.failed_event)
        self.assertGreater(second_offset, first_offset)
        self.assertEqual(second_offset, final_size)

    def test_initial_read_is_bounded_to_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _path(home, "session-a")
            path.parent.mkdir(parents=True)
            with path.open("wb") as handle:
                handle.write(b"x" * (MAX_INCREMENTAL_READ_BYTES + 4096))
                handle.write(b"\n")
                handle.write(_terminal_line("turn-a", "turn_aborted"))
            state = _state("session-a", "turn-a")

            activity = scan_terminal_activity(state, home)
            cached = _TAIL_CACHE[str(path)]
            final_size = path.stat().st_size

        self.assertTrue(activity.failed_event)
        self.assertEqual(cached.offset, final_size)

    def test_known_turn_id_conflict_never_uses_newer_other_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _path(home, "session-a")
            path.parent.mkdir(parents=True)
            _append_terminal(path, "turn-old", "turn_aborted")

            activity = scan_terminal_activity(_state("session-a", "turn-new"), home)

        self.assertFalse(activity.terminal_event)

    def test_task_started_is_active_without_being_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _path(home, "session-a")
            path.parent.mkdir(parents=True)
            _append_terminal(path, "turn-a", "task_started")

            activity = scan_terminal_activity(_state("session-a", "turn-a"), home)

        self.assertTrue(activity.turn_active)
        self.assertFalse(activity.terminal_event)
        self.assertFalse(activity.failed_event)

    def test_process_fd_binds_exact_open_session_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "codex-home"
            proc = root / "proc"
            fd_dir = proc / "100" / "fd"
            fd_dir.mkdir(parents=True)
            session_id = "019fb176-333f-7071-aa87-1d1837579794"
            path = _path(home, session_id)
            path.parent.mkdir(parents=True)
            _append_terminal(path, "turn-auto", "task_started")
            (fd_dir / "14").symlink_to(path)

            activities = scan_process_terminal_activities(
                100,
                proc_root=proc,
                codex_home=home,
                cwd="/work/a",
            )
            _append_terminal(path, "turn-auto", "task_complete", error=None)
            completed = scan_process_terminal_activities(
                100,
                proc_root=proc,
                codex_home=home,
                cwd="/work/a",
            )

        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0].session_id, session_id)
        self.assertEqual(activities[0].turn_id, "turn-auto")
        self.assertTrue(activities[0].turn_active)
        self.assertFalse(completed[0].turn_active)
        self.assertTrue(completed[0].terminal_event)
        self.assertFalse(completed[0].failed_event)


def _state(session_id: str, turn_id: str) -> HookSessionState:
    return HookSessionState(
        cwd="/work/a",
        updated_at=time.time(),
        last_event="user_prompt_submit",
        in_turn=True,
        has_turn_activity=True,
        turn_started_at=time.time() - 1,
        session_id=session_id,
        turn_id=turn_id,
        codex_pid=100,
    )


def _path(home: Path, session_id: str) -> Path:
    return home / "sessions" / "2026" / "07" / "29" / f"rollout-{session_id}.jsonl"


def _append_terminal(
    path: Path,
    turn_id: str,
    event_type: str,
    *,
    error: object = "absent",
) -> None:
    with path.open("ab") as handle:
        handle.write(_terminal_line(turn_id, event_type, error=error))


def _terminal_line(
    turn_id: str,
    event_type: str,
    *,
    error: object = "absent",
) -> bytes:
    payload: dict[str, object] = {"type": event_type, "turn_id": turn_id}
    if error != "absent":
        payload["error"] = error
    return (
        json.dumps(
            {"timestamp": _now(), "type": "event_msg", "payload": payload}
        )
        + "\n"
    ).encode()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    unittest.main()
