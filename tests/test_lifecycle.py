"""Held events and socket cleanup across drops, with a fake Socket.IO transport.

These races need timing a real server cannot pin down, so a fake stands in for
socketio.AsyncClient: ``connected`` is the link, ``emit`` fails when it is down.
"""
from __future__ import annotations

import asyncio

import pytest
import socketio

from nicegui_wire import sio_client
from nicegui_wire.html_parser import BootstrapConfig
from nicegui_wire.sio_client import WireClient


class FakeSio:
    instances: list["FakeSio"] = []
    handshake_answer = True

    def __init__(self, *_args, **_kwargs) -> None:
        self.connected = False
        self.sid = "sid"
        self.sent: list[tuple[str, dict]] = []
        self.shutdowns = 0
        self.flaky = 0  # emits to fail as if the namespace dropped, even while connected
        FakeSio.instances.append(self)

    def on(self, _event, handler=None):
        return handler if handler is not None else (lambda f: f)

    async def connect(self, *_args, **_kwargs) -> None:
        self.connected = True

    async def call(self, *_args, **_kwargs):
        return self.handshake_answer

    async def emit(self, event: str, payload: dict) -> None:
        if not self.connected or self.flaky:
            self.flaky = max(0, self.flaky - 1)
            raise socketio.exceptions.BadNamespaceError("/ is not a connected namespace.")
        self.sent.append((event, payload))

    async def disconnect(self) -> None:
        self.connected = False

    async def shutdown(self) -> None:
        self.shutdowns += 1
        self.connected = False


def _bootstrap() -> BootstrapConfig:
    return BootstrapConfig(version="3", prefix="", query={"client_id": "c1", "next_message_id": 0},
                           extra_headers={}, transports=["websocket"], elements={})


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture
def client(monkeypatch) -> WireClient:
    FakeSio.instances.clear()
    monkeypatch.setattr(sio_client.socketio, "AsyncClient", FakeSio)
    c = WireClient("http://example.invalid/")

    async def fetch() -> BootstrapConfig:
        return _bootstrap()
    monkeypatch.setattr(c, "_fetch_bootstrap", fetch)
    return c


async def test_held_event_survives_a_second_drop(client):
    await client.connect()
    sio = client.sio
    sio.connected = False  # first drop
    client._ready.clear()
    await client._emit_when_ready("event", {"n": 1})
    await _settle()  # the held event is now waiting for the link

    sio.connected = True  # back up: the held event's waiter wakes...
    client._ready.set()
    sio.connected = False  # ...but the link drops again before it runs
    client._ready.clear()
    await _settle()

    sio.connected = True  # and comes back for good
    client._ready.set()
    await asyncio.sleep(0.3)
    assert sio.sent == [("event", {"n": 1})]


async def test_held_events_keep_their_order(client):
    await client.connect()
    sio = client.sio
    sio.connected = False
    client._ready.clear()
    await client._emit_when_ready("event", {"n": 1})
    await _settle()

    sio.connected = True
    sio.flaky = 1  # the held event's first try hits a namespace that is not back yet
    client._ready.set()
    await _settle()
    await client._emit_when_ready("event", {"n": 2})  # a fresh send must not overtake it
    await asyncio.sleep(0.3)
    assert [p["n"] for _, p in sio.sent] == [1, 2]


async def test_send_after_disconnect_is_rejected(client):
    await client.connect()
    await client.disconnect()
    with pytest.raises(ConnectionError):
        await client._emit_when_ready("event", {"n": 1})
    assert not client._pending


async def test_held_events_are_bounded(client):
    await client.connect()
    client.sio.connected = False
    client._ready.clear()
    for n in range(1000):
        await client._emit_when_ready("event", {"n": n})
    held = getattr(client, "_held", client._pending)
    assert len(held) == getattr(sio_client, "MAX_HELD_EVENTS", 100)
    await client.disconnect()


async def test_rejected_handshake_shuts_the_socket_down(client, monkeypatch):
    monkeypatch.setattr(FakeSio, "handshake_answer", False)
    with pytest.raises(ConnectionError):
        await client.connect()
    assert FakeSio.instances[-1].shutdowns == 1 and not FakeSio.instances[-1].connected


async def test_failed_rebootstrap_shuts_the_replacement_down(client, monkeypatch):
    await client.connect()
    client._handshaken = True
    monkeypatch.setattr(FakeSio, "handshake_answer", False)  # the replacement's handshake is rejected
    await client._rebootstrap()
    replacement = FakeSio.instances[-1]
    assert len(FakeSio.instances) == 2 and client._closed
    assert replacement.shutdowns == 1 and not replacement.connected
