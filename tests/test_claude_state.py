from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_cli_monitor.claude_state import (
    DEFAULT_WAITING_REASON,
    STATUS_FAILURE,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_WAITING,
    TRANSCRIPT_HEAD_BYTES,
    TRANSCRIPT_TAIL_BYTES,
    claude_session_state,
    claude_state_health,
    default_claude_home,
    encode_project_dir,
    read_session_registration,
    read_transcript_outcome,
    registration_matches_process,
    reset_caches,
    resolve_transcript_path,
    transcript_proves_work_submitted,
)
from codex_cli_monitor.models import MAX_WAITING_REASON_LENGTH, ProcessInfo


SESSION_ID = "c578535a-e73e-4f74-86dd-af2273c5375b"
CWD = "/work/project a"


def _process(
    pid: int = 4321,
    start_ticks: int | None = 987654,
    started_at: float | None = 1_700_000_000.0,
    cwd: str | None = CWD,
) -> ProcessInfo:
    return ProcessInfo(
        pid=pid,
        ppid=1,
        comm="claude",
        state="S",
        cmdline=("claude",),
        cwd=cwd,
        exe="/usr/bin/claude",
        tty="/dev/pts/3",
        tty_nr=34816,
        elapsed_seconds=120.0,
        cpu_seconds=1.0,
        started_at=started_at,
        start_ticks=start_ticks,
    )


def _registration(**overrides: object) -> dict:
    data = {
        "pid": 4321,
        "procStart": 987654,
        "sessionId": SESSION_ID,
        "cwd": CWD,
        "kind": "interactive",
        "entrypoint": "cli",
        "version": "2.0.0",
        "status": "idle",
        "startedAt": 1_700_000_000_000,
        "updatedAt": 1_700_000_050_000,
        "statusUpdatedAt": 1_700_000_050_000,
    }
    data.update(overrides)
    return data


def _write_registration(home: Path, data: dict, pid: int = 4321) -> None:
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{pid}.json").write_text(json.dumps(data), encoding="utf-8")


def _write_transcript(
    home: Path,
    records: list[dict],
    cwd: str = CWD,
    session_id: str = SESSION_ID,
) -> Path:
    directory = home / "projects" / encode_project_dir(cwd)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _assistant(**overrides: object) -> dict:
    record = {"type": "assistant", "timestamp": "2026-08-25T02:00:00.000Z"}
    record.update(overrides)
    return record


def _prompt(**overrides: object) -> dict:
    record = {
        "type": "user",
        "timestamp": "2026-08-25T02:00:10.000Z",
        "origin": {"kind": "human"},
        "promptSource": "typed",
    }
    record.update(overrides)
    # ``origin=None`` means "an injected record that carries no origin marker",
    # which is how Claude Code writes its own context records.
    return {key: value for key, value in record.items() if value is not None}


def _tool_result(**overrides: object) -> dict:
    record = {"type": "user", "timestamp": "2026-08-25T02:00:05.000Z"}
    record.update(overrides)
    return record


class ClaudeHomeTests(unittest.TestCase):
    def test_config_dir_env_overrides_home(self) -> None:
        home = default_claude_home({"CLAUDE_CONFIG_DIR": "/custom/claude"})
        self.assertEqual(home, Path("/custom/claude"))

    def test_default_home_is_dot_claude(self) -> None:
        home = default_claude_home({})
        self.assertEqual(home, Path.home() / ".claude")

    def test_project_dir_encoding_matches_claude_code(self) -> None:
        self.assertEqual(
            encode_project_dir("/work/project a.b"),
            "-work-project-a-b",
        )


class RegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_caches()

    def test_missing_registration_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(read_session_registration(4321, Path(tmp)))

    def test_invalid_json_registration_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "sessions").mkdir()
            (home / "sessions" / "4321.json").write_text("{not json", encoding="utf-8")
            self.assertIsNone(read_session_registration(4321, home))

    def test_oversized_registration_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "sessions").mkdir()
            (home / "sessions" / "4321.json").write_text(
                json.dumps({"pad": "x" * (128 * 1024)}),
                encoding="utf-8",
            )
            self.assertIsNone(read_session_registration(4321, home))

    def test_matching_proc_start_binds_process(self) -> None:
        self.assertTrue(registration_matches_process(_registration(), _process()))

    def test_reused_pid_with_different_proc_start_is_rejected(self) -> None:
        self.assertFalse(
            registration_matches_process(
                _registration(procStart=111111),
                _process(),
            )
        )

    def test_started_at_is_used_when_proc_start_is_absent(self) -> None:
        data = _registration()
        data.pop("procStart")
        self.assertTrue(registration_matches_process(data, _process(start_ticks=None)))
        self.assertFalse(
            registration_matches_process(
                data,
                _process(start_ticks=None, started_at=1_700_009_999.0),
            )
        )

    def test_registration_without_temporal_proof_is_rejected(self) -> None:
        data = _registration()
        data.pop("procStart")
        data.pop("startedAt")
        self.assertFalse(registration_matches_process(data, _process(start_ticks=None)))

    def test_pid_mismatch_is_rejected(self) -> None:
        self.assertFalse(
            registration_matches_process(_registration(pid=999), _process())
        )


class TranscriptOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_caches()

    def test_missing_transcript_has_no_outcome(self) -> None:
        outcome = read_transcript_outcome(None)
        self.assertFalse(outcome.assistant_seen)
        self.assertFalse(outcome.terminal_event)
        self.assertFalse(outcome.failed_event)

    def test_completed_turn_is_successful(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(Path(tmp), [_prompt(), _assistant()])
            outcome = read_transcript_outcome(path)
        self.assertTrue(outcome.assistant_seen)
        self.assertTrue(outcome.terminal_event)
        self.assertFalse(outcome.failed_event)

    def test_api_error_marks_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(
                Path(tmp),
                [_prompt(), _assistant(isApiErrorMessage=True)],
            )
            outcome = read_transcript_outcome(path)
        self.assertTrue(outcome.failed_event)

    def test_mid_stream_abort_marks_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(
                Path(tmp),
                [_prompt(), _assistant(isAbortedMidStream=True)],
            )
            outcome = read_transcript_outcome(path)
        self.assertTrue(outcome.failed_event)

    def test_prompt_interrupted_before_any_assistant_record_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(
                Path(tmp),
                [_prompt(), _assistant(), _prompt(), {"type": "attachment"}],
            )
            outcome = read_transcript_outcome(path)
        self.assertTrue(outcome.assistant_seen)
        self.assertTrue(outcome.failed_event)

    def test_first_turn_interrupted_before_any_assistant_record_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(Path(tmp), [_prompt()])
            outcome = read_transcript_outcome(path)
        self.assertTrue(outcome.assistant_seen)
        self.assertTrue(outcome.failed_event)

    def test_tool_result_records_do_not_open_a_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(
                Path(tmp),
                [_prompt(), _assistant(), _tool_result()],
            )
            outcome = read_transcript_outcome(path)
        self.assertFalse(outcome.failed_event)

    def test_meta_prompt_records_do_not_open_a_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(
                Path(tmp),
                [_prompt(), _assistant(), _prompt(isMeta=True)],
            )
            outcome = read_transcript_outcome(path)
        self.assertFalse(outcome.failed_event)

    def test_sidechain_records_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(
                Path(tmp),
                [
                    _prompt(),
                    _assistant(),
                    _prompt(isSidechain=True),
                    _assistant(isSidechain=True, isApiErrorMessage=True),
                ],
            )
            outcome = read_transcript_outcome(path)
        self.assertTrue(outcome.assistant_seen)
        self.assertFalse(outcome.failed_event)

    def test_corrupt_lines_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(Path(tmp), [_prompt(), _assistant()])
            with path.open("a", encoding="utf-8") as handle:
                handle.write("{truncated\n\n")
            outcome = read_transcript_outcome(path)
        self.assertTrue(outcome.assistant_seen)
        self.assertFalse(outcome.failed_event)

    def test_only_a_bounded_tail_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _write_transcript(home, [_assistant(isApiErrorMessage=True)])
            filler = json.dumps({"type": "attachment", "pad": "x" * 4096}) + "\n"
            with path.open("a", encoding="utf-8") as handle:
                written = 0
                while written < TRANSCRIPT_TAIL_BYTES * 2:
                    handle.write(filler)
                    written += len(filler)
            outcome = read_transcript_outcome(path)
        # The failing assistant record now sits outside the tail window, so no
        # terminal event is visible and the row is not classified from it.
        self.assertFalse(outcome.assistant_seen)

    def test_outcome_is_cached_until_the_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(Path(tmp), [_prompt(), _assistant()])
            first = read_transcript_outcome(path)
            self.assertIs(read_transcript_outcome(path), first)


class WorkSubmittedTests(unittest.TestCase):
    """Display eligibility: proof that a session actually started working."""

    def setUp(self) -> None:
        reset_caches()

    def test_absent_transcript_proves_nothing(self) -> None:
        self.assertFalse(transcript_proves_work_submitted(None))
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.jsonl"
            self.assertFalse(transcript_proves_work_submitted(missing))

    def test_startup_preamble_proves_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(
                Path(tmp),
                [
                    {"type": "mode", "mode": "normal"},
                    {"type": "permission-mode", "permissionMode": "default"},
                    {"type": "file-history-snapshot"},
                    _prompt(isMeta=True, origin=None),
                ],
            )
            self.assertFalse(transcript_proves_work_submitted(path))

    def test_submitted_prompt_proves_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(Path(tmp), [_prompt()])
            self.assertTrue(transcript_proves_work_submitted(path))

    def test_assistant_record_alone_proves_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(Path(tmp), [_assistant()])
            self.assertTrue(transcript_proves_work_submitted(path))

    def test_sidechain_records_prove_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(
                Path(tmp),
                [_prompt(isSidechain=True), _assistant(isSidechain=True)],
            )
            self.assertFalse(transcript_proves_work_submitted(path))

    def test_tool_result_user_record_proves_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(Path(tmp), [_tool_result()])
            self.assertFalse(transcript_proves_work_submitted(path))

    def test_first_submission_beyond_the_head_window_is_still_found(self) -> None:
        # Pathological but survivable: the head window is filled with startup
        # bookkeeping, so the bounded tail read has to supply the proof.
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(Path(tmp), [{"type": "mode"}])
            filler = json.dumps({"type": "attachment", "pad": "x" * 4096}) + "\n"
            with path.open("a", encoding="utf-8") as handle:
                written = 0
                while written < TRANSCRIPT_HEAD_BYTES:
                    handle.write(filler)
                    written += len(filler)
                handle.write(json.dumps(_prompt()) + "\n")
            self.assertTrue(transcript_proves_work_submitted(path))

    def test_eligibility_is_latched_after_the_transcript_disappears(self) -> None:
        # A session that has submitted work never becomes ineligible again, and
        # the latch keeps the open-turn path from re-reading on every scan.
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(Path(tmp), [_prompt()])
            self.assertTrue(transcript_proves_work_submitted(path))
            path.unlink()
            self.assertTrue(transcript_proves_work_submitted(path))
            reset_caches()
            self.assertFalse(transcript_proves_work_submitted(path))


class TranscriptPathTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_caches()

    def test_encoded_directory_is_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            expected = _write_transcript(home, [_assistant()])
            self.assertEqual(resolve_transcript_path(SESSION_ID, CWD, home), expected)

    def test_shortened_directory_is_found_by_bounded_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            expected = _write_transcript(home, [_assistant()], cwd="/other/place")
            self.assertEqual(resolve_transcript_path(SESSION_ID, CWD, home), expected)

    def test_unknown_session_resolves_to_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "projects").mkdir()
            self.assertIsNone(resolve_transcript_path(SESSION_ID, CWD, home))

    def test_cached_path_is_dropped_when_the_file_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _write_transcript(home, [_assistant()])
            self.assertEqual(resolve_transcript_path(SESSION_ID, CWD, home), path)
            path.unlink()
            self.assertIsNone(resolve_transcript_path(SESSION_ID, CWD, home))


class ClaudeSessionStateTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_caches()

    def test_process_without_registration_is_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(claude_session_state(_process(), Path(tmp)))

    def test_reused_pid_is_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(home, _registration(status="busy"))
            _write_transcript(home, [_prompt(), _assistant()])
            self.assertIsNone(
                claude_session_state(_process(start_ticks=222222), home)
            )

    def test_launch_dialog_without_any_submission_is_hidden(self) -> None:
        # Claude Code reports ``waiting`` for its own onboarding, trust, and
        # model dialogs, before the user has typed anything.  Such a session has
        # no transcript at all and must not appear as 待确认.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(
                home,
                _registration(status="waiting", waitingFor="dialog open"),
            )
            self.assertIsNone(claude_session_state(_process(), home))

    def test_busy_startup_without_any_submission_is_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(home, _registration(status="busy"))
            self.assertIsNone(claude_session_state(_process(), home))

    def test_startup_preamble_alone_does_not_make_a_session_displayable(
        self,
    ) -> None:
        # A transcript can exist and still prove nothing: a slash command such
        # as ``/model`` writes ``mode``/``permission-mode`` bookkeeping and
        # injected ``isMeta`` context records without opening a turn.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(
                home,
                _registration(status="waiting", waitingFor="dialog open"),
            )
            _write_transcript(
                home,
                [
                    {"type": "mode", "mode": "normal"},
                    {"type": "permission-mode", "permissionMode": "default"},
                    {"type": "file-history-snapshot"},
                    _prompt(isMeta=True, origin=None),
                    {"type": "last-prompt"},
                ],
            )
            self.assertIsNone(claude_session_state(_process(), home))

    def test_open_turn_displays_once_a_prompt_was_submitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(
                home,
                _registration(status="waiting", waitingFor="plan approval"),
            )
            _write_transcript(
                home,
                [
                    {"type": "mode", "mode": "normal"},
                    _prompt(isMeta=True, origin=None),
                    _prompt(),
                ],
            )
            state = claude_session_state(_process(), home)
        assert state is not None
        self.assertEqual(state.status, STATUS_WAITING)
        self.assertEqual(state.waiting_for, "plan approval")

    def test_open_turn_after_a_finished_turn_still_displays(self) -> None:
        # A second turn is open while the transcript's newest record is the
        # previous turn's assistant reply; the registration decides the status.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(home, _registration(status="busy"))
            _write_transcript(home, [_prompt(), _assistant()])
            state = claude_session_state(_process(), home)
        assert state is not None
        self.assertEqual(state.status, STATUS_RUNNING)
        self.assertTrue(state.turn_active)

    def test_sidechain_only_transcript_is_not_displayable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(home, _registration(status="busy"))
            _write_transcript(
                home,
                [
                    _prompt(isSidechain=True),
                    _assistant(isSidechain=True),
                ],
            )
            self.assertIsNone(claude_session_state(_process(), home))

    def test_fresh_session_without_any_turn_is_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(home, _registration(status="idle"))
            self.assertIsNone(claude_session_state(_process(), home))

    def test_busy_session_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(home, _registration(status="busy"))
            _write_transcript(home, [_prompt()])
            state = claude_session_state(_process(), home)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.status, STATUS_RUNNING)
        self.assertTrue(state.turn_active)
        self.assertEqual(state.session_id, SESSION_ID)
        self.assertEqual(state.cwd, CWD)

    def test_shell_status_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(home, _registration(status="shell"))
            _write_transcript(home, [_prompt()])
            state = claude_session_state(_process(), home)
        assert state is not None
        self.assertEqual(state.status, STATUS_RUNNING)
        self.assertTrue(state.turn_active)
        self.assertIsNone(state.waiting_for)

    def test_waiting_status_is_reported_as_a_pending_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(
                home,
                _registration(status="waiting", waitingFor="goal proposal"),
            )
            _write_transcript(home, [_prompt()])
            state = claude_session_state(_process(), home)
        assert state is not None
        # The turn is open, but it is blocked on the user rather than advancing.
        self.assertEqual(state.status, STATUS_WAITING)
        self.assertTrue(state.turn_active)
        self.assertEqual(state.waiting_for, "goal proposal")
        self.assertEqual(state.to_dict()["waiting_for"], "goal proposal")

    def test_waiting_status_without_a_label_uses_the_default_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(home, _registration(status="waiting"))
            _write_transcript(home, [_prompt()])
            state = claude_session_state(_process(), home)
        assert state is not None
        self.assertEqual(state.status, STATUS_WAITING)
        self.assertEqual(state.waiting_for, DEFAULT_WAITING_REASON)

    def test_waiting_label_is_clamped_and_stripped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(
                home,
                _registration(status="waiting", waitingFor="  a\x07" + "b" * 200),
            )
            _write_transcript(home, [_prompt()])
            state = claude_session_state(_process(), home)
        assert state is not None
        assert state.waiting_for is not None
        self.assertEqual(len(state.waiting_for), MAX_WAITING_REASON_LENGTH)
        self.assertNotIn("\x07", state.waiting_for)
        self.assertTrue(state.waiting_for.startswith("ab"))

    def test_health_counts_registrations_blocked_on_a_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(home, _registration(status="waiting"))
            _write_registration(
                home,
                _registration(pid=4322, status="busy"),
                pid=4322,
            )
            health = claude_state_health(home)
        self.assertEqual(health["registered_sessions"], 2)
        self.assertEqual(health["waiting_sessions"], 1)

    def test_unknown_status_falls_back_to_the_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(home, _registration(status="hibernating"))
            _write_transcript(home, [_prompt(), _assistant()])
            state = claude_session_state(_process(), home)
        assert state is not None
        self.assertEqual(state.status, STATUS_SUCCESS)

    def test_idle_session_after_a_completed_turn_is_successful(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(home, _registration(status="idle"))
            _write_transcript(home, [_prompt(), _assistant()])
            state = claude_session_state(_process(), home)
        assert state is not None
        self.assertEqual(state.status, STATUS_SUCCESS)
        self.assertFalse(state.turn_active)
        self.assertTrue(state.terminal_event)

    def test_idle_session_after_a_failed_turn_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(home, _registration(status="idle"))
            _write_transcript(
                home,
                [_prompt(), _assistant(isApiErrorMessage=True)],
            )
            state = claude_session_state(_process(), home)
        assert state is not None
        self.assertEqual(state.status, STATUS_FAILURE)
        self.assertTrue(state.failed_event)

    def test_a_later_successful_turn_clears_an_earlier_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(home, _registration(status="idle"))
            _write_transcript(
                home,
                [
                    _prompt(),
                    _assistant(isApiErrorMessage=True),
                    _prompt(),
                    _assistant(),
                ],
            )
            state = claude_session_state(_process(), home)
        assert state is not None
        self.assertEqual(state.status, STATUS_SUCCESS)

    def test_non_interactive_kinds_are_hidden(self) -> None:
        for kind in ("bg", "daemon", "daemon-worker"):
            with self.subTest(kind=kind):
                reset_caches()
                with tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp)
                    _write_registration(
                        home,
                        _registration(status="busy", kind=kind),
                    )
                    self.assertIsNone(claude_session_state(_process(), home))

    def test_registration_without_session_id_is_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            data = _registration(status="busy")
            data.pop("sessionId")
            _write_registration(home, data)
            self.assertIsNone(claude_session_state(_process(), home))

    def test_state_serializes_to_a_plain_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(home, _registration(status="busy"))
            _write_transcript(home, [_prompt()])
            state = claude_session_state(_process(), home)
        assert state is not None
        payload = state.to_dict()
        self.assertEqual(payload["pid"], 4321)
        self.assertEqual(payload["status"], STATUS_RUNNING)
        self.assertEqual(payload["registered_status"], "busy")
        self.assertEqual(payload["kind"], "interactive")
        self.assertEqual(payload["version"], "2.0.0")


class ClaudeHealthTests(unittest.TestCase):
    def test_health_counts_registrations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_registration(home, _registration(), pid=4321)
            _write_registration(home, _registration(pid=99), pid=99)
            (home / "sessions" / "notes.txt").write_text("x", encoding="utf-8")
            (home / "projects").mkdir()
            health = claude_state_health(home)
        self.assertEqual(health["registered_sessions"], 2)
        self.assertTrue(health["home_exists"])
        self.assertTrue(health["sessions_dir_exists"])
        self.assertTrue(health["projects_dir_exists"])

    def test_health_is_safe_without_claude_code_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            health = claude_state_health(Path(tmp) / "absent")
        self.assertEqual(health["registered_sessions"], 0)
        self.assertFalse(health["home_exists"])
        self.assertFalse(health["sessions_dir_exists"])
        self.assertFalse(health["projects_dir_exists"])


if __name__ == "__main__":
    unittest.main()
