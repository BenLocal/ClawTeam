"""HTTP server for the Web UI dashboard and live agent terminals."""

from __future__ import annotations

import ipaddress
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from clawteam.board.collector import BoardCollector
from clawteam.board.terminal import TerminalTickets, list_terminals, same_origin, stream_terminal

_STATIC_DIR = Path(__file__).parent / "static"
_ALLOWED_PROXY_HOSTS = {
    "api.github.com",
    "github.com",
    "raw.githubusercontent.com",
}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects for proxied fetches."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code, msg, headers, fp)


def _is_blocked_hostname(hostname: str) -> bool:
    host = hostname.strip().lower()
    if host in {"localhost"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved


def _normalize_proxy_target(target_url: str) -> str:
    parsed = urlparse(target_url)
    if parsed.scheme != "https":
        raise ValueError("Proxy only allows https URLs")

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValueError("Proxy URL must include a hostname")
    if _is_blocked_hostname(hostname):
        raise ValueError("Proxy target is not allowed")

    if hostname == "github.com":
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) == 2:
            return f"https://api.github.com/repos/{parts[0]}/{parts[1]}/readme"
        return target_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")

    if hostname not in _ALLOWED_PROXY_HOSTS:
        raise ValueError("Proxy only allows GitHub-hosted content")

    return target_url


def _fetch_proxy_content(target_url: str) -> bytes:
    normalized = _normalize_proxy_target(target_url)
    opener = urllib.request.build_opener(_NoRedirectHandler)
    req = urllib.request.Request(normalized, headers={"User-Agent": "ClawTeam-Server"})
    with opener.open(req, timeout=10) as resp:
        final_url = resp.geturl()
        _normalize_proxy_target(final_url)
        body = resp.read()

    if normalized.startswith("https://api.github.com/repos/") and final_url == normalized:
        payload = json.loads(body.decode("utf-8"))
        download_url = payload.get("download_url")
        if not download_url:
            raise ValueError("GitHub README proxy target has no downloadable content")
        normalized = _normalize_proxy_target(download_url)
        req = urllib.request.Request(normalized, headers={"User-Agent": "ClawTeam-Server"})
        with opener.open(req, timeout=10) as resp:
            _normalize_proxy_target(resp.geturl())
            return resp.read()

    return body


@dataclass
class TeamSnapshotCache:
    """Tiny TTL cache for full team snapshots shared across HTTP handlers."""

    ttl_seconds: float
    _entries: dict[str, tuple[float, dict]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, team_name: str, loader) -> dict:
        with self._lock:
            entry = self._entries.get(team_name)
            if entry and time.monotonic() - entry[0] < self.ttl_seconds:
                return entry[1]

        # Load outside the lock so one slow collector run does not block all
        # other readers. Concurrent expiry can trigger duplicate refreshes, but
        # this path only rebuilds an in-memory snapshot and the latest result wins.
        data = loader()
        loaded_at = time.monotonic()
        with self._lock:
            self._entries[team_name] = (loaded_at, data)
        return data


