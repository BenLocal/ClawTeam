import { init, Terminal, FitAddon } from "./vendor/ghostty-web/ghostty-web.js";

const team = new URL(location.href).searchParams.get("team");
const cards = new Map();
const root = document.getElementById("terminals");
const pageStatus = document.getElementById("page-status");
const summary = document.getElementById("summary");
let stopped = false;
let refreshTimer;
// Only the selected card takes the wheel; otherwise it scrolls the page.
let selected = null;

function select(card) {
  if (selected === card) return;
  selected?.element.classList.remove("selected");
  selected = card;
  card?.element.classList.add("selected");
}

document.addEventListener(
  "pointerdown",
  (event) => {
    if (!event.target.closest?.(".terminal-card")) select(null);
  },
  { capture: true },
);

document.getElementById("team-name").textContent = team || "";
const back = new URL("./", location.href);
if (team) back.searchParams.set("team", team);
document.getElementById("back-link").href = back.href;

async function request(path, payload) {
  const response = await fetch(
    new URL(path, location.href),
    payload
      ? {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        }
      : { cache: "no-store" },
  );
  const data = await response.json();
  if (!response.ok)
    throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

class AgentTerminal {
  constructor(member) {
    this.member = member;
    this.generation = 0;
    this.control = false;
    this.disposed = false;
    this.retryDelay = 1000;
    this.element = document.createElement("section");
    this.element.className = "terminal-card";
    this.element.dataset.agent = member.key;
    this.element.innerHTML = `
            <div class="card-head"><span class="agent-name"></span><span class="agent-role"></span><span class="connection" role="status"></span></div>
            <p class="task"></p>
            <div class="card-actions"><button class="expand" type="button">Expand</button><button class="reconnect" type="button">Reconnect</button><button class="history" type="button">History</button><button class="control" type="button">Take control</button></div>
            <p class="history-note" role="status"></p>
            <div class="terminal-host"></div><p class="card-error" role="status"></p>`;
    this.host = this.element.querySelector(".terminal-host");
    this.status = this.element.querySelector(".connection");
    this.error = this.element.querySelector(".card-error");
    this.controlButton = this.element.querySelector(".control");
    this.historyButton = this.element.querySelector(".history");
    this.historyNote = this.element.querySelector(".history-note");
    this.historyButton.addEventListener("click", () => {
      if (this.historyTerminal || this.historyLoading) this.closeHistory();
      else this.openHistory(-Math.max(5, this.terminal?.rows || 24));
    });
    this.element.addEventListener("pointerdown", () => select(this));
    this.element.addEventListener("focusin", () => select(this));
    this.host.addEventListener(
      "wheel",
      (event) => {
        if (event.ctrlKey || !event.deltaY) return;
        if (selected !== this && document.fullscreenElement !== this.element) {
          // Keep ghostty from consuming it, but let the browser scroll the page.
          event.stopImmediatePropagation();
          return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        const lines =
          Math.sign(event.deltaY) *
          Math.max(1, Math.ceil(Math.abs(event.deltaY) / 20));
        if (this.historyTerminal) {
          if (lines > 0 && this.historyTerminal.viewportY === 0)
            this.closeHistory();
          else this.historyTerminal.scrollLines(lines);
        } else if (this.historyLoading) this.historyOffset += lines;
        else if (lines < 0) this.openHistory(lines);
      },
      { capture: true, passive: false },
    );
    this.host.addEventListener(
      "keydown",
      (event) => {
        if (this.historyTerminal) {
          const actions = {
            ArrowUp: () => this.historyTerminal.scrollLines(-1),
            ArrowDown: () => this.historyTerminal.scrollLines(1),
            PageUp: () => this.historyTerminal.scrollPages(-1),
            PageDown: () => this.historyTerminal.scrollPages(1),
            Home: () => this.historyTerminal.scrollToTop(),
            End: () => this.historyTerminal.scrollToBottom(),
            Escape: () => this.closeHistory(),
          };
          if (actions[event.key]) {
            event.preventDefault();
            event.stopImmediatePropagation();
            actions[event.key]();
          }
        } else if (event.shiftKey && event.key === "PageUp") {
          event.preventDefault();
          event.stopImmediatePropagation();
          this.openHistory(-Math.max(5, this.terminal?.rows || 24));
        }
      },
      { capture: true },
    );
    this.element.querySelector(".agent-name").textContent = member.name;
    this.element.querySelector(".agent-role").textContent = member.agentType;
    this.controlButton.addEventListener("click", () =>
      this.connect(!this.control),
    );
    this.element
      .querySelector(".reconnect")
      .addEventListener("click", () => this.connect(false));
    this.element
      .querySelector(".expand")
      .addEventListener("click", async () => {
        try {
          if (document.fullscreenElement === this.element)
            await document.exitFullscreen();
          else await this.element.requestFullscreen();
        } catch (error) {
          this.error.textContent = error.message;
        }
      });
    root.appendChild(this.element);
    this.observer = new ResizeObserver(() => this.fit());
    this.observer.observe(this.host);
    this.update(member);
    if (member.available) this.connect(false);
  }

  update(member) {
    const becameAvailable = !this.member.available && member.available;
    this.member = member;
    this.element.querySelector(".task").textContent = member.tasks.length
      ? member.tasks.join(" · ")
      : member.command || member.reason;
    this.controlButton.disabled = !member.available;
    if (!member.available) {
      this.disconnect();
      this.element.classList.add("offline");
      this.host.textContent = member.reason;
      this.setStatus("Offline", "offline");
    } else if (becameAvailable) this.connect(false);
  }

  setStatus(text, state) {
    this.status.textContent = text;
    this.status.dataset.state = state;
  }

  fit() {
    if (!this.terminal || !this.host.clientWidth) return;
    if (this.historyTerminal) {
      this.historyFit.fit();
      return;
    }
    this.fitAddon.fit();
    const cols = Math.max(20, Math.min(400, this.terminal.cols));
    const rows = Math.max(5, Math.min(150, this.terminal.rows));
    if (cols !== this.terminal.cols || rows !== this.terminal.rows)
      this.terminal.resize(cols, rows);
    this.send({ type: "resize", cols, rows });
  }

  send(message) {
    if (this.socket?.readyState === WebSocket.OPEN)
      this.socket.send(JSON.stringify(message));
  }

  openHistory(offset) {
    if (this.socket?.readyState !== WebSocket.OPEN) return;
    this.historyLoading = true;
    this.historyOffset = offset;
    this.historyButton.textContent = "Back to live";
    this.historyNote.textContent = "Loading history…";
    this.send({ type: "history" });
  }

  showHistory(message) {
    if (!this.historyLoading) return;
    if (message.error) {
      this.closeHistory();
      this.error.textContent = message.error;
      return;
    }
    this.historyLoading = false;
    this.liveHost.hidden = true;
    this.historyHost.hidden = false;
    this.historyHost.replaceChildren();
    const terminal = new Terminal({
      fontSize: 13,
      cursorBlink: false,
      scrollback: 5000,
      theme: {
        background: "#0d1119",
        foreground: "#dfe6f1",
        cursor: "#e8b36b",
      },
    });
    this.historyTerminal = terminal;
    this.historyFit = new FitAddon();
    terminal.loadAddon(this.historyFit);
    terminal.attachCustomKeyEventHandler(() => false);
    terminal.open(this.historyHost);
    this.historyFit.fit();
    const generation = this.generation;
    terminal.write(message.text.replace(/\n/g, "\r\n") + "\x1b[0m", () => {
      if (generation !== this.generation || this.historyTerminal !== terminal)
        return;
      terminal.scrollToBottom();
      terminal.scrollLines(this.historyOffset);
    });
    terminal.focus();
    this.historyNote.textContent =
      "History snapshot. Input paused. Scroll or use Page Up / Page Down; Esc returns to live." +
      (message.truncated ? " Older output was trimmed." : "");
  }

  closeHistory(refocus = true) {
    this.historyLoading = false;
    this.historyTerminal?.dispose();
    this.historyTerminal = null;
    if (this.liveHost) this.liveHost.hidden = false;
    if (this.historyHost) {
      this.historyHost.hidden = true;
      this.historyHost.replaceChildren();
    }
    this.historyButton.textContent = "History";
    this.historyNote.textContent = "";
    if (refocus) {
      this.fit();
      if (this.control) this.terminal?.focus();
    }
  }

  disconnect() {
    this.generation++;
    this.closeHistory(false);
    clearTimeout(this.retryTimer);
    if (this.socket) {
      this.socket.onclose = null;
      this.socket.close();
      this.socket = null;
    }
    this.inputSubscription?.dispose();
    this.terminal?.dispose();
    this.terminal = null;
    this.control = false;
    this.element.classList.remove("controlling");
    this.controlButton.textContent = "Take control";
  }

  async connect(control) {
    this.disconnect();
    if (this.disposed || !this.member.available || stopped) return;
    const generation = this.generation;
    this.error.textContent = "";
    this.element.classList.remove("offline");
    this.host.replaceChildren();
    this.liveHost = document.createElement("div");
    this.liveHost.className = "terminal-surface";
    this.historyHost = document.createElement("div");
    this.historyHost.className = "terminal-surface";
    this.historyHost.hidden = true;
    this.host.append(this.liveHost, this.historyHost);
    this.setStatus("Connecting…", "connecting");
    this.controlButton.disabled = true;
    try {
      this.terminal = new Terminal({
        fontSize: 13,
        cursorBlink: control,
        scrollback: 5000,
        theme: {
          background: "#0d1119",
          foreground: "#dfe6f1",
          cursor: "#e8b36b",
        },
      });
      this.fitAddon = new FitAddon();
      this.terminal.loadAddon(this.fitAddon);
      this.terminal.open(this.liveHost);
      this.fit();
      const data = await request("api/terminal/ticket", {
        team,
        agent: this.member.key,
        control,
        cols: this.terminal.cols,
        rows: this.terminal.rows,
      });
      if (generation !== this.generation || this.disposed || stopped) return;
      const url = new URL("api/terminal/ws", location.href);
      url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
      url.searchParams.set("ticket", data.ticket);
      const socket = new WebSocket(url);
      socket.binaryType = "arraybuffer";
      this.socket = socket;
      socket.onopen = () => {
        if (generation !== this.generation) return socket.close();
        this.retryDelay = 1000;
        this.control = control;
        this.element.classList.toggle("controlling", control);
        this.controlButton.disabled = false;
        this.controlButton.textContent = control
          ? "Release control"
          : "Take control";
        this.setStatus(control ? "Controlling" : "Watching", "connected");
        this.fit();
        if (control) {
          select(this);
          this.terminal.focus();
        }
      };
      socket.onmessage = (event) => {
        if (generation !== this.generation) return;
        if (typeof event.data === "string") {
          const message = JSON.parse(event.data);
          if (message.type === "history") this.showHistory(message);
        } else this.terminal.write(new Uint8Array(event.data));
      };
      socket.onclose = () => {
        if (generation !== this.generation || this.disposed) return;
        this.control = false;
        this.controlButton.textContent = "Take control";
        this.controlButton.disabled = false;
        this.element.classList.remove("controlling");
        this.setStatus("Disconnected", "disconnected");
        this.error.textContent = control
          ? "Control ended. Reconnecting in watch mode; another viewer may already have control."
          : "Connection lost. Reconnecting…";
        this.retryTimer = setTimeout(
          () => this.connect(false),
          this.retryDelay,
        );
        this.retryDelay = Math.min(10000, this.retryDelay * 2);
      };
      this.inputSubscription = this.terminal.onData((data) => {
        if (
          this.control &&
          !this.historyTerminal &&
          !this.historyLoading &&
          generation === this.generation
        )
          this.send({ type: "input", data });
      });
    } catch (error) {
      if (generation !== this.generation || this.disposed) return;
      this.controlButton.disabled = !this.member.available;
      this.error.textContent = error.message;
      this.setStatus("Unavailable", "disconnected");
      this.retryTimer = setTimeout(() => this.connect(false), this.retryDelay);
      this.retryDelay = Math.min(10000, this.retryDelay * 2);
    }
  }

  dispose() {
    if (selected === this) select(null);
    this.disposed = true;
    this.disconnect();
    this.observer.disconnect();
    this.element.remove();
  }
}

async function refresh() {
  try {
    const data = await request(
      `api/team/${encodeURIComponent(team)}/terminals`,
    );
    if (stopped) return;
    const keys = new Set(data.members.map((member) => member.key));
    for (const [key, card] of cards) {
      if (!keys.has(key)) {
        card.dispose();
        cards.delete(key);
      }
    }
    for (const member of data.members) {
      if (cards.has(member.key)) cards.get(member.key).update(member);
      else cards.set(member.key, new AgentTerminal(member));
    }
    const available = data.members.filter((member) => member.available).length;
    summary.textContent = `${available} of ${data.members.length} agents online`;
    pageStatus.textContent = data.members.length
      ? ""
      : "This team has no agents yet.";
  } catch (error) {
    if (!stopped) pageStatus.textContent = error.message;
  }
  if (!stopped) refreshTimer = setTimeout(refresh, 5000);
}

window.addEventListener("pagehide", () => {
  stopped = true;
  clearTimeout(refreshTimer);
  for (const card of cards.values()) card.dispose();
  cards.clear();
});

try {
  if (!team)
    throw new Error("Select a team from the ClawTeam dashboard first.");
  await init();
  await refresh();
} catch (error) {
  summary.textContent = "Unable to open terminals";
  pageStatus.textContent = error.message;
}
