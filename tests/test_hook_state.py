from __future__ import annotations

import tempfile
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

        state = states[str(Path("/work/a").resolve())]
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

        state = states[str(Path("/work/a").resolve())]
        self.assertFalse(state.in_turn)
        self.assertEqual(state.active_tool_count, 0)
        self.assertEqual(state.last_event, "stop")


if __name__ == "__main__":
    unittest.main()
