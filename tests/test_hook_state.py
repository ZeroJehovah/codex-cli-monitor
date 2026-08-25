from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from unittest.mock import patch

from codex_cli_monitor.hook_state import (
    append_hook_event,
    hook_log_health,
    load_hook_events,
    summarize_hook_events,
)


def _append_events_in_process(arguments: tuple[str, int]) -> int:
    path, worker = arguments
    completed = 0
    for index in range(500):
        if append_hook_event(
            "user_prompt_submit",
            cwd="/work/concurrent",
            ppid=worker,
            timestamp=time.time() + index / 1000,
            path=Path(path),
            hook_payload={"session_id": f"s-{worker}", "turn_id": f"t-{worker}-{index}"},
        ):
            completed += 1
    return completed


class HookStateTests(unittest.TestCase):
    def test_session_start_alone_has_no_displayable_turn_activity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            append_hook_event(
                "session_start",
                cwd="/work/a",
                ppid=100,
                path=path,
                hook_payload={"session_id": "session-a"},
            )
            state = summarize_hook_events(load_hook_events(path))[
                str(Path("/work/a").resolve())
            ][0]

        self.assertFalse(state.has_turn_activity)

    def test_session_start_diagnostic_does_not_close_open_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            append_hook_event(
                "user_prompt_submit",
                cwd="/work/a",
                ppid=100,
                path=path,
                hook_payload={"session_id": "session-a", "turn_id": "turn-a"},
            )
            append_hook_event(
                "session_start",
                cwd="/work/a",
                ppid=100,
                path=path,
                hook_payload={"session_id": "session-a"},
            )
            state = summarize_hook_events(load_hook_events(path))[
                str(Path("/work/a").resolve())
            ][0]

        self.assertTrue(state.has_turn_activity)
        self.assertTrue(state.in_turn)
        self.assertEqual(state.turn_id, "turn-a")

    def test_schema_v1_v2_corruption_and_truncated_tail_are_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            path.write_bytes(
                b'{"event":"session_start","timestamp":1,"pid":1,"ppid":2,"cwd":"/work/a"}\n'
                b'broken\x00line\n'
                b'{"schema_version":2,"event":"user_prompt_submit","timestamp":2,'
                b'"pid":1,"ppid":2,"cwd":"/work/a","session_id":"s","turn_id":"t"}\n'
                b'{"event":"stop"'
            )
            events = load_hook_events(path, max_age_seconds=10**12)
            health = hook_log_health(path)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].schema_version, 1)
        self.assertEqual(events[1].turn_id, "t")
        self.assertEqual(health["schema_versions"], [1, 2])
        self.assertEqual(health["corrupt_lines"], 2)

    def test_parallel_tool_ids_handle_duplicates_and_out_of_order_posts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            common = {"session_id": "s", "turn_id": "t"}
            append_hook_event("user_prompt_submit", cwd="/work/a", ppid=100, path=path, hook_payload=common)
            append_hook_event(
                "pre_tool_use", cwd="/work/a", ppid=100, path=path,
                hook_payload={**common, "tool_name": "Bash", "tool_use_id": "a"},
            )
            append_hook_event(
                "pre_tool_use", cwd="/work/a", ppid=100, path=path,
                hook_payload={**common, "tool_name": "MCP", "tool_use_id": "b"},
            )
            append_hook_event(
                "pre_tool_use", cwd="/work/a", ppid=100, path=path,
                hook_payload={**common, "tool_name": "MCP", "tool_use_id": "b"},
            )
            append_hook_event(
                "post_tool_use", cwd="/work/a", ppid=100, path=path,
                hook_payload={**common, "tool_use_id": "missing"},
            )
            append_hook_event(
                "post_tool_use", cwd="/work/a", ppid=100, path=path,
                hook_payload={**common, "tool_use_id": "a"},
            )
            state = summarize_hook_events(load_hook_events(path))[str(Path("/work/a").resolve())][0]

        self.assertEqual(state.active_tool_count, 1)
        self.assertEqual(state.active_tool_use_ids, ("b",))

    def test_old_turn_stop_does_not_close_new_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            append_hook_event(
                "user_prompt_submit", cwd="/work/a", ppid=100, timestamp=1, path=path,
                hook_payload={"session_id": "s", "turn_id": "old"},
            )
            append_hook_event(
                "user_prompt_submit", cwd="/work/a", ppid=100, timestamp=2, path=path,
                hook_payload={"session_id": "s", "turn_id": "new"},
            )
            append_hook_event(
                "stop", cwd="/work/a", ppid=100, timestamp=3, path=path,
                hook_payload={"session_id": "s", "turn_id": "old"},
            )
            state = summarize_hook_events(load_hook_events(path, max_age_seconds=10**12))[
                str(Path("/work/a").resolve())
            ][0]

        self.assertTrue(state.in_turn)
        self.assertEqual(state.turn_id, "new")
        self.assertEqual(state.last_stopped_turn_id, "old")

    def test_same_pid_keeps_different_session_states_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            append_hook_event(
                "user_prompt_submit", cwd="/work/a", ppid=100, timestamp=1, path=path,
                hook_payload={"session_id": "session-old", "turn_id": "turn-old"},
            )
            append_hook_event(
                "user_prompt_submit", cwd="/work/a", ppid=100, timestamp=2, path=path,
                hook_payload={"session_id": "session-new", "turn_id": "turn-new"},
            )
            append_hook_event(
                "stop", cwd="/work/a", ppid=100, timestamp=3, path=path,
                hook_payload={"session_id": "session-old", "turn_id": "turn-old"},
            )

            states = summarize_hook_events(
                load_hook_events(path, max_age_seconds=10**12)
            )[str(Path("/work/a").resolve())]

        by_session = {state.session_id: state for state in states}
        self.assertEqual(set(by_session), {"session-old", "session-new"})
        self.assertFalse(by_session["session-old"].in_turn)
        self.assertTrue(by_session["session-new"].in_turn)
        self.assertEqual(by_session["session-new"].turn_id, "turn-new")

    def test_concurrent_process_writes_are_complete_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            workers = 40
            with ProcessPoolExecutor(max_workers=workers) as executor:
                completed = sum(executor.map(_append_events_in_process, ((str(path), i) for i in range(workers))))
            lines = path.read_bytes().splitlines()

        self.assertEqual(len(lines), completed)
        self.assertGreater(completed, 15_000)
        self.assertTrue(all(isinstance(json.loads(line), dict) for line in lines))
        self.assertNotIn(b"\x00", b"".join(lines))

    def test_low_frequency_append_p95_is_under_twenty_milliseconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            durations = []
            for index in range(100):
                started = time.perf_counter()
                append_hook_event(
                    "user_prompt_submit",
                    cwd="/work/a",
                    ppid=100,
                    path=path,
                    hook_payload={"session_id": "s", "turn_id": f"t-{index}"},
                )
                durations.append(time.perf_counter() - started)
        p95 = sorted(durations)[94]
        self.assertLess(p95, 0.020)

    def test_lock_rotation_and_disk_errors_are_fail_open(self) -> None:
        failures = (
            (patch("codex_cli_monitor.hook_state._acquire_lock", side_effect=TimeoutError()), False),
            (patch("codex_cli_monitor.hook_state._rotate_hook_log", side_effect=OSError("full")), True),
            (patch("codex_cli_monitor.hook_state.os.write", side_effect=OSError("full")), False),
        )
        for failure, prefill in failures:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "hooks.jsonl"
                if prefill:
                    path.write_bytes(b"x" * 65536)
                with patch.dict(os.environ, {"CODEX_MONITOR_HOOK_LOG_MAX_BYTES": "65536"}), failure:
                    result = append_hook_event("stop", cwd="/work/a", path=path)
                self.assertFalse(result)

    def test_rotation_bounds_total_size_and_preserves_recent_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            with patch.dict(os.environ, {"CODEX_MONITOR_HOOK_LOG_MAX_BYTES": "65536"}):
                for index in range(900):
                    append_hook_event(
                        "user_prompt_submit", cwd="/work/a", ppid=100, path=path,
                        hook_payload={"session_id": "s", "turn_id": f"turn-{index}-" + "x" * 100},
                    )
            total = sum(item.stat().st_size for item in path.parent.glob("hooks.jsonl*"))
            events = load_hook_events(path)
            rotated = path.with_name("hooks.jsonl.1").exists()

        self.assertLess(total, 4 * 65536)
        self.assertEqual(events[-1].turn_id, "turn-899-" + "x" * 100)
        self.assertTrue(rotated)

    def test_sparse_100_mib_history_reads_only_bounded_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            with path.open("wb") as handle:
                handle.write(b'{"event":"session_start","timestamp":1,"cwd":"/old"}\n')
                handle.seek(100 * 1024 * 1024)
                handle.write(
                    b'\n{"schema_version":2,"event":"stop","timestamp":9999999999,'
                    b'"cwd":"/work/a","turn_id":"latest"}\n'
                )
            events = load_hook_events(path)
            health = hook_log_health(path)

        self.assertEqual(events[-1].turn_id, "latest")
        self.assertLessEqual(health["tail_bytes_read"], 4 * 1024 * 1024)

    def test_cache_key_detects_inode_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            replacement = Path(tmp) / "replacement"
            append_hook_event("session_start", cwd="/work/a", timestamp=time.time(), path=path)
            first = load_hook_events(path)
            append_hook_event("stop", cwd="/work/b", timestamp=time.time(), path=replacement)
            os.replace(replacement, path)
            second = load_hook_events(path)

        self.assertEqual(first[0].event, "session_start")
        self.assertEqual(second[0].event, "stop")
    def test_load_hook_events_reuses_unchanged_file_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            append_hook_event("session_start", cwd="/work/a", ppid=100, path=path)

            first = load_hook_events(path)
            with patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("unchanged hook log should use cache"),
            ):
                second = load_hook_events(path)

        self.assertEqual(first, second)

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

    def test_session_start_payload_keeps_start_source_and_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            append_hook_event(
                "session_start",
                cwd="/work/a",
                ppid=100,
                path=path,
                hook_payload={
                    "source": "clear",
                    "session_id": "019f-new",
                    "prompt": "not logged",
                },
            )

            events = load_hook_events(path)
            states = summarize_hook_events(events)

        self.assertEqual(events[0].hook_source, "clear")
        self.assertEqual(events[0].session_id, "019f-new")
        payload = events[0].to_dict()
        self.assertNotIn("prompt", payload)
        state = states[str(Path("/work/a").resolve())][0]
        self.assertEqual(state.session_start_source, "clear")
        self.assertEqual(state.session_id, "019f-new")

    def test_later_hook_payload_updates_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            append_hook_event(
                "session_start",
                cwd="/work/a",
                ppid=100,
                path=path,
            )
            append_hook_event(
                "user_prompt_submit",
                cwd="/work/a",
                ppid=100,
                path=path,
                hook_payload={"session_id": "019f-turn"},
            )
            append_hook_event(
                "stop",
                cwd="/work/a",
                ppid=100,
                path=path,
                hook_payload={"session_id": "019f-turn"},
            )

            states = summarize_hook_events(load_hook_events(path))

        state = states[str(Path("/work/a").resolve())][0]
        self.assertEqual(state.last_event, "stop")
        self.assertEqual(state.session_id, "019f-turn")

    def test_permission_request_opens_a_pending_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            append_hook_event("user_prompt_submit", cwd="/work/a", path=path)
            append_hook_event(
                "permission_request",
                tool="Bash",
                cwd="/work/a",
                path=path,
            )

            states = summarize_hook_events(load_hook_events(path))

        state = states[str(Path("/work/a").resolve())][0]
        self.assertTrue(state.in_turn)
        self.assertTrue(state.awaiting_decision)
        self.assertEqual(state.permission_tool, "Bash")
        self.assertIsNotNone(state.permission_pending_at)
        self.assertTrue(state.to_dict()["awaiting_decision"])

    def test_permission_request_alone_opens_a_displayable_turn(self) -> None:
        # A resumed session can reach an approval prompt without this monitor
        # ever seeing the UserPromptSubmit that opened the turn.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            append_hook_event(
                "permission_request",
                tool="Bash",
                cwd="/work/a",
                path=path,
            )

            states = summarize_hook_events(load_hook_events(path))

        state = states[str(Path("/work/a").resolve())][0]
        self.assertTrue(state.has_turn_activity)
        self.assertTrue(state.awaiting_decision)
        self.assertIsNotNone(state.turn_started_at)

    def test_post_tool_use_answers_a_pending_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            append_hook_event("user_prompt_submit", cwd="/work/a", path=path)
            append_hook_event(
                "permission_request",
                tool="Bash",
                cwd="/work/a",
                path=path,
            )
            append_hook_event("post_tool_use", tool="Bash", cwd="/work/a", path=path)

            states = summarize_hook_events(load_hook_events(path))

        state = states[str(Path("/work/a").resolve())][0]
        self.assertTrue(state.in_turn)
        self.assertFalse(state.awaiting_decision)
        self.assertIsNone(state.permission_pending_at)

    def test_stop_and_new_prompt_clear_a_pending_decision(self) -> None:
        for closing_event in ("stop", "user_prompt_submit"):
            with self.subTest(event=closing_event):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "hooks.jsonl"
                    append_hook_event("user_prompt_submit", cwd="/work/a", path=path)
                    append_hook_event(
                        "permission_request",
                        tool="Bash",
                        cwd="/work/a",
                        path=path,
                    )
                    append_hook_event(closing_event, cwd="/work/a", path=path)

                    states = summarize_hook_events(load_hook_events(path))

                state = states[str(Path("/work/a").resolve())][0]
                self.assertFalse(state.awaiting_decision)
                self.assertIsNone(state.permission_pending_at)
                self.assertIsNone(state.permission_tool)

    def test_mismatched_turn_stop_keeps_a_live_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.jsonl"
            append_hook_event(
                "user_prompt_submit",
                cwd="/work/a",
                ppid=100,
                path=path,
                hook_payload={"session_id": "s-a", "turn_id": "turn-2"},
            )
            append_hook_event(
                "permission_request",
                tool="Bash",
                cwd="/work/a",
                ppid=100,
                path=path,
                hook_payload={"session_id": "s-a", "turn_id": "turn-2"},
            )
            append_hook_event(
                "stop",
                cwd="/work/a",
                ppid=100,
                path=path,
                hook_payload={"session_id": "s-a", "turn_id": "turn-1"},
            )

            states = summarize_hook_events(load_hook_events(path))

        state = states[str(Path("/work/a").resolve())][0]
        self.assertTrue(state.in_turn)
        self.assertTrue(state.awaiting_decision)


if __name__ == "__main__":
    unittest.main()
