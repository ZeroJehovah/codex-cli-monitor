from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from .models import CodexStateSummary, StateFile


DEFAULT_MAX_FILES = 12
STATE_PATTERNS = (
    "sessions/**/*.jsonl",
    "shell_snapshots/*.sh",
    "history.jsonl",
    "*.sqlite",
    "*.sqlite-wal",
    "*.sqlite-shm",
)


def default_codex_home(env: Mapping[str, str] | None = None) -> Path:
    env = env or os.environ
    if env.get("CODEX_HOME"):
        return Path(env["CODEX_HOME"]).expanduser()
    return Path.home() / ".codex"


def scan_codex_state(
    codex_home: Path | None = None,
    max_files: int = DEFAULT_MAX_FILES,
) -> CodexStateSummary:
    home = (codex_home or default_codex_home()).expanduser()
    newest: list[StateFile] = []
    errors: list[str] = []

    if not home.exists():
        return CodexStateSummary(
            codex_home=str(home),
            newest_files=(),
            scan_errors=(f"{home} does not exist",),
        )
    if not home.is_dir():
        return CodexStateSummary(
            codex_home=str(home),
            newest_files=(),
            scan_errors=(f"{home} is not a directory",),
        )

    seen: set[Path] = set()
    for pattern in STATE_PATTERNS:
        try:
            paths = tuple(home.glob(pattern))
        except OSError as error:
            errors.append(f"{pattern}: {error}")
            continue
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            state_file = _state_file(home, path)
            if state_file is not None:
                newest.append(state_file)

    newest.sort(key=lambda item: item.modified_at, reverse=True)
    return CodexStateSummary(
        codex_home=str(home),
        newest_files=tuple(newest[:max_files]),
        scan_errors=tuple(errors),
    )


def _state_file(home: Path, path: Path) -> StateFile | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    try:
        relative_path = path.relative_to(home)
    except ValueError:
        relative_path = path
    return StateFile(
        relative_path=relative_path.as_posix(),
        size_bytes=stat.st_size,
        modified_at=stat.st_mtime,
        kind=_classify_state_path(relative_path),
    )


def _classify_state_path(relative_path: Path) -> str:
    parts = relative_path.parts
    name = relative_path.name
    if parts and parts[0] == "sessions" and name.endswith(".jsonl"):
        return "session_jsonl"
    if parts and parts[0] == "shell_snapshots" and name.endswith(".sh"):
        return "shell_snapshot"
    if name == "history.jsonl":
        return "history"
    if name.endswith(".sqlite-wal"):
        return "sqlite_wal"
    if name.endswith(".sqlite-shm"):
        return "sqlite_shm"
    if name.endswith(".sqlite"):
        return "sqlite"
    return "other"
