from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from codex_cli_monitor.hook_state import (
    append_hook_event,
    load_hook_events,
    summarize_hook_events,
)


class HookStateTests(unittest.TestCase):
    def test_summarize_open_turn_and_tool_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            append_hook_event("user_prompt_submit", cwd="/work/a", path=path)
            append_hook_event("pre_tool_use", tool="Bash", cwd="/work/a", path=path)

            states = summarize_hook_events(load_hook_events(path))

        state = states[str(Path("/work/a").resolve())][0]
        self.assertTrue(state.in_turn)
        self.assertEqual(state.active_tool_count, 1)
        self.assertEqual(state.last_tool, "Bash")

    def test_stop_closes_turn_and_clears_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            append_hook_event("user_prompt_submit", cwd="/work/a", path=path)
            append_hook_event("pre_tool_use", tool="Bash", cwd="/work/a", path=path)
            append_hook_event("stop", cwd="/work/a", path=path)

            states = summarize_hook_events(load_hook_events(path))

        state = states[str(Path("/work/a").resolve())][0]
        self.assertFalse(state.in_turn)
        self.assertEqual(state.active_tool_count, 0)
        self.assertEqual(state.last_event, "stop")

    def test_summarize_tracks_turn_stop_and_new_turn_times(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = time.time() - 60
            path = Path(tmp) / "hooks.jsonl"
            append_hook_event(
                "session_start",
                cwd="/work/a",
                ppid=100,
                timestamp=base,
                path=path,
            )
            append_hook_event(
                "user_prompt_submit",
                cwd="/work/a",
                ppid=100,
                timestamp=base + 10,
                path=path,
            )
            append_hook_event(
                "stop",
                cwd="/work/a",
                ppid=100,
                timestamp=base + 20,
                path=path,
            )
            append_hook_event(
                "user_prompt_submit",
                cwd="/work/a",
                ppid=100,
                timestamp=base + 30,
                path=path,
            )

            states = summarize_hook_events(load_hook_events(path))

        state = states[str(Path("/work/a").resolve())][0]
        self.assertTrue(state.in_turn)
        self.assertEqual(state.turn_started_at, base + 30)
        self.assertIsNone(state.last_stopped_at)
        self.assertEqual(state.session_started_at, base)

    def test_same_cwd_keeps_separate_codex_parent_process_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            append_hook_event("user_prompt_submit", cwd="/work/a", ppid=100, path=path)
            append_hook_event("stop", cwd="/work/a", ppid=100, path=path)
            append_hook_event("session_start", cwd="/work/a", ppid=200, path=path)

            states = summarize_hook_events(load_hook_events(path))

        states_for_cwd = states[str(Path("/work/a").resolve())]
        self.assertEqual({state.codex_pid for state in states_for_cwd}, {100, 200})
        latest = states_for_cwd[0]
        self.assertEqual(latest.codex_pid, 200)
        self.assertEqual(latest.last_event, "session_start")


if __name__ == "__main__":
    unittest.main()
