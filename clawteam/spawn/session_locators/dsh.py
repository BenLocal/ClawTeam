"""DeepSeek Harness (dsh) session locator."""

from __future__ import annotations

import os
import re
from pathlib import Path

from clawteam.spawn.adapters import dsh_profile, is_dsh_command
from clawteam.spawn.command_validation import normalize_spawn_command

from .base import CapturedSession, PreparedSession, SessionContext, option_value

_SAFE_CHAR = re.compile(r"[A-Za-z0-9._-]")
_ESCAPE = re.compile(r"~([0-9A-F]{4})")


class DshSessionLocator:
    client = "dsh"

    def matches(self, command: list[str]) -> bool:
        return is_dsh_command(normalize_spawn_command(command))

    def prepare(self, command: list[str], context: SessionContext) -> PreparedSession:
        session_id = option_value(normalize_spawn_command(command), _resume_option(command))
        return PreparedSession(
            command=list(command),
            client=self.client,
            session_id=session_id,
            source="provided" if session_id else "",
            confidence="exact" if session_id else "",
            cwd=context.cwd,
            started_at=context.started_at,
            async_capture=not bool(session_id),
            hint=context.hint,
        )

    def capture(self, prepared: PreparedSession, context: SessionContext) -> CapturedSession | None:
        if prepared.session_id:
            return CapturedSession(
                prepared.session_id, self.client, "provided", "exact", context.cwd
            )
        headless = dsh_profile(normalize_spawn_command(prepared.command)) == "headless"
        # A fresh spawn must not adopt an older session from the same workspace.
        return _latest_session(context, headless=headless, since=prepared.started_at)

    def current_session(self, context: SessionContext) -> CapturedSession | None:
        return _latest_session(context, headless=False, since=0.0)

    def resume_command(self, command: list[str], session_id: str) -> list[str]:
        normalized = normalize_spawn_command(command)
        option = _resume_option(normalized)
        if option_value(normalized, option):
            return list(command)
        return [*command, option, session_id]


def _resume_option(command: list[str]) -> str:
    """headless adopts a session by id; the TUI resumes one."""
    return (
        "--session-id"
        if dsh_profile(normalize_spawn_command(command)) == "headless"
        else "--resume"
    )


def _latest_session(
    context: SessionContext, *, headless: bool, since: float
) -> CapturedSession | None:
    if not context.cwd:
        return None
    launch_dir = Path(context.cwd).expanduser().resolve()
    session_cwd = launch_dir if headless else _git_worktree_root(launch_dir) or launch_dir
    project_dir = _dsh_home() / "sessions" / _project_key(str(session_cwd))
    if not project_dir.is_dir():
        return None
    candidates = []
    for session_dir in project_dir.iterdir():
        if not session_dir.is_dir():
            continue
        updated = _last_modified(session_dir)
        if updated >= since:
            candidates.append((updated, session_dir.name))
    if not candidates:
        return None
    _, name = max(candidates)
    session_id = _ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), name)
    return CapturedSession(session_id, "dsh", "session-store", "latest", context.cwd)


def _dsh_home() -> Path:
    return Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh").expanduser()


def _project_key(cwd: str) -> str:
    """Mirror dsh-session-persistence-jsonl's readable project directory key."""
    readable = ""
    separator_run = False
    for ch in cwd:
        if ch in "/\\:":
            if not separator_run:
                readable += "-"
            separator_run = True
            continue
        separator_run = False
        if ch != "~" and _SAFE_CHAR.fullmatch(ch):
            readable += ch
        else:
            readable += "".join(f"~{unit:04X}" for unit in _utf16_units(ch))
    return f"--{(readable.lstrip('-') or 'root')[:251]}--"


def _utf16_units(ch: str) -> list[int]:
    encoded = ch.encode("utf-16-be")
    return [int.from_bytes(encoded[i : i + 2], "big") for i in range(0, len(encoded), 2)]


def _git_worktree_root(start: Path) -> Path | None:
    """Mirror dsh-tui's session cwd: the nearest git worktree root below $HOME."""
    home = Path.home().resolve()
    for directory in (start, *start.parents):
        if directory == home:
            return None
        if (directory / ".git").exists():
            return directory
    return None


def _last_modified(session_dir: Path) -> float:
    try:
        return max(
            [
                session_dir.stat().st_mtime,
                *(entry.stat().st_mtime for entry in session_dir.iterdir()),
            ]
        )
    except OSError:
        return 0.0
