"""Persistent team instructions, HTTP editing and startup injection."""

import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import pytest

from clawteam.board.server import BoardHandler
from clawteam.harness.prompts import build_harness_system_prompt
from clawteam.spawn.prompt import build_agent_prompt
from clawteam.team.mailbox import MailboxManager
from clawteam.team.manager import MAX_TEAM_PROMPT_LENGTH, TeamManager


@pytest.fixture
def teams():
    for name in ("alpha", "beta"):
        TeamManager.create_team(name, "leader", name + "-leader")
    TeamManager.add_member("alpha", "developer", "dev", user="alice")


@pytest.fixture
def api_request(teams):
    class Handler(BoardHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def send(method="GET", team="alpha", payload=None, raw=None, headers=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        body = raw if raw is not None else json.dumps(payload) if payload is not None else None
        connection.request(
            method,
            f"/api/team/{quote(team, safe='')}/prompt",
            body=body,
            headers=headers or {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    yield send
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_prompt_is_persistent_isolated_and_preserves_config(teams, isolated_data_dir):
    config = isolated_data_dir / "teams/alpha/config.json"
    original = config.read_bytes()
    text = "代码仓库: https://example.com/repo\nVikunja: project 42\n  keep whitespace  \n"
    assert TeamManager.get_team_prompt("alpha") == ""
    assert TeamManager.set_team_prompt("alpha", text)
    assert (isolated_data_dir / "teams/alpha/prompt.md").read_text() == text
    assert config.read_bytes() == original
    TeamManager.add_member("alpha", "tester", "qa")
    assert TeamManager.get_team_prompt("alpha") == text
    assert TeamManager.get_team_prompt("beta") == ""
    assert not TeamManager.set_team_prompt("alpha", text)
    assert TeamManager.set_team_prompt("alpha", "")
    assert TeamManager.get_team_prompt("alpha") == ""


def test_old_teams_and_unknown_teams(teams):
    assert TeamManager.get_team_prompt("alpha") == ""
    assert TeamManager.get_team_prompt("missing") is None
    with pytest.raises(ValueError, match="not found"):
        TeamManager.set_team_prompt("missing", "rules")
    with pytest.raises(ValueError, match="Invalid team name"):
        TeamManager.get_team_prompt("../escape")


def test_startup_prompts_load_current_instructions_for_all_roles(teams):
    TeamManager.set_team_prompt("alpha", "Use the staging environment and Vikunja project 42.")
    for role in ("leader", "developer", "tester"):
        prompt = build_agent_prompt(role, role, role, "alpha", "leader", "Inspect tests")
        assert "## Team Instructions" in prompt
        assert "Vikunja project 42" in prompt
        assert prompt.index("## Team Instructions") < prompt.index("## Task")
        assert "Vikunja project 42" in build_harness_system_prompt("alpha", role)
        assert "Vikunja project 42" not in build_agent_prompt(
            role, role, role, "beta", "leader", "Inspect tests"
        )
    TeamManager.set_team_prompt("alpha", "")
    assert "## Team Instructions" not in build_agent_prompt(
        "leader", "id", "leader", "alpha", "leader", "task"
    )
    assert "## Team Instructions" not in build_harness_system_prompt("alpha", "leader")


def test_http_save_reload_notify_and_clear(api_request):
    text = "测试环境: https://stage.example.com\n</textarea><script>alert(1)</script>"
    assert api_request() == (200, {"prompt": ""})
    status, saved = api_request("POST", payload={"prompt": text})
    assert status == 200
    assert saved == {"prompt": text, "notified": 2, "warning": ""}
    assert api_request() == (200, {"prompt": text})
    assert api_request(team="beta") == (200, {"prompt": ""})
    for member in ("leader", "alice_developer"):
        messages = MailboxManager("alpha").peek(member)
        assert len(messages) == 1
        assert text in messages[0].content
        assert messages[0].key == "team-prompt"
    assert not MailboxManager("beta").peek("leader")
    assert api_request("POST", payload={"prompt": text})[1]["notified"] == 0
    assert api_request("POST", payload={"prompt": ""})[1]["notified"] == 2
    assert api_request() == (200, {"prompt": ""})
    assert "cleared" in MailboxManager("alpha").peek("leader")[-1].content


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"prompt": None},
        {"prompt": 123},
        {"prompt": ["text"]},
        {"prompt": "x" * (MAX_TEAM_PROMPT_LENGTH + 1)},
    ],
)
def test_invalid_requests_do_not_replace_existing_prompt(api_request, payload):
    TeamManager.set_team_prompt("alpha", "original")
    status, result = api_request("POST", raw=json.dumps(payload))
    assert status == 400
    assert result["error"]
    assert TeamManager.get_team_prompt("alpha") == "original"
    assert not MailboxManager("alpha").peek("leader")


def test_bad_json_and_large_request(api_request):
    assert api_request("POST", raw="{")[0] == 400
    assert api_request("POST", raw="", headers={"Content-Length": str(1024 * 1024 + 1)})[0] == 413
    assert api_request("POST", raw="", headers={"Content-Length": "invalid"})[0] == 400
    assert api_request("POST", raw="", headers={"Content-Length": "-1"})[0] == 413


def test_missing_and_invalid_team(api_request):
    for method in ("GET", "POST"):
        assert api_request(method, team="missing", payload={"prompt": "text"})[0] == 404
        assert api_request(method, team="../escape", payload={"prompt": "text"})[0] == 400


def test_notification_failure_reports_saved_state(api_request, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("transport unavailable")

    monkeypatch.setattr(MailboxManager, "send", fail)
    status, data = api_request("POST", payload={"prompt": "saved despite notification failure"})
    assert status == 200
    assert data["warning"]
    assert data["notified"] == 0
    assert api_request()[1]["prompt"] == "saved despite notification failure"
