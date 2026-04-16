# NiceGUI wire protocol — reference for `nicegui-wire`

Observed in NiceGUI **3.10.0** on Python 3.14. Source citations are against a
stock install at `~/.venv/lib/python3.14/site-packages/nicegui/`.

This document is a working reference, not spec: it describes what the server
actually does, not what it promises to keep doing.

## High-level shape

A NiceGUI page is a two-part protocol:

1. **HTTP GET `/`** returns a skeleton HTML whose `<body>` ends with a
   `<script type="module">` that carries the *entire initial element tree*
   plus the info needed to open a WebSocket.
2. **Socket.IO (WebSocket transport)** on `/_nicegui_ws/socket.io/` delivers
   every later element update + every client-side interaction.

No polling is required after upgrade; the wire is push/pull over a single WS.

## 1. HTML bootstrap

### 1.1 Template

See `nicegui/templates/index.html`. The critical block at the end of
`<body>`:

```html
<script type="module">
  import DOMPurify from "dompurify";
  Element.prototype.setHTML = function (html) { ... };

  const app = createApp(parseElements(String.raw`{{ elements | safe }}`), {
    version: "{{ version }}",
    prefix: "{{ prefix | safe }}",
    query: {{ socket_io_js_query_params | safe }},
    extraHeaders: {{ socket_io_js_extra_headers | safe }},
    transports: {{ socket_io_js_transports | safe }},
  });
  ...
  app.mount("#app");
</script>
```

`{{ elements | safe }}` is produced by `Client.build_response` (`nicegui/client.py:156`):

```python
elements = json.dumps({
    id: element._to_dict() for id, element in self.elements.items()
})
# ...
context['elements'] = elements.translate(HTML_ESCAPE_TABLE)
```

The `HTML_ESCAPE_TABLE` escapes five characters so the JSON is safe inside a
`String.raw`` ` `` `` template literal:

| char  | escape   |
| ----- | -------- |
| `&`   | `&amp;`  |
| `<`   | `&lt;`   |
| `>`   | `&gt;`   |
| `` ` `` | `&#96;` |
| `$`   | `&#36;`  |

Reversing this table is the *only* HTML decode needed to retrieve the JSON.

### 1.2 Config block

`query`, `extraHeaders`, and `transports` are rendered by Jinja's `| safe`
filter with no `json.dumps`. Jinja calls `str()` on them, producing
**Python literal syntax** (single-quoted strings, capitalised booleans,
`None`, etc) — not JSON. Example from the wild:

```js
query: {'client_id': 'd27f08eb-9f20-48de-9d53-7e4bf20c0d9f', 'next_message_id': 0, 'implicit_handshake': True},
extraHeaders: {},
transports: ['websocket', 'polling'],
```

JavaScript accepts this as an object literal. A Python client must parse it
with `ast.literal_eval`, not `json.loads`. The reference implementation is
`nicegui_wire/html_parser.py::_safe_literal`.

### 1.3 Element dict shape

`Element._to_dict()` (in `nicegui/element.py`) produces, approximately:

```json
{
  "id": 7,
  "tag": "q-btn",
  "class": ["nicegui-button"],
  "props": {"color": "primary", "label": "Increment"},
  "text": null,
  "style": "",
  "component": null,
  "libraries": [],
  "exposed_libraries": [],
  "events": [
    {
      "listener_id": "b4764579-05b4-4e7e-ab78-3f92e88cde5c",
      "type": "click",
      "specials": [],
      "modifiers": [],
      "keys": [],
      "args": [],
      "throttle": 0.0,
      "leading_events": true,
      "trailing_events": true,
      "js_handler": null
    }
  ],
  "children": [8, 9]
}
```

Two things to note:

- **`children` is a flat integer list**, not a nested tree.
- **`listener_id` is the key**: the server keys event handlers by this UUID,
  not by `type`. Sending a `click` event without the matching `listener_id`
  produces a `KeyError: 'listener_id'` server-side.

## 2. Socket.IO connection

Mounted in `nicegui/nicegui.py:53`:

```python
sio_app = SocketIoApp(socketio_server=sio, socketio_path='/socket.io')
app.mount('/_nicegui_ws/', sio_app)
```

So the client-visible path is `/_nicegui_ws/socket.io/`.

### 2.1 Query string

The server's `_on_disconnect` handler reads `client_id` out of the raw
query string (`nicegui/nicegui.py:222`):

```python
query_bytes = sio.get_environ(sid)['asgi.scope']['query_string']
query = urllib.parse.parse_qs(query_bytes.decode())
client_id = query['client_id'][0]
```

So **these params MUST be in the URL**, not an auth dict:

- `client_id` — UUID from the bootstrap's `query.client_id`.
- `tab_id` — fresh UUID per tab. Acts as the session key; used for
  `app.storage.tab`.
- `document_id` — UUID per browser "document" (i.e. per page load, shared
  across reconnects).
- `next_message_id` — integer; tells the server which outbox messages to
  replay on reconnect. For a fresh connection, 0.

### 2.2 Transport

Polling works for the initial handshake and for browsers that don't
upgrade, but **polling transport triggers a server-side `KeyError` on our
explicit-disconnect path** (the `_on_disconnect` handler expects the query
string to be present on every sid, which is only reliable after the
WebSocket upgrade). `nicegui-wire` therefore uses `transports=['websocket']`
and skips polling entirely. See `sio_client.py::connect`.

### 2.3 Handshake

There are two paths. The server accepts both:

