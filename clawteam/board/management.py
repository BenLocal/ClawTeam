"""Validated board operations backed by the same stores and launcher as the CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    model_validator,
)

from clawteam.paths import validate_identifier
from clawteam.team.mailbox import MailboxManager
from clawteam.team.manager import TeamManager
from clawteam.team.models import TaskPriority, TaskStatus, get_data_dir
from clawteam.team.tasks import TaskStore

Identifier = Annotated[StrictStr, AfterValidator(validate_identifier)]
Nonempty = Annotated[
    StrictStr, Field(min_length=1), AfterValidator(lambda s: s if s.strip() else _blank())
]
_SPAWN_LOCK = threading.Lock()
SPAWN_TIMEOUT = 60


def _blank() -> str:
    raise ValueError("Value must not be blank")


class APIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTask(RequestModel):
    subject: Nonempty
    description: StrictStr = ""
    owner: StrictStr = ""
    priority: TaskPriority = TaskPriority.medium
    blocks: list[Identifier] = Field(default_factory=list)
    blocked_by: list[Identifier] = Field(default_factory=list)


class UpdateTask(RequestModel):
    subject: Nonempty | None = None
    description: StrictStr | None = None
    owner: StrictStr | None = None
    status: TaskStatus | None = None
    priority: TaskPriority | None = None
    add_blocks: list[Identifier] = Field(default_factory=list)
    add_blocked_by: list[Identifier] = Field(default_factory=list)
    caller: Identifier = "board"
    force: StrictBool = False

    @model_validator(mode="after")
    def has_changes(self):
        changes = self.model_dump(exclude_none=True, exclude={"caller", "force"})
        if not any(value != [] for value in changes.values()):
            raise ValueError("At least one task change is required")
        return self


class SendMessage(RequestModel):
    to: Identifier
    content: Nonempty
    sender: Identifier = Field(default="board", alias="from")
    key: StrictStr | None = None


class BroadcastMessage(RequestModel):
    content: Nonempty
    sender: Identifier = Field(default="board", alias="from")
    key: StrictStr | None = None


class SpawnAgent(RequestModel):
    agent_name: Identifier
    backend: Literal["tmux", "subprocess", "wsh"] = "tmux"
    profile: Identifier | None = None
    command: list[Nonempty] = Field(default_factory=list)
    repo: Nonempty
    agent_type: Nonempty = "general-purpose"
    task: StrictStr = ""
    workspace: StrictBool = True
    skip_permissions: StrictBool = False
    resume: StrictBool = False
    replace: StrictBool = False
    keepalive: StrictBool = True

    @model_validator(mode="after")
    def check_launch(self):
        if not self.profile and not self.command:
            raise ValueError("A profile or command array is required")
        if not Path(self.repo).is_absolute() or not Path(self.repo).is_dir():
            raise ValueError("repo must be an existing absolute directory on the server")
        if self.profile:
            from clawteam.spawn.profiles import load_profile

            load_profile(self.profile)
        if self.workspace:
            from clawteam.workspace import get_workspace_manager

            if get_workspace_manager(self.repo) is None:
                raise ValueError(
                    "workspace=true requires a Git repository; use workspace=false otherwise"
                )
        for value in [self.repo, self.task, self.agent_type, *self.command]:
            if "\0" in value:
                raise ValueError("Launch parameters must not contain NUL")
        return self


def require_team(team: str) -> None:
    validate_identifier(team, "team name")
    if TeamManager.get_team(team) is None:
        raise APIError(404, "Team not found")


def create_task(team: str, payload: dict) -> dict:
    request = CreateTask.model_validate(payload)
    task = TaskStore(team).create(**request.model_dump())
    # Preserve the existing board response for older clients.
    return {"status": "ok", "task_id": task.id, "task": task.model_dump(mode="json", by_alias=True)}


def update_task(team: str, task_id: str, payload: dict) -> dict:
    validate_identifier(task_id, "task ID")
    request = UpdateTask.model_validate(payload)
    task = TaskStore(team).update(task_id, **request.model_dump(exclude_none=True))
    if task is None:
        raise APIError(404, "Task not found")
    return {"status": "ok", "task": task.model_dump(mode="json", by_alias=True)}


def send_message(team: str, payload: dict, *, broadcast: bool = False) -> dict:
    if broadcast:
        request = BroadcastMessage.model_validate(payload)
        messages = MailboxManager(team).broadcast(
            from_agent=request.sender,
            content=request.content,
            key=request.key,
        )
        return {"status": "ok", "count": len(messages), "recipients": [m.to for m in messages]}
    request = SendMessage.model_validate(payload)
    inbox = TeamManager.resolve_inbox(team, request.to)
    if inbox not in {TeamManager.inbox_name_for(m) for m in TeamManager.list_members(team)}:
        raise APIError(404, "Recipient is not a team member")
    message = MailboxManager(team).send(
        from_agent=request.sender,
        to=request.to,
        content=request.content,
        key=request.key,
    )
    return {
        "status": "ok",
        "message": message.model_dump(mode="json", by_alias=True, exclude_none=True),
    }


def spawn_agent(team: str, payload: dict) -> dict:
    request = SpawnAgent.model_validate(payload)
    argv = [
        sys.executable,
        "-m",
        "clawteam",
        "--json",
        "--data-dir",
        str(get_data_dir()),
        "spawn",
        f"--team={team}",
        f"--agent-name={request.agent_name}",
        f"--repo={request.repo}",
        f"--agent-type={request.agent_type}",
        f"--task={request.task}",
    ]
    if request.profile:
        argv += [f"--profile={request.profile}"]
    for flag in ("workspace", "skip_permissions", "keepalive"):
        name = flag.replace("_", "-")
        argv.append("--" + ("" if getattr(request, flag) else "no-") + name)
    for flag in ("resume", "replace"):
        if getattr(request, flag):
            argv.append("--" + flag)
    argv += ["--", request.backend, *request.command]
    # Isolate Typer's process-global state, stdout, and launcher environment from
    # concurrent HTTP requests. No remote shell or shell=True is involved.
    if not _SPAWN_LOCK.acquire(blocking=False):
        raise APIError(409, "Another agent launch is in progress; query state before retrying")
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=SPAWN_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise APIError(
            504, "Launch timed out; the agent may have started. Query team state before retrying"
        ) from exc
    finally:
        _SPAWN_LOCK.release()
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise APIError(
            502, "Launcher did not return JSON; check server logs and team state"
        ) from exc
    if not isinstance(data, dict):
        raise APIError(502, "Invalid launcher response")
    if result.returncode or data.get("error"):
        raise APIError(409, str(data.get("error") or "Agent launch failed; check server state"))
    if data.get("status") != "spawned":
        raise APIError(502, "Launcher did not confirm agent startup")
    return data
