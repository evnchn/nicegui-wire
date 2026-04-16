"""A NiceGUI-aware Socket.IO client.

Connects to a running NiceGUI server, performs the explicit ``handshake``,
and dispatches every inbound server event to a caller-supplied callback.

Protocol notes (traced from nicegui v3 source, see PROTOCOL.md):

    Socket.IO mount:         /_nicegui_ws/socket.io
    Handshake query:         client_id=<uuid>, next_message_id=<int>
    Implicit handshake:      if query also has implicit_handshake=true the
                             server handshakes on connect using the query
                             params themselves. Otherwise the client must
                             emit a 'handshake' event.

    Server-sent events (enumerated in nicegui/nicegui.py and outbox.py):

        update              {element_id: element_dict|null, _id: int}
        load_js_components  {components: [{key, tag}], _id: int}
        run_javascript      {code, request_id?, _id: int}
        run_method          {_id, ...}
        notify              {message, type, ..., _id}
        open                {path, new_tab, _id}
        download            {src, filename, media_type, _id}

    Client-sent events:

        handshake           {client_id, tab_id, document_id, next_message_id?, old_tab_id?}
        event               {client_id, id, type, args}
        javascript_response {client_id, request_id, result}
        ack                 {client_id, next_message_id}
        log                 {client_id, level, message}
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse, urlunparse

import aiohttp
import socketio

from .html_parser import BootstrapConfig, parse_bootstrap


log = logging.getLogger("nicegui_wire.sio")

# Every server→client event NiceGUI is known to emit. We register handlers
# for each so socketio's AsyncClient doesn't just drop them silently.
KNOWN_SERVER_EVENTS = (
    "update",
    "load_js_components",
    "run_javascript",
    "run_method",
    "notify",
    "open",
    "download",
    "reload_elements",
    # Anything else lands in the catch-all handler.
)


class WireClient:
    """Connect to a NiceGUI page and surface every wire message.

    Usage::

        async with WireClient("http://localhost:8080/") as c:
            c.on_message(lambda evt, data: print(evt, data))
            await c.run_until_disconnect()

    The client parses the initial HTML for bootstrap info, opens a Socket.IO
    connection, performs an explicit ``handshake`` (we always use the
    explicit form for portability; implicit handshake requires query-string
    support in the Socket.IO library and is harder to verify).
    """

    def __init__(
        self,
        url: str,
        *,
        tab_id: str | None = None,
        document_id: str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.url = url.rstrip("/") + ("/" if not url.endswith("/") else "")
        self.tab_id = tab_id or str(uuid.uuid4())
        self.document_id = document_id or str(uuid.uuid4())
        self.log = logger or log

        self.bootstrap: BootstrapConfig | None = None
        self.sio: socketio.AsyncClient | None = None
        self._handlers: list[Callable[[str, Any], Any]] = []
        self._disconnect_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._next_message_id: int = 0
        self._owned_session = True

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "WireClient":
        await self.connect()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.disconnect()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_message(self, handler: Callable[[str, Any], Any]) -> None:
        """Register a callback ``handler(event_name, data)`` for every server-sent message.

        Handlers are called synchronously in the Socket.IO event loop. For
        long work, schedule a task inside the handler.
        """
        self._handlers.append(handler)

    async def connect(self) -> None:
        """Fetch the bootstrap page and open the Socket.IO connection."""
        self.bootstrap = await self._fetch_bootstrap()
        cid = self.bootstrap.client_id
        prefix = self.bootstrap.prefix or ""
        self._next_message_id = self.bootstrap.next_message_id

        self.sio = socketio.AsyncClient(
            reconnection=False,
            logger=False,
            engineio_logger=False,
        )
        self._wire_handlers()

        # Build the Socket.IO URL. NiceGUI mounts at /_nicegui_ws/.
        # NiceGUI's ``_on_disconnect`` reads ``client_id`` out of the raw
        # query string on the connection — it *requires* these params in the
        # URL, not in a handshake auth payload. So we build a URL with
        # ?client_id=...&tab_id=...&document_id=...&next_message_id=...
        base = urlparse(self.url)
        query_pairs = [
            ("client_id", cid),
            ("tab_id", self.tab_id),
            ("document_id", self.document_id),
            ("next_message_id", str(self._next_message_id)),
        ]
        # Forward any extra query params NiceGUI wants us to echo
        # (but never ``implicit_handshake`` — we always do explicit).
        for k, v in self.bootstrap.query.items():
            if k in ("client_id", "next_message_id", "implicit_handshake"):
                continue
            query_pairs.append((k, str(v)))
        from urllib.parse import urlencode
        sio_url = urlunparse((
            base.scheme, base.netloc, "", "",
            urlencode(query_pairs),
            "",
        ))
        socketio_path = f"{prefix}/_nicegui_ws/socket.io"

        self.log.debug(
            "connecting socket.io url=%s path=%s",
            sio_url, socketio_path,
        )
        # WebSocket-only. Polling triggers a NiceGUI server-side KeyError
        # on disconnect (``_on_disconnect`` reads ``client_id`` out of the
        # polling query string but the WebSocket-upgraded scope drops it),
        # which then tears the connection down prematurely. WebSocket-only
        # matches what the browser does after upgrade anyway.
        await self.sio.connect(
            sio_url,
            socketio_path=socketio_path,
            transports=["websocket"],
            wait=True,
        )

        # Explicit handshake.
        handshake_data = {
            "client_id": cid,
            "tab_id": self.tab_id,
            "document_id": self.document_id,
            "next_message_id": self._next_message_id,
        }
        self.log.debug("emitting handshake: %s", handshake_data)
        await self.sio.emit("handshake", handshake_data)
        # A successful NiceGUI handshake does not send back an ack. If the
        # page already caused outbox messages to queue (e.g. reconnecting
        # to a session that had pending updates) we'll see them within a
        # tick. Otherwise the socket just sits idle until the server has
        # something to say, which is fine.

    async def disconnect(self) -> None:
        if self.sio is not None and self.sio.connected:
            try:
                await self.sio.disconnect()
            except Exception:
                pass
        self._disconnect_event.set()

    async def run_until_disconnect(self) -> None:
        """Block until the connection closes."""
        await self._disconnect_event.wait()

    async def send_event(
        self,
        element_id: int,
        event_type: str,
        *,
        listener_id: str,
        args: Any = None,
    ) -> None:
        """Forward a user-interaction event back to the NiceGUI server.

        Server-side handler (``Element._handle_event``) keys listeners by
        ``listener_id`` — not by ``type`` — so callers MUST supply the
        listener_id discovered from the element's ``events`` list. In
        practice, use :meth:`send_element_event` which does the lookup.
        """
        assert self.sio is not None and self.bootstrap is not None
        import json as _json
        # ``args`` is transported as a list of JSON-encoded strings; the
        # server deserialises each argument separately (client.py:347).
        if args is None:
            args_list: list[str] = []
        elif isinstance(args, list):
            args_list = [_json.dumps(a) for a in args]
        else:
            args_list = [_json.dumps(args)]
        payload = {
            "client_id": self.bootstrap.client_id,
            "id": element_id,
            "listener_id": listener_id,
            "type": event_type,
            "args": args_list,
        }
        await self.sio.emit("event", payload)

    async def send_ack(self, next_message_id: int) -> None:
        """Acknowledge all server messages up to (and including) ``next_message_id - 1``."""
        assert self.sio is not None and self.bootstrap is not None
        await self.sio.emit("ack", {
            "client_id": self.bootstrap.client_id,
            "next_message_id": next_message_id,
        })

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _fetch_bootstrap(self) -> BootstrapConfig:
        async with aiohttp.ClientSession() as session:
            async with session.get(self.url) as resp:
                resp.raise_for_status()
                html = await resp.text()
        return parse_bootstrap(html)

    def _wire_handlers(self) -> None:
        """Register one handler per known event name plus a catch-all."""
        assert self.sio is not None
        for event_name in KNOWN_SERVER_EVENTS:
            def make(ev: str):
                async def handler(data):
                    self._dispatch(ev, data)
                return handler
            self.sio.on(event_name, make(event_name))

        @self.sio.on("connect")
        async def _on_connect():
            self.log.debug("sio connected sid=%s", self.sio.sid)

        @self.sio.on("disconnect")
        async def _on_disconnect():
            self.log.debug("sio disconnected")
            self._disconnect_event.set()

        # Final catch-all for anything we didn't enumerate (future NiceGUI versions).
        @self.sio.on("*")
        async def _catchall(event, data):
            self._dispatch(event, data)

    def _dispatch(self, event: str, data: Any) -> None:
        if not self._connected_event.is_set():
            self._connected_event.set()

        # Track message IDs so we can ACK.
        if isinstance(data, dict) and isinstance(data.get("_id"), int):
            self._next_message_id = max(self._next_message_id, data["_id"] + 1)

        for handler in self._handlers:
            try:
                result = handler(event, data)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:
                self.log.exception("handler error for event %s", event)