class BoardHandler(BaseHTTPRequestHandler):
    """HTTP handler for the board Web UI."""

    collector: BoardCollector
    default_team: str = ""
    interval: float = 2.0
    team_cache: TeamSnapshotCache
    terminal_tickets = TerminalTickets()

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/" or path == "/index.html":
            self._serve_static("index.html", "text/html")
        elif path == "/terminals.html":
            self._serve_static("terminals.html", "text/html")
        elif path == "/terminals.js":
            self._serve_static("terminals.js", "text/javascript")
        elif path.startswith("/vendor/ghostty-web/"):
            filename = path.removeprefix("/vendor/ghostty-web/")
            if filename in {"ghostty-web.js", "__vite-browser-external-2447137e.js"}:
                self._serve_static("vendor/ghostty-web/" + filename, "text/javascript")
            else:
                self.send_error(404)
        elif path == "/api/terminal/ws":
            stream_terminal(self, self.terminal_tickets)
        elif path == "/api/overview":
            self._serve_json(self.collector.collect_overview())
        elif path.startswith("/api/team/") and path.endswith("/terminals"):
            try:
                team = unquote(path[len("/api/team/") : -len("/terminals")])
                self._serve_json({"team": team, "members": list_terminals(team)}, cors=False)
            except (ValueError, OSError) as exc:
                self._serve_json({"error": str(exc)}, status=400, cors=False)
        elif path.startswith("/api/team/") and path.endswith("/prompt"):
            self._serve_team_prompt(unquote(path[len("/api/team/") : -len("/prompt")]))
        elif path.startswith("/api/team/"):
            team_name = path[len("/api/team/") :].strip("/")
            if not team_name:
                self.send_error(400, "Team name required")
                return
            self._serve_team(team_name)
        elif path.startswith("/api/events/"):
            team_name = path[len("/api/events/") :].strip("/")
            if not team_name:
                self.send_error(400, "Team name required")
                return
            self._serve_sse(team_name)
        elif path.startswith("/api/proxy"):
            query = parse_qs(urlparse(self.path).query)
            target_url = query.get("url", [""])[0]
            if not target_url:
                self.send_error(400, "URL required")
                return
            try:
                content = _fetch_proxy_content(target_url)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(content)
            except ValueError as e:
                self.send_error(403, str(e))
            except Exception as e:
                self.send_error(500, str(e))
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/terminal/ticket":
            if not same_origin(self.headers):
                self._serve_json(
                    {"error": "Terminal requests require the same origin"}, status=403, cors=False
                )
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 32768:
                    raise ValueError("Invalid terminal request size")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Terminal request must be an object")
                token = self.terminal_tickets.issue(payload)
                self._serve_json({"ticket": token}, cors=False)
            except (ValueError, UnicodeError, OSError) as exc:
                self._serve_json({"error": str(exc)}, status=400, cors=False)
            return
        if path.startswith("/api/team/") and path.endswith("/prompt"):
            self._serve_team_prompt(unquote(path[len("/api/team/") : -len("/prompt")]), save=True)
            return
        if path.startswith("/api/team/") and path.endswith("/task"):
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[3] == "task":
                team_name = parts[2]
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length).decode("utf-8")
                try:
                    payload = json.loads(body)
                    from clawteam.team.tasks import TaskStore

                    store = TaskStore(team_name)
                    task = store.create(
                        subject=payload.get("subject", ""),
                        description=payload.get("description", ""),
                        owner=payload.get("owner", ""),
                    )
                    self._serve_json({"status": "ok", "task_id": task.id})
                except Exception as e:
                    self.send_error(400, str(e))
                return
        self.send_error(404)

    def _serve_team_prompt(self, team_name: str, *, save: bool = False):
        from clawteam.team.manager import TeamManager

        try:
            prompt = TeamManager.get_team_prompt(team_name)
            if prompt is None:
                self._serve_json({"error": "Team not found"}, status=404)
                return
            if not save:
                self._serve_json({"prompt": prompt})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 1024 * 1024:
                self._serve_json({"error": "Prompt request is too large"}, status=413)
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict) or "prompt" not in payload:
                raise ValueError("A prompt field is required")
            prompt = payload["prompt"]
            changed = TeamManager.set_team_prompt(team_name, prompt)
        except (ValueError, UnicodeError) as exc:
            self._serve_json({"error": str(exc)}, status=400)
            return
        except OSError:
            self._serve_json({"error": "Unable to read or save the team prompt"}, status=500)
            return

        notified = 0
        warning = ""
        if changed:
            from clawteam.team.mailbox import MailboxManager

            content = (
                "The shared team prompt has been updated. Replace the previous team instructions "
                "with the following on your next inbox check:\n\n" + prompt
                if prompt
                else "The shared team prompt has been cleared. Stop applying its previous instructions."
            )
            try:
                mailbox = MailboxManager(team_name)
                for member in TeamManager.list_members(team_name):
                    mailbox.send(
                        from_agent="board",
                        to=TeamManager.inbox_name_for(member),
                        content=content,
                        key="team-prompt",
                    )
                    notified += 1
            except Exception:
                warning = (
                    "Prompt saved, but some members could not be notified. Restart them to load it."
                )
        self._serve_json({"prompt": prompt, "notified": notified, "warning": warning})

    def _serve_static(self, filename: str, content_type: str):
        filepath = _STATIC_DIR / filename
        if not filepath.exists():
            self.send_error(404, f"Static file not found: {filename}")
            return
        content = filepath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_json(self, data, status: int = 200, *, cors: bool = True):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_team(self, team_name: str):
        try:
            data = self.collector.collect_team(team_name)
            self._serve_json(data)
        except ValueError as e:
            body = json.dumps({"error": str(e)}).encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _serve_sse(self, team_name: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            while True:
                try:
                    data = self.team_cache.get(
                        team_name,
                        lambda: self.collector.collect_team(team_name),
                    )
                except ValueError as e:
                    data = {"error": str(e)}
                payload = json.dumps(data, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(self.interval)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def log_message(self, format, *args):
        # Suppress default stderr logging for SSE connections
        first = str(args[0]) if args else ""
        if "/api/events/" not in first and "/api/terminal/" not in first:
            super().log_message(format, *args)


def serve(
    host: str = "127.0.0.1",
    port: int = 8080,
    default_team: str = "",
    interval: float = 2.0,
):
    """Start the Web UI server."""
    collector = BoardCollector()
    BoardHandler.collector = collector
    BoardHandler.default_team = default_team
    BoardHandler.interval = interval
    BoardHandler.team_cache = TeamSnapshotCache(ttl_seconds=interval)

    server = ThreadingHTTPServer((host, port), BoardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
