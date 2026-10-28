"""Exercise management requests over HTTP using isolated real stores and launchers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from clawteam.board import management
from clawteam.board.collector import BoardCollector
from clawteam.board.server import BoardHandler
from clawteam.spawn.registry import stop_agent
from clawteam.team.mailbox import MailboxManager
from clawteam.team.manager import TeamManager
from clawteam.team.models import TaskStatus
from clawteam.team.tasks import TaskStore


@pytest.fixture
def api(monkeypatch):
    monkeypatch.delenv("CLAWTEAM_BOARD_API_KEY", raising=False)
    TeamManager.create_team("demo", "leader", "leader-id")
    TeamManager.add_member("demo", "worker", "worker-id")

    class Handler(BoardHandler):
        collector = BoardCollector()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    opener = build_opener(ProxyHandler({}))

    def call(path, payload=None, method=None, headers=None, raw=None):
        body = (
            raw
            if raw is not None
            else json.dumps(payload).encode()
            if payload is not None
            else None
        )
        request = Request(
            origin + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            response = opener.open(request, timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            data = response.read()
            return response.status, json.loads(data), response.headers

    call.base_url = origin + "/api"
    yield call
    server.shutdown()
    server.server_close()
    thread.join()


def test_task_options_update_and_dependency_resolution(api):
    status, first, _ = api("/api/team/demo/task", {"subject": "准备", "priority": "high"})
    assert status == 200
    assert first["status"] == "ok"
    assert first["task"]["priority"] == "high"
    _, second, _ = api("/api/team/demo/task", {"subject": "构建", "blocked_by": [first["task_id"]]})
    assert second["task"]["status"] == "blocked"
    status, result, _ = api(
        f"/api/team/demo/task/{first['task_id']}",
        {
            "status": "completed",
            "owner": "worker",
            "description": "第一行\n`literal` $HOME",
            "priority": "urgent",
        },
        method="PATCH",
    )
    assert status == 200
    assert result["task"]["owner"] == "worker"
    assert result["task"]["description"] == "第一行\n`literal` $HOME"
    assert TaskStore("demo").get(second["task_id"]).status == TaskStatus.pending
    # POST is also supported for clients/proxies that do not permit PATCH.
    status, _, _ = api(f"/api/team/demo/task/{first['task_id']}", {"subject": "重命名"})
    assert status == 200


def test_lock_conflicts_and_missing_tasks(api):
    task = TaskStore("demo").create("locked")
    TaskStore("demo").update(task.id, status=TaskStatus.in_progress, caller="worker")
    status, _, _ = api(
        f"/api/team/demo/task/{task.id}", {"status": "in_progress", "caller": "board"}
    )
    assert status == 409
    assert api("/api/team/demo/task/missing", {"owner": "worker"})[0] == 404


def test_messages_preserve_content_and_broadcast_to_members(api):
    content = "你好\n$(echo literal) `literal` 'quoted'"
    status, message, headers = api("/api/team/demo/messages", {"to": "worker", "content": content})
    assert status == 200
    assert message["message"]["content"] == content
    assert "Access-Control-Allow-Origin" not in headers
    assert MailboxManager("demo").peek("worker")[0].content == content
    status, result, _ = api("/api/team/demo/broadcast", {"from": "leader", "content": "通知"})
    assert status == 200
    assert result["recipients"] == ["worker"]
    assert api("/api/team/demo/messages", {"to": "unknown", "content": "x"})[0] == 404


@pytest.mark.parametrize(
    "path,body",
    [
        ("task", {"subject": "x", "priority": "invalid"}),
        ("task", {"subject": " "}),
        ("task", {"subject": "x", "blocked_by": ["../escape"]}),
        ("task", {"subject": "x", "unexpected": True}),
        ("task/id", {}),
        ("task/id", {"force": "false", "status": "completed"}),
        ("messages", {"to": "../escape", "content": "x"}),
        ("messages", {"to": "worker", "content": 42}),
        ("agents/spawn", {"agent_name": "bad", "repo": "/tmp"}),
    ],
)
def test_invalid_requests_do_not_write(api, path, body):
    before = TaskStore("demo").list_tasks()
    assert api("/api/team/demo/" + path, body)[0] == 400
    assert TaskStore("demo").list_tasks() == before
    assert MailboxManager("demo").peek_count("worker") == 0


def test_body_origin_and_path_boundaries(api, isolated_data_dir):
    assert api("/api/team/demo/task", raw=b"{")[0] == 400
    assert api("/api/team/demo/task", ["not an object"])[0] == 400
    assert (
        api("/api/team/demo/task", {"subject": "x"}, headers={"Origin": "https://evil.test"})[0]
        == 403
    )
    assert (
        api("/api/team/demo/task", {"subject": "x"}, headers={"Content-Type": "text/plain"})[0]
        == 415
    )
    assert api("/api/team/missing/task", {"subject": "x"})[0] == 404
    assert not (isolated_data_dir / "tasks" / "missing").exists()
    assert api("/api/team/demo/task/..%2Fescape", {"owner": "worker"})[0] == 400
    assert api("/api/team/demo/task", raw=b" " * (1024 * 1024 + 1))[0] == 413


def test_spawn_real_local_process_and_query_agents(api, tmp_path):
    marker = tmp_path / "spawn-result.json"
    code = "import json,os,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps({'team':os.environ['CLAWTEAM_TEAM_NAME'],'args':sys.argv[2:]}))"
    literal = "'中文' $(touch should-not-exist) `literal`\nsecond line"
    status, result, _ = api(
        "/api/team/demo/agents/spawn",
        {
            "agent_name": "smoke",
            "backend": "subprocess",
            "repo": str(tmp_path),
            "command": [sys.executable, "-c", code, str(marker), literal],
            "workspace": False,
            "keepalive": False,
        },
    )
    try:
        assert status == 200, result
        assert result["status"] == "spawned"
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.02)
        actual = json.loads(marker.read_text())
        assert actual == {"team": "demo", "args": [literal]}
        status, agents, _ = api("/api/team/demo/agents")
        assert status == 200
        assert any(a["name"] == "smoke" and a["backend"] == "subprocess" for a in agents["agents"])
    finally:
        stop_agent("demo", "smoke")


def test_spawn_timeout_is_not_retried(api, tmp_path, monkeypatch):
    calls = []

    def timeout(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 60)

    monkeypatch.setattr(management.subprocess, "run", timeout)
    status, data, _ = api(
        "/api/team/demo/agents/spawn",
        {
            "agent_name": "slow",
            "workspace": False,
            "repo": str(tmp_path),
            "command": ["fake"],
        },
    )
    assert status == 504
    assert "may have started" in data["error"]
    assert len(calls) == 1


def test_capabilities_advertise_management_operations(api):
    status, result, _ = api("/api/capabilities")
    assert status == 200
    assert {"task_update", "message_send", "message_broadcast", "agent_spawn", "agent_list"} <= set(
        result["features"]
    )


@pytest.mark.skipif(
    not os.environ.get("CLAWTEAM_HTTP_CLIENT"),
    reason="Set CLAWTEAM_HTTP_CLIENT for cross-repository client integration",
)
def test_skill_client_against_real_http_api(api, tmp_path, monkeypatch):
    client = os.environ["CLAWTEAM_HTTP_CLIENT"]
    monkeypatch.setenv("CLAWTEAM_BOARD_API_KEY", "test-secret")
    env = {**os.environ, "CLAWTEAM_API_KEY_FILE": ""}

    def run(*args):
        result = subprocess.run(
            [sys.executable, client, "--api-url", api.base_url, "--team", "demo", *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    created = run("task", "create", "HTTP integration", "--priority", "high")
    updated = run("task", "update", created["task_id"], "--status", "completed")
    assert updated["task"]["status"] == "completed"
    message = run("message", "send", "worker", "--content", "HTTP 中文\n$HOME")
    assert message["message"]["content"] == "HTTP 中文\n$HOME"
    broadcast = run("message", "broadcast", "--from", "leader", "--content", "通知")
    assert broadcast["count"] == 1
    assert len(run("agents")["agents"]) == 2
    completed = run("team")["tasks"]["completed"]
    assert [task["id"] for task in completed] == [created["task_id"]]
    assert any(team["name"] == "demo" for team in run("overview"))


@pytest.mark.parametrize(
    "extra", [{"workspace": True}, {"workspace": False, "profile": "missing-profile"}]
)
def test_spawn_invalid_environment_has_no_effects(api, tmp_path, extra):
    before = TeamManager.list_members("demo")
    status, data, _ = api(
        "/api/team/demo/agents/spawn",
        {
            "agent_name": "bad",
            "repo": str(tmp_path),
            "command": [sys.executable],
            **extra,
        },
    )
    assert status == 400, data
    assert TeamManager.list_members("demo") == before


def test_spawn_workspace_keeps_json_response_clean(api, tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    marker = tmp_path / "cwd.txt"
    code = "import os,sys; from pathlib import Path; Path(sys.argv[1]).write_text(os.getcwd())"
    status, data, _ = api(
        "/api/team/demo/agents/spawn",
        {
            "agent_name": "isolated",
            "backend": "subprocess",
            "repo": str(repo),
            "command": [sys.executable, "-c", code, str(marker)],
            "keepalive": False,
        },
    )
    try:
        assert status == 200, data
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.02)
        assert marker.read_text().endswith("/workspaces/demo/isolated")
    finally:
        stop_agent("demo", "isolated")


@pytest.mark.parametrize(
    "path,payload,method",
    [
        ("/openapi/capabilities", None, "GET"),
        ("/openapi/overview", None, "GET"),
        ("/openapi/team/demo/agents", None, "GET"),
        ("/openapi/team/demo/task", {"subject": "protected"}, "POST"),
        ("/openapi/team/demo/task/missing", {"status": "completed"}, "PATCH"),
        ("/openapi/team/demo/prompt", {"prompt": "blocked"}, "POST"),
        ("/openapi/terminal/ticket", {}, "POST"),
        ("/openapi/terminal/ws", None, "GET"),
    ],
)
def test_api_key_rejects_unauthorized_requests(api, monkeypatch, path, payload, method):
    monkeypatch.setenv("CLAWTEAM_BOARD_API_KEY", "test-secret")
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        status, data, response_headers = api(path, payload, method=method, headers=headers)
        assert status == 401
        assert data == {"error": "Invalid or missing API key"}
        assert "Access-Control-Allow-Origin" not in response_headers
    assert TaskStore("demo").list_tasks() == []


def test_api_key_accepts_bearer_and_rejects_query_key(api, monkeypatch):
    monkeypatch.setenv("CLAWTEAM_BOARD_API_KEY", "test-secret")
    headers = {"Authorization": "Bearer test-secret"}
    assert api("/openapi/capabilities", headers=headers)[0] == 200
    status, data, _ = api("/openapi/team/demo/task", {"subject": "authorized"}, headers=headers)
    assert status == 200
    assert data["task"]["subject"] == "authorized"
    task_id = data["task_id"]
    assert (
        api(
            f"/openapi/team/demo/task/{task_id}",
            {"status": "completed"},
            method="PATCH",
            headers=headers,
        )[0]
        == 200
    )
    assert (
        api(
            "/openapi/team/demo/messages", {"to": "worker", "content": "external"}, headers=headers
        )[0]
        == 200
    )
    assert api("/openapi/team/demo/broadcast", {"content": "external"}, headers=headers)[0] == 200
    assert api("/openapi/team/demo/agents", headers=headers)[0] == 200
    assert api("/openapi/capabilities?api_key=test-secret")[0] == 401


def test_openapi_exposes_read_only_snapshots(api, monkeypatch):
    monkeypatch.setenv("CLAWTEAM_BOARD_API_KEY", "test-secret")
    headers = {"Authorization": "Bearer test-secret"}
    status, data, _ = api("/openapi/overview", headers=headers)
    assert status == 200
    assert any(team["name"] == "demo" for team in data)
    status, data, _ = api("/openapi/team/demo", headers=headers)
    assert status == 200
    assert data["team"]["name"] == "demo"
    for path in (
        "/openapi/team/demo/messages",
        "/openapi/team/demo/task/missing",
        "/openapi/proxy",
    ):
        assert api(path, headers=headers)[0] == 404


@pytest.mark.parametrize("key", [None, ""])
def test_openapi_requires_configured_key(api, monkeypatch, key):
    if key is not None:
        monkeypatch.setenv("CLAWTEAM_BOARD_API_KEY", key)
    assert api("/openapi/capabilities", headers={"Authorization": "Bearer "})[0] == 401


def test_browser_api_stays_separate(api, monkeypatch):
    monkeypatch.setenv("CLAWTEAM_BOARD_API_KEY", "test-secret")
    assert api("/api/capabilities")[0] == 200
    assert api("/api/team/demo/task", {"subject": "browser"})[0] == 200
    headers = {"Authorization": "Bearer test-secret"}
    assert api("/openapi/team/demo/prompt", headers=headers)[0] == 404
    assert api("/openapi/terminal/ws", headers=headers)[0] == 404
