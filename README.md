# nicegui-wire

> [!CAUTION]
> **Vibe-coded, unreviewed.** This repo was built end-to-end in one autonomous overnight stretch by Claude Code (April 2026) and shipped here without a human code review. The code works against the bundled dev server (`examples/hello.py`) — handshake, tree reconstruction, round-trip interactivity all pass — but it has **never been pointed at `nicegui.io` itself**, and we wouldn't dare; that page is a monster for a prototype this thin. Use this for protocol exploration on small NiceGUI apps you control. Expect rough edges, leaky abstractions, and surprises. *Talk is cheap, show me the code.*

**A network-level NiceGUI client.** Point it at a running NiceGUI site, and it speaks the Socket.IO wire + parses the HTML bootstrap to reconstruct the live element tree in-memory — without ever importing NiceGUI.

Same element stream, two back-ends:

1. **`ngwire tui <url>`** — renders the site as a Textual TUI over SSH or in your terminal.
2. **`ngwire fb <url>`** — renders into a 320×240 framebuffer (pygame-ce simulator), sized and primitive-shaped for an ESP32-S3 + ILI9341 panel.

Unlike its sister project [`nicegui-tui`](https://github.com/evnchn/nicegui-tui), `nicegui-wire` never touches NiceGUI's Python objects. It talks to NiceGUI the way a browser does, so in principle any NiceGUI 3.x site you can reach over HTTP is a candidate — though so far we've only proven it against small dev apps (see CAUTION above).

## Status

Pre-alpha, overnight prototype. v0.0.1.

What works:
- Parse initial HTML bootstrap (element tree + config) for any NiceGUI 3.x page.
- Socket.IO (websocket-only) handshake + message dispatch.
- Apply `update` deltas to an in-memory element tree.
- Send events back (`click`, `update:value`, `update:modelValue`) — full round-trip interactivity.
- Textual TUI renderer with ~10 widget types.
- 320×240 framebuffer renderer (pygame-ce) with keyboard + mouse input.
- Proven against the bundled `examples/hello.py` dev server (counter, label, input round-trip clean). **Not tested against `nicegui.io` or other production sites.**

What's limited:
- Widget palette is a subset (same scope as nicegui-tui v0.0.1). Unknown tags render as `[unsupported: <tag>]`.
- No Vue component rendering (anything `q-*` that isn't in the factory just shows its tag name).
- Tree is rebuilt wholesale on every `update` — fine for weekend-hack correctness, bad for large pages.
- No cookies / session auth yet — sites that gate the page behind login won't work.

## Install

```bash
git clone https://github.com/evnchn/nicegui-wire
cd nicegui-wire
python -m venv .venv && source .venv/bin/activate
pip install -e '.[tui,fb,test]'
```

Needs Python 3.10+. Uses `pygame-ce` (not upstream `pygame`; the latter has a Python 3.14 circular-import bug in its font module).

## Try it

### 1. Against a self-hosted hello app

```bash
# Terminal 1
python examples/hello.py

# Terminal 2 — pick one:
ngwire sniff http://127.0.0.1:8181/             # dump wire as JSONL
ngwire tui   http://127.0.0.1:8181/             # Textual TUI
ngwire fb    http://127.0.0.1:8181/ --scale 2   # 320x240 framebuffer sim
```

### 2. Against your own NiceGUI app

```bash
ngwire sniff http://localhost:8080/ -o /tmp/sniff.jsonl
```

For the renderers, point them at any small NiceGUI app you control. **`nicegui.io` itself has not been tested and almost certainly does interesting things this prototype hasn't seen yet** — try at your own risk:

```bash
ngwire tui http://localhost:8080/
```

## Architecture

```
    ┌────────────────┐       HTTP GET /        ┌──────────────────┐
    │  NiceGUI site  │◀──────────────────────▶│  html_parser.py  │
    │  (any 3.x)     │  initial element tree  │ (bootstrap cfg)  │
    └────────────────┘                         └──────────────────┘
            ▲                                           │
            │ Socket.IO (websocket)                     ▼
            │ handshake / update / event         ┌────────────────┐
            └───────────────────────────────────▶│ sio_client.py  │
                                                 │ (WireClient)   │
                                                 └────────┬───────┘
                                                          │ updates
                                                          ▼
                                                 ┌────────────────┐
                                                 │   tree.py      │
                                                 │  ElementTree   │
                                                 └────────┬───────┘
                                                          │
                              ┌───────────────────────────┼───────────────────────────┐
                              ▼                           ▼                           ▼
                     ┌───────────────┐           ┌────────────────┐           ┌────────────────┐
                     │  sniffer.py   │           │ textual_app.py │           │   fb_sim.py    │
                     │  JSONL dump   │           │  Textual TUI   │           │ 320x240 RGB    │
                     └───────────────┘           └────────────────┘           └────────────────┘
```

Each back-end consumes the same `ElementTree` and maps node tags (`q-btn`, `nicegui-input`, `div`, …) to its own widget primitives.

See [`PROTOCOL.md`](./PROTOCOL.md) for a tour of the wire.

## Why both a TUI and a framebuffer sim?

Two target profiles:

- **TUI**: for SRE/devops consumption of a dashboard over SSH. Same rationale as `nicegui-tui`.
- **Framebuffer (320×240)**: provoking a different ceiling. If a NiceGUI dashboard can render on a display this small, with input from a knob and three buttons, the backend contract is cheap enough to run on an ESP32-S3. See [`ESP32.md`](./ESP32.md) for the hardware bill of materials and port notes.

## License

MIT.
