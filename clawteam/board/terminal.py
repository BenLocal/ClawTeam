"""Live, team-scoped tmux terminals for the board's WebSocket endpoint."""

from __future__ import annotations

import json
import os
import secrets
import select
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from wsproto import ConnectionType, WSConnection
from wsproto.events import (
    AcceptConnection,
    BytesMessage,
    CloseConnection,
    Ping,
    Request,
    TextMessage,
)
from wsproto.utilities import RemoteProtocolError

from clawteam.spawn.registry import get_registry
from clawteam.team.manager import TeamManager
from clawteam.team.tasks import TaskStore

MAX_MESSAGE = 32768
MAX_HISTORY_LINES = 5000
MAX_HISTORY_BYTES = 2 * 1024 * 1024
_VIEWERS = threading.BoundedSemaphore(32)
_CONTROL_LOCK = threading.Lock()
_CONTROLLERS: set[tuple[str, str]] = set()


def _tmux(*args: str) -> str:
    result = subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    if result.returncode:
        raise ValueError("Agent terminal is unavailable")
    return result.stdout


@dataclass(frozen=True)
class Pane:
    session: str
    window: str
    pane: str
    pid: int
    command: str
    dead: bool


def _team_panes(team: str) -> list[tuple[str, Pane]]:
    try:
        output = _tmux(
            "list-panes",
            "-s",
            "-t",
            f"=clawteam-{team}",
            "-F",
            "#{session_name}\t#{window_id}\t#{window_name}\t#{pane_id}\t"
            "#{pane_pid}\t#{pane_dead}\t#{pane_current_command}",
        )
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return []
    panes = []
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) == 7 and fields[4].isdigit():
            session, window, name, pane, pid, dead, command = fields
            panes.append((name, Pane(session, window, pane, int(pid), command, dead == "1")))
    return panes


def _resolve_member(member, registry: dict, panes: list) -> tuple[Pane | None, str]:
    key = TeamManager.inbox_name_for(member)
    info = registry.get(key, {})
    backend = info.get("backend", "tmux")
    if backend != "tmux":
        return None, backend
    # PIDs survive pane tiling; never attach an unrelated window on a stale PID.
    if info.get("pid"):
        return next((p for _, p in panes if p.pid == info["pid"]), None), backend
    return next((p for name, p in panes if name == key), None), backend


def list_terminals(team: str) -> list[dict]:
    config = TeamManager.get_team(team)
    if config is None:
        raise ValueError("Team not found")
    panes = _team_panes(team)
    registry = get_registry(team)
    tasks = TaskStore(team).list_tasks()
    result = []
    for member in config.members:
        key = TeamManager.inbox_name_for(member)
        pane, backend = _resolve_member(member, registry, panes)
        active = [
            t.subject for t in tasks if t.owner in {key, member.name} and t.status == "in_progress"
        ]
        result.append(
            {
                "key": key,
                "name": member.name,
                "agentType": member.agent_type,
                "backend": backend,
                "available": pane is not None and not pane.dead,
                "command": pane.command if pane else "",
                "tasks": active,
                "reason": ""
                if pane and not pane.dead
                else (
                    "Live terminals require the tmux backend"
                    if backend != "tmux"
                    else "Agent terminal is offline"
                ),
            }
        )
    return result


def resolve_pane(team: str, agent: str) -> Pane:
    config = TeamManager.get_team(team)
    if config is None:
        raise ValueError("Team not found")
    member = next((m for m in config.members if TeamManager.inbox_name_for(m) == agent), None)
    if member is None:
        raise ValueError("Agent is not a member of this team")
    pane, _ = _resolve_member(member, get_registry(team), _team_panes(team))
    if pane is None or pane.dead:
        raise ValueError("Agent terminal is unavailable")
    return pane


def capture_history(pane: Pane) -> dict:
    """Read scrollback without entering copy mode on the agent's shared pane."""
    text = _tmux("capture-pane", "-p", "-e", "-J", "-S", f"-{MAX_HISTORY_LINES}", "-t", pane.pane)
    data = text.encode("utf-8")
    truncated = len(data) > MAX_HISTORY_BYTES
    if truncated:
        # Cut at a complete line so UTF-8 and color escapes remain intact.
        text = data[-MAX_HISTORY_BYTES:].decode("utf-8", errors="replace").partition("\n")[2]
    return {"type": "history", "text": text, "truncated": truncated}


