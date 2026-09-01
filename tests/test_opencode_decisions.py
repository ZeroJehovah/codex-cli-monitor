from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_cli_monitor.opencode_decisions import (
    DECISION_LOG_ENV,
    default_opencode_decision_log_path,
    find_pending_decision,
    load_decision_records,
    opencode_decision_log_health,
    pending_decisions,
)


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _asked(**overrides: object) -> dict:
    record = {
        "schema_version": 1,
        "event": "permission.asked",
        "kind": "permission",
        "timestamp": time.time() - 5.0,
        "pid": 5000,
        "directory": "/work/a",
        "session_id": "ses_1",
        "request_id": "per_1",
        "category": "bash",
    }
    record.update(overrides)
    return record


def _replied(**overrides: object) -> dict:
    record = {
        "schema_version": 1,
        "event": "permission.replied",
        "kind": None,
        "timestamp": time.time() - 1.0,
        "pid": 5000,
        "directory": "/work/a",
        "session_id": "ses_1",
        "request_id": "per_1",
        "category": None,
    }
    record.update(overrides)
    return record


class DecisionLogPathTests(unittest.TestCase):
    def test_env_override_wins(self) -> None:
        path = default_opencode_decision_log_path({DECISION_LOG_ENV: "/tmp/d.jsonl"})
        self.assertEqual(path, Path("/tmp/d.jsonl"))

    def test_state_home_default_matches_the_plugin(self) -> None:
        path = default_opencode_decision_log_path({"XDG_STATE_HOME": "/state"})
        self.assertEqual(path, Path("/state/opencode-cli-monitor/decisions.jsonl"))

    def test_plugin_and_reader_agree_on_the_default_path(self) -> None:
        # The plugin computes this path in JavaScript, so the two literals must
        # not drift apart.
        source = (
            Path(__file__).resolve().parents[1]
            / "assets"
            / "opencode"
            / "codex-monitor-decisions.js"
        ).read_text(encoding="utf-8")
        self.assertIn('"opencode-cli-monitor", "decisions.jsonl"', source)
        self.assertIn("OPENCODE_MONITOR_DECISION_LOG", source)


class PendingDecisionTests(unittest.TestCase):
    def test_missing_log_yields_no_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            self.assertEqual(pending_decisions(path), ())
            self.assertEqual(load_decision_records(path), ())

    def test_unanswered_ask_is_pending_with_its_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            _write(path, [_asked()])
            decisions = pending_decisions(path)

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].kind, "permission")
        self.assertEqual(decisions[0].session_id, "ses_1")
        self.assertEqual(decisions[0].reason, "bash")

    def test_reply_clears_the_matching_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            _write(path, [_asked(), _replied()])
            self.assertEqual(pending_decisions(path), ())

    def test_reply_without_a_matching_request_id_clears_by_session(self) -> None:
        # The ask names the request `id` and the reply names it `requestID`, so
        # an identifier that stops lining up must not pin a row forever.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            _write(path, [_asked(), _replied(request_id="unrelated")])
            self.assertEqual(pending_decisions(path), ())

    def test_reply_leaves_a_different_session_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            _write(
                path,
                [
                    _asked(),
                    _asked(session_id="ses_2", request_id="per_2", directory="/work/b"),
                    _replied(),
                ],
            )
            decisions = pending_decisions(path)

        self.assertEqual([item.session_id for item in decisions], ["ses_2"])

    def test_question_rejected_answers_a_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            _write(
                path,
                [
                    _asked(
                        event="question.asked",
                        kind="question",
                        request_id="qst_1",
                        category=None,
                    ),
                    _replied(event="question.rejected", request_id="qst_1"),
                ],
            )
            self.assertEqual(pending_decisions(path), ())

    def test_question_without_a_category_uses_a_default_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            _write(
                path,
                [
                    _asked(
                        event="question.asked",
                        kind="question",
                        request_id="qst_1",
                        category=None,
                    )
                ],
            )
            decisions = pending_decisions(path)

        self.assertEqual(decisions[0].reason, "question prompt")

    def test_stale_asks_expire_so_a_killed_session_never_sticks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            _write(path, [_asked(timestamp=time.time() - 48 * 3600)])
            self.assertEqual(pending_decisions(path), ())

    def test_marker_without_any_identifier_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            _write(path, [_asked(session_id=None, request_id=None)])
            self.assertEqual(pending_decisions(path), ())

    def test_corrupt_and_unrelated_lines_are_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            path.write_text(
                "{not json\n"
                + json.dumps({"event": "message.updated", "timestamp": time.time()})
                + "\n"
                + json.dumps(_asked())
                + "\n"
                + "half-a-record",
                encoding="utf-8",
            )
            decisions = pending_decisions(path)

        self.assertEqual(len(decisions), 1)

    def test_rotated_generation_is_read_oldest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            _write(path.with_name(path.name + ".1"), [_asked()])
            _write(path, [_replied()])
            self.assertEqual(pending_decisions(path), ())

    def test_health_reports_counts_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            _write(path, [_asked()])
            health = opencode_decision_log_health(path)

        self.assertTrue(health["exists"])
        self.assertEqual(health["record_count"], 1)
        self.assertEqual(health["pending_decisions"], 1)
        self.assertGreater(health["size_bytes"], 0)

    def test_health_on_a_missing_log_reports_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            health = opencode_decision_log_health(Path(tmp) / "decisions.jsonl")
        self.assertFalse(health["exists"])
        self.assertEqual(health["pending_decisions"], 0)


