"""Exercise multiline notification delivery against a real raw-mode tmux pane."""

import os
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from clawteam.spawn.tmux_backend import _inject_prompt_via_buffer


@pytest.fixture
def raw_pane(tmp_path, monkeypatch):
    if os.name != "posix" or not shutil.which("tmux"):
        pytest.skip("Requires tmux and POSIX terminals")
    socket_dir = tmp_path / "socket"
    socket_dir.mkdir(mode=0o700)
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_dir))
    monkeypatch.delenv("TMUX", raising=False)
    received = tmp_path / "received.bin"
    script = (
        "import os,sys,tty; tty.setraw(0); "
        "out=open(sys.argv[1],'ab',buffering=0); "
        "os.write(1,b'\\x1b[?2004hREADY');\n"
        "while True: out.write(os.read(0,65536))"
    )
    command = shlex.join([sys.executable, "-u", "-c", script, str(received)])
    target = "injection-test:receiver"
    subprocess.run(
        [
            "tmux",
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            "injection-test",
            "-n",
            "receiver",
            command,
        ],
        check=True,
    )
    for _ in range(50):
        screen = subprocess.check_output(["tmux", "capture-pane", "-p", "-t", target])
        if b"READY" in screen:
            break
        time.sleep(0.02)
    else:
        pytest.fail("Raw terminal did not become ready")
    yield target, received
    subprocess.run(["tmux", "kill-server"], capture_output=True, check=False)


def test_multiline_notification_arrives_as_one_bracketed_paste(raw_pane):
    target, received = raw_pane
    prompt = "<clawteam_notification>\n<summary>完整通知</summary>\n</clawteam_notification>"
    _inject_prompt_via_buffer(target, "leader", prompt)
    data = received.read_bytes()
    expected = b"\x1b[200~" + prompt.encode("utf-8") + b"\x1b[201~"
    assert data.startswith(expected), repr(data)
    assert data.count(prompt.encode("utf-8")) == 1


def test_concurrent_messages_to_one_pane_do_not_interleave(raw_pane):
    target, received = raw_pane
    messages = ["first\nnotification", "second\nnotification"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_inject_prompt_via_buffer, target, "leader", m) for m in messages]
        for future in futures:
            future.result(timeout=5)
    blocks = [b"\x1b[200~" + m.encode() + b"\x1b[201~\r\r" for m in messages]
    assert received.read_bytes() in {blocks[0] + blocks[1], blocks[1] + blocks[0]}


def test_same_named_agents_do_not_share_paste_buffer(raw_pane, monkeypatch):
    target, received = raw_pane
    other_file = received.with_name("other.bin")
    script = (
        "import os,sys,tty; tty.setraw(0); out=open(sys.argv[1],'ab',buffering=0); "
        "os.write(1,b'\\x1b[?2004hREADY');\nwhile True: out.write(os.read(0,65536))"
    )
    subprocess.run(
        [
            "tmux",
            "new-window",
            "-t",
            "injection-test",
            "-n",
            "other",
            shlex.join([sys.executable, "-u", "-c", script, str(other_file)]),
        ],
        check=True,
    )
    for _ in range(50):
        if b"READY" in subprocess.check_output(
            ["tmux", "capture-pane", "-p", "-t", "injection-test:other"]
        ):
            break
        time.sleep(0.02)
    real_run = subprocess.run
    barrier = Barrier(2)

    def synchronized_load(args, **kwargs):
        result = real_run(args, **kwargs)
        if args[:2] == ["tmux", "load-buffer"]:
            barrier.wait(timeout=3)
        return result

    monkeypatch.setattr("clawteam.spawn.tmux_backend.subprocess.run", synchronized_load)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_inject_prompt_via_buffer, target, "leader", "team A\nmessage")
        second = pool.submit(
            _inject_prompt_via_buffer, "injection-test:other", "leader", "team B\nmessage"
        )
        first.result(timeout=5)
        second.result(timeout=5)
    assert received.read_bytes() == b"\x1b[200~team A\nmessage\x1b[201~\r\r"
    assert other_file.read_bytes() == b"\x1b[200~team B\nmessage\x1b[201~\r\r"