def dimensions(cols, rows) -> tuple[int, int]:
    if (
        type(cols) is not int
        or type(rows) is not int
        or not (20 <= cols <= 400 and 5 <= rows <= 150)
    ):
        raise ValueError("Terminal size must be 20–400 columns and 5–150 rows")
    return cols, rows


@dataclass(frozen=True)
class Ticket:
    team: str
    agent: str
    control: bool
    cols: int
    rows: int
    expires: float


class TerminalTickets:
    """Short-lived, single-use capabilities, issued only to same-origin requests."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tickets: dict[str, Ticket] = {}

    def issue(self, payload: dict) -> str:
        team, agent = payload.get("team"), payload.get("agent")
        if not isinstance(team, str) or not isinstance(agent, str):
            raise ValueError("Team and agent are required")
        control = payload.get("control", False)
        if type(control) is not bool:
            raise ValueError("Control must be a boolean")
        cols, rows = dimensions(payload.get("cols", 100), payload.get("rows", 30))
        resolve_pane(team, agent)
        now = time.monotonic()
        with self._lock:
            self._tickets = {k: t for k, t in self._tickets.items() if t.expires > now}
            if len(self._tickets) >= 128:
                raise ValueError("Too many pending terminal connections")
            token = secrets.token_urlsafe(32)
            self._tickets[token] = Ticket(team, agent, control, cols, rows, now + 30)
        return token

    def consume(self, token: str) -> Ticket:
        with self._lock:
            ticket = self._tickets.pop(token, None)
        if ticket is None or ticket.expires <= time.monotonic():
            raise ValueError("Terminal ticket expired; reconnect to continue")
        return ticket


def same_origin(headers) -> bool:
    try:
        origin = urlparse(headers.get("Origin", ""))
    except ValueError:
        return False
    return (
        origin.scheme in {"http", "https"}
        and bool(origin.netloc)
        and origin.netloc.lower() == headers.get("Host", "").lower()
    )


class TmuxTerminal:
    """A disposable tmux client; closing it never terminates the agent's pane."""

    def __init__(self, pane: Pane, ticket: Ticket):
        self.viewer = "clawteam-live-" + secrets.token_hex(12)
        self.process = None
        self.fd = -1
        try:
            import pty

            _tmux("new-session", "-d", "-s", self.viewer, "-t", "=" + pane.session)
            _tmux("set-option", "-t", self.viewer, "status", "off")
            _tmux("set-option", "-t", self.viewer, "prefix", "None")
            _tmux(
                "set-hook",
                "-t",
                self.viewer,
                "client-detached",
                f"kill-session -t {self.viewer}",
            )
            _tmux("select-window", "-t", f"{self.viewer}:{pane.window}")
            if ticket.control:
                _tmux("select-pane", "-t", pane.pane)
            self.fd, slave = pty.openpty()
            try:
                self.resize(ticket.cols, ticket.rows)
                env = dict(os.environ, TERM="xterm-256color")
                env.pop("TMUX", None)
                env.pop("TMUX_PANE", None)
                # A fresh interpreter acquires the controlling tty without preexec_fn
                # (which is unsafe in the threaded HTTP server).
                attach = (
                    "import fcntl,os,sys,termios; "
                    "fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execvp('tmux',sys.argv[1:])"
                )
                flags = "ignore-size" + ("" if ticket.control else ",read-only")
                self.process = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        attach,
                        "tmux",
                        "attach-session",
                        "-f",
                        flags,
                        "-t",
                        self.viewer,
                    ],
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env=env,
                    start_new_session=True,
                )
                os.set_blocking(self.fd, False)
            finally:
                os.close(slave)
        except BaseException:
            self.close()
            raise

    def resize(self, cols: int, rows: int):
        import fcntl
        import termios

        cols, rows = dimensions(cols, rows)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        if self.process is not None:
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        try:
            _tmux("kill-session", "-t", self.viewer)
        except (ValueError, OSError, subprocess.TimeoutExpired):
            pass


