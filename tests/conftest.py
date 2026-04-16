"""Shared test fixtures for nicegui-wire.

Spins up a local NiceGUI server in a subprocess for every test that needs
one (scope="session", a single instance is shared).
"""
from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def hello_app_port() -> int:
    # Pick a random free port so we don't collide with running demos.
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def hello_server(hello_app_port: int):
    """Launch examples/hello.py on a free port."""
    repo = Path(__file__).parent.parent
    example = repo / "examples" / "hello.py"
    env = os.environ.copy()
    env["NGW_PORT"] = str(hello_app_port)
    # Patch port via env var by rewriting example on the fly in a temp file
    # would be overkill; instead we let the fixture spawn on 8181 and pick
    # that port. See pytest parametrisation note below.
    # For simplicity we just use the stock port; the hello port fixture
    # returns 8181 always.
    yield f"http://127.0.0.1:8181/"


@pytest.fixture(scope="session", autouse=False)
def hello_url(tmp_path_factory) -> str:
    """Spawn hello.py as a subprocess on a throwaway port."""
    # Find free port
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    repo = Path(__file__).parent.parent
    # Write a one-off launcher that binds to our chosen port.
    launcher = tmp_path_factory.mktemp("ngw") / "launch.py"
    launcher.write_text(
        "import sys\n"
        "from nicegui import ui\n"
        "counter = {'n': 0}\n"
        "@ui.page('/')\n"
        "def index():\n"
        "    ui.label('wire test')\n"
        "    lbl = ui.label('count = 0')\n"
        "    def bump():\n"
        "        counter['n'] += 1\n"
        "        lbl.text = f\"count = {counter['n']}\"\n"
        "    ui.button('Inc', on_click=bump)\n"
        "    inp = ui.input('Name', value='')\n"
        "    ui.label().bind_text_from(inp, 'value', lambda v: f'you={v}')\n"
        f"ui.run(host='127.0.0.1', port={port}, show=False, reload=False)\n"
    )
    # Scrub pytest-injected env so NiceGUI doesn't take its is_pytest branch
    # (which demands NICEGUI_SCREEN_TEST_PORT).
    clean_env = {k: v for k, v in os.environ.items()
                 if not (k.startswith("PYTEST_") or k.startswith("NICEGUI_"))}
    proc = subprocess.Popen(
        [sys.executable, str(launcher)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=clean_env,
    )
    url = f"http://127.0.0.1:{port}/"
    # Wait until server answers.
    deadline = time.time() + 15
    import urllib.request
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5)
            break
        except Exception:
            time.sleep(0.25)
    else:
        proc.terminate()
        stdout = proc.stdout.read() if proc.stdout else b""
        raise RuntimeError(f"hello server didn't start: {stdout!r}")

    yield url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
