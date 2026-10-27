"""Terminal access boundaries and real WebSocket-to-tmux interaction."""

import http.client
import json
import os
import shlex
import shutil
import socket
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer

import pytest
from wsproto import ConnectionType, WSConnection
from wsproto.events import (
    AcceptConnection,
    BytesMessage,
    CloseConnection,
    Ping,
    Request,
    TextMessage,
)

from clawteam.board.collector import BoardCollector
from clawteam.board.server import BoardHandler, TeamSnapshotCache
from clawteam.board.terminal import (
    TerminalTickets,
    dimensions,
    list_terminals,
    resolve_pane,
    same_origin,
)
from clawteam.spawn.registry import register_agent
from clawteam.team.manager import TeamManager


@pytest.fixture
def team():
    TeamManager.create_team("live-test", "leader", "leader-id")
    return "live-test"


@pytest.fixture
def api(team):
    class Handler(BoardHandler):
        terminal_tickets = TerminalTickets()
        collector = BoardCollector()
        team_cache = TeamSnapshotCache(0.1)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def request(api, path, payload=None, origin=True):
    host, port = api
    connection = http.client.HTTPConnection(host, port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = f"http://{host}:{port}" if origin is True else origin
    connection.request(
        "POST" if payload is not None else "GET",
        path,
        body=json.dumps(payload) if payload is not None else None,
        headers=headers,
    )
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


@pytest.mark.parametrize(
    "origin,host,allowed",
    [
        ("http://localhost:8080", "localhost:8080", True),
        ("https://teams.example", "teams.example", True),
        ("https://other.example", "teams.example", False),
        ("http://localhost:9000", "localhost:8080", False),
        ("null", "localhost", False),
        ("", "localhost", False),
    ],
)
def test_origin_boundary(origin, host, allowed):
    assert same_origin({"Origin": origin, "Host": host}) is allowed


@pytest.mark.parametrize("size", [(0, 30), (401, 20), (80, 151), (True, 30), (80, "30")])
def test_invalid_dimensions(size):
    with pytest.raises(ValueError):
        dimensions(*size)


def test_member_resolution_survives_tiling_and_rejects_stale_pid(team, monkeypatch):
    TeamManager.add_member(team, "developer", "dev-id", user="alice")
    register_agent(team, "alice_developer", "tmux", pid=42, tmux_target="old:window")
    monkeypatch.setattr(
        "clawteam.board.terminal._tmux",
        lambda *args: (
            "clawteam-live-test\t@1\tcombined\t%1\t42\t0\tclaude\n"
            "clawteam-live-test\t@2\tleader\t%2\t43\t0\tbash\n"
        ),
    )
    assert resolve_pane(team, "alice_developer").pane == "%1"
    with pytest.raises(ValueError, match="not a member"):
        resolve_pane(team, "developer")
    register_agent(team, "alice_developer", "tmux", pid=999)
    with pytest.raises(ValueError, match="unavailable"):
        resolve_pane(team, "alice_developer")


def test_unsupported_backend_has_explicit_offline_state(team, monkeypatch):
    register_agent(team, "leader", "subprocess", pid=os.getpid())
    monkeypatch.setattr("clawteam.board.terminal._team_panes", lambda _: [])
    member = list_terminals(team)[0]
    assert not member["available"]
    assert member["backend"] == "subprocess"
    assert "tmux" in member["reason"]


def test_tickets_are_single_use_scoped_and_expire(team, monkeypatch):
    monkeypatch.setattr("clawteam.board.terminal.resolve_pane", lambda *args: None)
    tickets = TerminalTickets()
    token = tickets.issue({"team": team, "agent": "leader"})
    ticket = tickets.consume(token)
    assert ticket.team == team and ticket.agent == "leader" and not ticket.control
    with pytest.raises(ValueError, match="expired"):
        tickets.consume(token)
    token = tickets.issue({"team": team, "agent": "leader"})
    monkeypatch.setattr("clawteam.board.terminal.time.monotonic", lambda: ticket.expires + 31)
    with pytest.raises(ValueError, match="expired"):
        tickets.consume(token)


def test_http_rejects_cross_origin_and_malformed_requests(api):
    for origin in (False, "http://evil.example"):
        status, headers, _ = request(api, "/api/terminal/ticket", {}, origin=origin)
        assert status == 403
        assert "Access-Control-Allow-Origin" not in headers
    for payload in (
        [],
        {"team": "../escape", "agent": "leader"},
        {"team": "live-test"},
        {"team": "live-test", "agent": "leader", "control": "yes"},
    ):
        status, _, _ = request(api, "/api/terminal/ticket", payload)
        assert status == 400
    status, headers, body = request(api, "/api/team/live-test/terminals")
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in headers
    assert json.loads(body)["members"][0]["key"] == "leader"


def test_terminal_assets_are_local_and_paths_are_allowlisted(api):
    for path, content_type in (
        ("/terminals.html", "text/html"),
        ("/terminals.js", "text/javascript"),
        ("/vendor/ghostty-web/ghostty-web.js", "text/javascript"),
    ):
        status, headers, body = request(api, path)
        assert status == 200 and body
        assert headers["Content-Type"].split(";", 1)[0] == content_type
    assert request(api, "/vendor/ghostty-web/../../server.py")[0] == 404
    assert request(api, "/api/terminal/ws", origin=False)[0] == 403
    assert request(api, "/api/terminal/ws")[0] == 426


def test_terminal_cli_serves_selected_team(team, monkeypatch):
    from typer.testing import CliRunner

    from clawteam.cli.commands import app

    calls = []
    monkeypatch.setattr("clawteam.board.server.serve", lambda **kwargs: calls.append(kwargs))
    result = CliRunner().invoke(app, ["board", "terminals", team, "--port", "8099"])
    assert result.exit_code == 0, result.output
    assert f"/terminals.html?team={team}" in result.output
    assert calls == [{"host": "127.0.0.1", "port": 8099, "default_team": team}]
    assert CliRunner().invoke(app, ["board", "terminals", "missing"]).exit_code == 1


@pytest.fixture
def real_tmux(team, tmp_path, monkeypatch):
    if not shutil.which("tmux") or os.name != "posix":
        pytest.skip("tmux and POSIX PTYs are required")
    directory = tmp_path / "tmux"
    directory.mkdir(mode=0o700)
    monkeypatch.setenv("TMUX_TMPDIR", str(directory))
    monkeypatch.delenv("TMUX", raising=False)
    session = "clawteam-" + team
    script = (
        "import sys; print(*['HISTORY_%03d'%i for i in range(250)],sep=chr(10),flush=True); "
        "print('LIVE_READY',flush=True);\nfor line in sys.stdin: print('ECHO:'+line,flush=True)"
    )
    command = shlex.join([shutil.which("python3"), "-u", "-c", script])
    subprocess.run(
        ["tmux", "-f", "/dev/null", "new-session", "-d", "-s", session, "-n", "leader", command],
        check=True,
    )
    yield session
    subprocess.run(["tmux", "kill-server"], check=False, capture_output=True)


class TerminalClient:
    def __init__(self, api, team, control=False):
        status, _, body = request(
            api,
            "/api/terminal/ticket",
            {
                "team": team,
                "agent": "leader",
                "control": control,
                "cols": 80,
                "rows": 24,
            },
        )
        assert status == 200, body
        token = json.loads(body)["ticket"]
        self.ws = WSConnection(ConnectionType.CLIENT)
        self.sock = socket.create_connection(api, timeout=4)
        host = f"{api[0]}:{api[1]}"
        self.sock.sendall(
            self.ws.send(
                Request(
                    host=host,
                    target="/api/terminal/ws?ticket=" + token,
                    extra_headers=[(b"origin", f"http://{host}".encode())],
                )
            )
        )
        self.output = bytearray()
        self.accepted = False

    def read_until(self, expected, timeout=5):
        deadline = time.monotonic() + timeout
        while expected not in self.output and time.monotonic() < deadline:
            self.ws.receive_data(self.sock.recv(65536))
            for event in self.ws.events():
                if isinstance(event, AcceptConnection):
                    self.accepted = True
                elif isinstance(event, BytesMessage):
                    self.output.extend(event.data)
                elif isinstance(event, Ping):
                    self.sock.sendall(self.ws.send(event.response()))
                elif isinstance(event, CloseConnection):
                    raise AssertionError(f"Terminal closed: {event}")
        assert expected in self.output, self.output

    def send(self, message):
        self.sock.sendall(self.ws.send(TextMessage(data=json.dumps(message))))

    def close(self):
        self.sock.close()


def test_real_terminal_watch_control_resize_and_disconnect(api, team, real_tmux):
    watch = TerminalClient(api, team)
    control = None
    try:
        watch.read_until(b"LIVE_READY")
        assert watch.accepted
        watch.send({"type": "input", "data": "FORBIDDEN\r"})
        control = TerminalClient(api, team, control=True)
        control.read_until(b"LIVE_READY")
        control.send({"type": "resize", "cols": 100, "rows": 30})
        control.send({"type": "input", "data": "operator-input\r"})
        control.read_until(b"ECHO:operator-input")
        watch.read_until(b"ECHO:operator-input")
        screen = subprocess.check_output(["tmux", "capture-pane", "-p", "-t", real_tmux])
        assert b"FORBIDDEN" not in screen
    finally:
        watch.close()
        if control:
            control.close()
    for _ in range(50):
        sessions = subprocess.check_output(["tmux", "list-sessions", "-F", "#{session_name}"])
        if sessions.splitlines() == [real_tmux.encode()]:
            break
        time.sleep(0.05)
    assert sessions.splitlines() == [real_tmux.encode()]
    assert subprocess.run(["tmux", "has-session", "-t", real_tmux], check=False).returncode == 0


def test_only_one_viewer_can_control_an_agent(api, team, real_tmux):
    controller = TerminalClient(api, team, control=True)
    other = None
    try:
        controller.read_until(b"LIVE_READY")
        other = TerminalClient(api, team, control=True)
        assert b"409" in other.sock.recv(65536)
        controller.send({"type": "input", "data": "still-controlling\r"})
        controller.read_until(b"ECHO:still-controlling")
    finally:
        controller.close()
        if other:
            other.close()


def test_read_only_viewer_can_read_history_without_changing_agent_mode(api, team, real_tmux):
    viewer = TerminalClient(api, team)
    try:
        viewer.read_until(b"LIVE_READY")
        viewer.send({"type": "history"})
        chunks = []
        history = None
        while history is None:
            viewer.ws.receive_data(viewer.sock.recv(65536))
            for event in viewer.ws.events():
                if isinstance(event, TextMessage):
                    chunks.append(event.data)
                    if event.message_finished:
                        history = json.loads("".join(chunks))
                elif isinstance(event, Ping):
                    viewer.sock.sendall(viewer.ws.send(event.response()))
        assert history["type"] == "history"
        assert "HISTORY_000" in history["text"]
        assert "HISTORY_249" in history["text"]
        assert "LIVE_READY" in history["text"]
        assert (
            subprocess.check_output(
                ["tmux", "display-message", "-p", "-t", real_tmux, "#{pane_in_mode}"]
            ).strip()
            == b"0"
        )
    finally:
        viewer.close()
