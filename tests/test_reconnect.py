"""A dropped connection must not kill the client.

The client reaches the server through a TCP proxy the test can cut, the way
Wi-Fi drops on the ESP32 target. A short cut must resume the same NiceGUI
client and replay what the server sent meanwhile; a cut longer than the page's
reconnect_timeout must re-bootstrap, the way a browser reloads.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from nicegui_wire.sniffer import Sniffer

APP = """
from nicegui import app, ui
labels = []
@app.get('/poke')
def poke():
    for label in labels:
        label.text = 'poked'
    return len(labels)
@ui.page('/')  # default reconnect_timeout: 3 s
@ui.page('/short', reconnect_timeout=1.0)
def index():
    labels.append(ui.label('fresh'))
    count = ui.label('count = 0')
    def bump():
        count.text = 'count = %d' % (int(count.text.split()[-1]) + 1)
    ui.button('Inc', on_click=bump)
ui.run(host='127.0.0.1', port={port}, show=False, reload=False)
"""


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_port(tmp_path_factory):
    port = _free_port()
    launcher = tmp_path_factory.mktemp("ngw") / "reconnect_app.py"
    launcher.write_text(APP.replace("{port}", str(port)))
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("PYTEST_") or k.startswith("NICEGUI_"))}
    proc = subprocess.Popen([sys.executable, str(launcher)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.5)
            break
        except Exception:
            time.sleep(0.25)
    else:
        proc.kill()
        raise RuntimeError("reconnect test server did not start")
    yield port
    proc.terminate()
    proc.wait(timeout=5)


class CuttableProxy:
    """Forward TCP to the server; ``cut()`` drops every connection and refuses new ones."""

    def __init__(self, target_port: int) -> None:
        self.target_port = target_port
        self.up = True
        self.writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _accept(self, reader, writer) -> None:
        if not self.up:
            writer.close()
            return
        up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self.target_port)
        self.writers |= {writer, up_writer}

        async def pipe(src, dst):
            with contextlib.suppress(Exception):
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            dst.close()

        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))

    def cut(self) -> None:
        self.up = False
        for w in self.writers:
            w.transport.abort()
        self.writers.clear()

    def close(self) -> None:
        self.cut()
        self.server.close()


def _text(s: Sniffer, prefix: str) -> str | None:
    return next((n.text for n in s.tree.nodes.values() if (n.text or "").startswith(prefix)), None)


async def _until(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def _click(s: Sniffer) -> None:
    btn = next(nid for nid, n in s.tree.nodes.items() if n.tag == "q-btn")
    await s.fire(btn, "click")


async def _connect(server_port: int, path: str = "/"):
    proxy = CuttableProxy(server_port)
    port = await proxy.start()
    s = Sniffer(f"http://127.0.0.1:{port}{path}", verbose=False)
    await s.client.connect()
    s.tree.ingest_initial(s.client.bootstrap.elements)
    return proxy, s


async def test_short_drop_resumes_and_replays_missed_updates(server_port):
    proxy, s = await _connect(server_port)
    try:
        client_id = s.client.bootstrap.client_id
        proxy.cut()
        await asyncio.sleep(0.2)
        # Sent while the socket is down: only the server's message history has it.
        await asyncio.to_thread(urllib.request.urlopen, f"http://127.0.0.1:{server_port}/poke")
        await _click(s)  # a press while the link is down is held, as the browser does
        proxy.up = True
        assert await _until(lambda: _text(s, "poked") is not None), "missed update never replayed"
        assert s.client.bootstrap.client_id == client_id, "resumed as a new client"
        assert await _until(lambda: _text(s, "count = 1") is not None), "press during the drop was lost"
        await _click(s)
        assert await _until(lambda: _text(s, "count = 2") is not None), "events lost after resume"
    finally:
        proxy.close()
        await s.client.disconnect()


async def test_long_drop_rebootstraps_like_a_browser_reload(server_port):
    proxy, s = await _connect(server_port, "/short")
    try:
        client_id = s.client.bootstrap.client_id
        await _click(s)
        assert await _until(lambda: _text(s, "count = 1") is not None)
        proxy.cut()
        await _click(s)  # meant for the old page: must not land on the new one
        await asyncio.sleep(2.5)  # past reconnect_timeout=1.0: the server deletes the client
        proxy.up = True
        assert await _until(lambda: s.client.bootstrap.client_id != client_id), "never re-bootstrapped"
        # The new page starts over, and the old tree must not linger.
        assert await _until(lambda: _text(s, "count = 0") is not None)
        assert _text(s, "count = 1") is None
        await asyncio.sleep(1.0)
        assert _text(s, "count = 0") is not None, "a press from the old page reached the new one"
        await _click(s)
        assert await _until(lambda: _text(s, "count = 1") is not None), "events lost after re-bootstrap"
    finally:
        proxy.close()
        await s.client.disconnect()