def stream_terminal(handler, tickets: TerminalTickets):
    """Upgrade this HTTP connection, then relay bounded WebSocket frames and PTY I/O."""
    if not same_origin(handler.headers):
        handler.send_error(403, "Terminal connections require the same origin")
        return
    if handler.headers.get("Upgrade", "").lower() != "websocket":
        handler.send_error(426, "WebSocket upgrade required")
        return
    token = parse_qs(urlparse(handler.path).query).get("ticket", [""])[0]
    try:
        ticket = tickets.consume(token)
        pane = resolve_pane(ticket.team, ticket.agent)
    except ValueError as exc:
        handler.send_error(403, str(exc))
        return
    if not _VIEWERS.acquire(blocking=False):
        handler.send_error(503, "Too many live terminals")
        return
    controller = (ticket.team, ticket.agent)
    controlling = False
    terminal = None
    try:
        if ticket.control:
            with _CONTROL_LOCK:
                if controller in _CONTROLLERS:
                    handler.send_error(409, "Another viewer is controlling this agent")
                    return
                _CONTROLLERS.add(controller)
                controlling = True

        ws = WSConnection(ConnectionType.SERVER)
        request = (
            handler.requestline
            + "\r\n"
            + "\r\n".join(f"{name}: {value}" for name, value in handler.headers.items())
            + "\r\n\r\n"
        )
        ws.receive_data(request.encode("latin-1"))
        if not any(isinstance(e, Request) for e in ws.events()):
            handler.send_error(400, "Invalid WebSocket handshake")
            return
        terminal = TmuxTerminal(pane, ticket)
        handler.close_connection = True
        sock = handler.connection
        sock.settimeout(10)
        sock.sendall(ws.send(AcceptConnection()))
        chunks = []
        size = 0
        pending_input = bytearray()
        history_message = None
        last_history = 0.0
        last_ping = time.monotonic()
        last_received = last_ping
        while terminal.process.poll() is None:
            ready, writable, _ = select.select(
                [sock, terminal.fd],
                [terminal.fd] if pending_input else [],
                [],
                1,
            )
            if sock in ready:
                data = sock.recv(65536)
                if not data:
                    break
                last_received = time.monotonic()
                ws.receive_data(data)
                for event in ws.events():
                    if isinstance(event, CloseConnection):
                        sock.sendall(ws.send(event.response()))
                        return
                    if isinstance(event, Ping):
                        sock.sendall(ws.send(event.response()))
                    elif isinstance(event, (TextMessage, BytesMessage)):
                        size += len(event.data)
                        if size > MAX_MESSAGE or isinstance(event, BytesMessage):
                            sock.sendall(
                                ws.send(
                                    CloseConnection(code=1009, reason="Invalid terminal message")
                                )
                            )
                            return
                        chunks.append(event.data)
                        if event.message_finished:
                            message = json.loads("".join(chunks))
                            chunks, size = [], 0
                            if not isinstance(message, dict):
                                raise ValueError("Invalid terminal message")
                            if message.get("type") == "resize":
                                terminal.resize(message.get("cols"), message.get("rows"))
                            elif message.get("type") == "history":
                                if history_message is None or time.monotonic() - last_history > 1:
                                    try:
                                        history_message = capture_history(pane)
                                    except (OSError, ValueError, subprocess.TimeoutExpired):
                                        history_message = {
                                            "type": "history",
                                            "error": "Agent history is unavailable",
                                        }
                                    last_history = time.monotonic()
                                sock.sendall(
                                    ws.send(
                                        TextMessage(
                                            data=json.dumps(history_message, ensure_ascii=False)
                                        )
                                    )
                                )
                            elif message.get("type") == "input" and ticket.control:
                                text = message.get("data")
                                if not isinstance(text, str):
                                    raise ValueError("Input must be text")
                                pending_input.extend(text.encode("utf-8"))
                                if len(pending_input) > MAX_MESSAGE * 4:
                                    raise ValueError("Terminal input buffer is full")
            if terminal.fd in writable:
                try:
                    written = os.write(terminal.fd, pending_input)
                    del pending_input[:written]
                except BlockingIOError:
                    pass
            if terminal.fd in ready:
                try:
                    output = os.read(terminal.fd, 16384)
                except BlockingIOError:
                    continue
                if not output:
                    break
                sock.sendall(ws.send(BytesMessage(data=output)))
            if time.monotonic() - last_ping > 20:
                sock.sendall(ws.send(Ping()))
                last_ping = time.monotonic()
            if time.monotonic() - last_received > 60:
                break
        sock.sendall(ws.send(CloseConnection(code=1000, reason="Agent terminal closed")))
    except (OSError, ValueError, subprocess.TimeoutExpired, RemoteProtocolError):
        # Disconnects and vanished panes are normal during agent restarts.
        pass
    finally:
        if terminal is not None:
            terminal.close()
        if controlling:
            with _CONTROL_LOCK:
                _CONTROLLERS.discard(controller)
        _VIEWERS.release()