class FindPendingDecisionTests(unittest.TestCase):
    def test_session_id_match_is_preferred_over_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            _write(
                path,
                [
                    _asked(session_id="ses_1", request_id="per_1", category="edit"),
                    _asked(session_id="ses_2", request_id="per_2", category="bash"),
                ],
            )
            decisions = pending_decisions(path)

        found = find_pending_decision(
            decisions,
            session_id="ses_1",
            directory="/work/a",
        )
        assert found is not None
        self.assertEqual(found.session_id, "ses_1")

    def test_directory_match_falls_back_only_for_the_same_process(self) -> None:
        # A decision recorded by the exact same OpenCode process is accepted
        # when its session id is absent (plugin could not read one), because
        # directory + pid is then the only binding the plugin had.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            _write(path, [_asked(session_id=None)])
            decisions = pending_decisions(path)

        found = find_pending_decision(
            decisions,
            session_id="ses_unknown",
            directory="/work/a",
            pid=5000,
        )
        assert found is not None
        self.assertEqual(found.request_id, "per_1")

    def test_different_session_in_same_directory_is_not_inherited(self) -> None:
        # A fresh OpenCode process in the same directory must never be pinned to
        # `待确认` by another session's unanswered prompt.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            _write(path, [_asked(session_id="ses_other", pid=5000)])
            decisions = pending_decisions(path)

        self.assertIsNone(
            find_pending_decision(
                decisions,
                session_id="ses_unknown",
                directory="/work/a",
                pid=6000,
            )
        )

    def test_unrelated_directory_does_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            _write(path, [_asked()])
            decisions = pending_decisions(path)

        self.assertIsNone(
            find_pending_decision(
                decisions,
                session_id="ses_unknown",
                directory="/work/elsewhere",
            )
        )

    def test_no_directory_and_no_session_never_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            _write(path, [_asked()])
            decisions = pending_decisions(path)

        self.assertIsNone(
            find_pending_decision(decisions, session_id=None, directory=None)
        )


class DecisionTailTests(unittest.TestCase):
    def test_only_a_bounded_tail_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            filler = (
                json.dumps(
                    _replied(
                        request_id="old",
                        session_id="old",
                        timestamp=time.time() - 600.0,
                    )
                )
                + "\n"
            )
            with path.open("w", encoding="utf-8") as handle:
                while handle.tell() < 3 * 1024 * 1024:
                    handle.write(filler)
                handle.write(json.dumps(_asked()) + "\n")

            records = load_decision_records(path)
            decisions = pending_decisions(path)

        # A multi-megabyte history must neither be read whole nor lose the
        # newest marker, which is the only one that decides the row.
        self.assertLessEqual(len(records), 2000)
        self.assertEqual(records[-1]["event"], "permission.asked")
        self.assertEqual(len(decisions), 1)

    def test_env_override_is_used_when_no_path_is_given(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            _write(path, [_asked()])
            with patch.dict(os.environ, {DECISION_LOG_ENV: str(path)}):
                self.assertEqual(len(pending_decisions()), 1)


if __name__ == "__main__":
    unittest.main()