**A. Implicit** — if the connection query has `implicit_handshake=true`, the
server does the handshake inside `_on_connect` using the connection's query
string itself. This requires `tab_id` + `document_id` to be in the query.

**B. Explicit** (nicegui-wire uses this) — client emits a `handshake` event
payload:

```json
{
  "client_id": "<uuid>",
  "tab_id": "<uuid>",
  "document_id": "<uuid>",
  "next_message_id": 0
}
```

Server handler at `nicegui/nicegui.py:199`:

```python
@sio.on('handshake')
async def _on_handshake(sid, data):
    client = Client.instances.get(data['client_id'])
    if not client: return False
    ...
    client.handle_handshake(sid, data['document_id'],
                            int(data['next_message_id']) if 'next_message_id' in data else None)
    return True
```

A successful handshake produces no ack message; the socket simply sits
ready. If there are queued outbox messages (e.g. after reconnect), they
arrive immediately.

## 3. Server → client events

Enumerated from `nicegui/outbox.py` + various `client.outbox.enqueue_message(...)` call sites:

### 3.1 `update`

Core channel — element tree deltas. `outbox.py:104`:

```python
data = {
    element_id: None if element is deleted else element._to_dict()
    for element_id, element in self.updates.items()
}
...
coros.append(self._emit((client.id, 'update', data)))
```

Payload:
```json
{
  "<element_id>": {...element dict...} | null,
  "_id": <message_id>
}
```

`null` means delete. `_id` is a monotonic per-client message ID used for
`ack` and resync after reconnect.

### 3.2 `load_js_components`

`outbox.py:115`:

```json
{"components": [{"key": "...", "tag": "..."}, ...], "_id": <n>}
```

Tells the browser to fetch JS bundles. A wire client can ignore this
unless it intends to replicate JS components (out of scope for
`nicegui-wire`).

### 3.3 Other, per `enqueue_message` call sites

| event              | payload shape                                            | where fired |
| ------------------ | -------------------------------------------------------- | ----------- |
| `run_javascript`   | `{code, request_id?, _id}`                               | `client.py:247`, `client.py:250` |
| `notify`           | `{message, type, position, closeBtn, multiLine, _id}`    | `functions/notify.py` |
| `open`             | `{path, new_tab, _id}`                                   | `client.py:259` |
| `download`         | `{src, filename, media_type, _id}`                       | `client.py:263` |

`nicegui-wire` surfaces all of these as named events on `WireClient.on_message`.

## 4. Client → server events

### 4.1 `handshake`

See §2.3.

### 4.2 `event`

User-interaction. `nicegui/nicegui.py:228`:

```python
@sio.on('event')
def _on_event(_: str, msg: dict) -> None:
    client = Client.instances.get(msg['client_id'])
    ...
    client.handle_event(msg)
```

Then `client.handle_event` (`client.py:342`):

```python
sender = self.elements.get(msg['id'])
if sender is not None and not sender.is_ignoring_events:
    msg['args'] = [None if arg is None else json.loads(arg) for arg in msg.get('args', [])]
    if len(msg['args']) == 1:
        msg['args'] = msg['args'][0]
    sender._handle_event(msg)
```

And `Element._handle_event` looks up the listener by **`listener_id`**, not
event type:

```python
listener = self._event_listeners[msg['listener_id']]
```

Required payload:

```json
{
  "client_id": "<uuid>",
  "id": <element_id>,
  "listener_id": "<uuid from elements[id].events[*].listener_id>",
  "type": "click" | "update:value" | "update:modelValue" | ...,
  "args": []                   // JSON-encoded strings; one per listener arg
}
```

Note `args` is a list of **JSON-encoded strings**, not arbitrary JSON
values. For a single-argument event, pass `[json.dumps(value)]`.

### 4.3 `javascript_response`

Response to a server-initiated `run_javascript`. Payload:

```json
{"client_id": "<uuid>", "request_id": "<uuid>", "result": <any>}
```

### 4.4 `ack`

Acknowledge all server `update` messages up to (but not including)
`next_message_id`. Used for outbox pruning:

```json
{"client_id": "<uuid>", "next_message_id": <int>}
```

### 4.5 `log`

Forward a client-side log to the server:

```json
{"client_id": "<uuid>", "level": "debug"|"info"|"warning"|"error", "message": "..."}
```

## 5. Minimal reproducer

For a one-button "Hello World":

1. Browser: `GET /` → HTML with element tree `{0: q-layout, ..., 7: q-btn{click listener}}`.
2. Browser: connect WS `/_nicegui_ws/socket.io/?client_id=...&tab_id=...&document_id=...&next_message_id=0&transport=websocket&EIO=4`.
3. Browser: emit `handshake {client_id, tab_id, document_id, next_message_id: 0}`.
4. Server: silence until something happens.
5. User clicks button → Browser emits `event {client_id, id: 7, listener_id: "<from step 1>", type: "click", args: []}`.
6. Server runs the click handler, which updates element 5's text to `"count = 1"`.
7. Server emits `update {"5": {"tag":"div","text":"count = 1"}, "_id": 0}`.
8. Browser patches the DOM.

`nicegui-wire` follows exactly this sequence — replace steps 1, 2, 3 and 8 with `html_parser` + `sio_client` + `tree`, and keep step 5 ≡ `Sniffer.fire`.

## 6. Version sensitivity

Traced against NiceGUI 3.10.0. Pre-v3 had a different socket mount path
(`/socket.io/`), different handshake payload (no `document_id`), and HTML
template (no `createApp` / `parseElements`, inline Vue component declarations).
This document does not cover v1/v2.
