# ESP32 port notes

`nicegui-wire`'s framebuffer renderer (`nicegui_wire/fb_sim.py`) was designed from the start as a simulator for an embedded target. The primitive contract is small on purpose:

| Primitive           | Desktop (pygame-ce)               | ESP32 equivalent                    |
| ------------------- | --------------------------------- | ----------------------------------- |
| `fill_rect(x,y,w,h,rgb)` | `pygame.draw.rect` (filled)  | LovyanGFX `fillRect(...)`           |
| `draw_rect(x,y,w,h,rgb)` | `pygame.draw.rect` (stroke)  | LovyanGFX `drawRect(...)`           |
| `draw_text(x,y,s,rgb,font)` | `font.render` + blit      | LovyanGFX `drawString(...)`         |
| Framebuffer flip     | `pygame.display.flip()`          | `tft.pushImage(buf)` or DMA         |

No alpha. No sub-pixel text. No arbitrary shapes. Everything is a box or a line or a monospace label. If you want a glass effect, you don't want this code.

## Hardware bill of materials (Taobao)

The author does not yet own the hardware. This is the intended acquisition list, vetted against what `fb_sim.py` actually needs:

| Item                          | Spec                                 | Rough ¥ (Taobao) |
| ----------------------------- | ------------------------------------ | ---------------- |
| ESP32-S3-DevKit-C (N16R8)     | 8 MB PSRAM, 16 MB flash, dual-core   | ¥45–70           |
| 2.4" ILI9341 SPI LCD, 320×240 | 16-bit parallel or SPI, resistive TS | ¥25–40           |
| EC11 rotary encoder + knob    | for focus / scrolling                | ¥5               |
| 3× tactile buttons            | confirm / back / menu                | ¥2 each          |
| Jumper wires + breadboard     |                                      | ¥20              |

**Why these choices:**

- **ESP32-S3, not C3/C6**: C3 is single-core RISC-V, no PSRAM on most dev kits. The S3 has PSRAM (needed to buffer the reconstructed element tree, especially for a 382-element page like `nicegui.io`), a second core for WiFi, and usable I/O pin count for SPI + buttons.
- **N16R8 variant**: 8 MB PSRAM + 16 MB flash. The 2 MB flash / 2 MB PSRAM variants can do it but are tight.
- **ILI9341 SPI**: cheapest panel with a reliable driver on all four of Arduino-ESP32, ESP-IDF, MicroPython, and Rust (`embedded-graphics`). 320×240 is exactly what the simulator renders natively.
- **EC11 encoder** for focus: matches the Tab / Shift-Tab keyboard semantics of the sim directly. One click per detent.

Planned pivot points:
- If you want a capacitive-touch 480×320 panel (ILI9488), the renderer's only assumption that doesn't hold is the fixed `WIDTH`/`HEIGHT`, which is already a class attribute.
- If you want an e-paper back-end, `fb_sim.py` already avoids per-frame re-render when nothing is dirty (`_dirty` flag, not yet wired everywhere) — extend that to a partial-refresh scheme.

## Firmware choice

Two realistic paths, in order of prototyping speed:

### Option A: MicroPython

- `uasyncio` gives you the exact event-loop shape the sim already uses.
- `umqtt`-style socket.io support is not great out of the box; you'll need to hand-write Engine.IO v4 polling / upgrade. See `nicegui_wire/sio_client.py` for the message schema you need to match.
- LCD: [`micropython-ili9341`](https://github.com/rdagger/micropython-ili9341) or [`lvgl_micropython`](https://github.com/lvgl-micropython/lvgl_micropython).
- Upside: iterate in Python. Downside: limited RAM on 4 MB PSRAM variants; JSON parsing of 382-element bootstrap will be tight.

### Option B: Arduino/ESP-IDF (C/C++)

- Use [`WebSockets`](https://github.com/Links2004/arduinoWebSockets) for the WS transport + hand-roll the Engine.IO v4 framing (it's a small state machine).
- Use `ArduinoJson` or `cJSON` for message decoding. Prefer streaming parse for the initial bootstrap — don't buffer the whole 30–80 KB HTML in RAM.
- LCD: `LovyanGFX` (same API on many panels).

**Recommendation for first ship:** Arduino-ESP32 + LovyanGFX + ArduinoJson. That stack has the fewest unknowns for the protocol translation.

## Memory budget (measured on the desktop sim)

On `hello.py` (14 elements):
- Element tree: ~3 KB (dicts + lists)
- Framebuffer (320×240 @ 24bpp): 230 KB
- Framebuffer (16bpp RGB565): 150 KB

The 16bpp LCD format is what ILI9341 wants natively. The S3 can keep the entire framebuffer in internal SRAM and still have headroom; no need to bounce through PSRAM.

On `nicegui.io` (382 elements):
- Element tree: ~180 KB of JSON
- That lives fine in PSRAM. Parsing it top-down streaming avoids a 2× transient.

## Protocol translation checklist

Before the ESP32 will speak to a NiceGUI server, you need to implement, in firmware:

1. HTTPS GET on `/` → strip HTML → extract `createApp(parseElements(String.raw`...`), {...});`
   - Locate the `createApp(...)` block (single `<script type="module">` containing it).
   - Unescape HTML entities (`&amp;` → `&`, `&lt;` → `<`, `&gt;` → `>`, `&#96;` → `` ` ``, `&#36;` → `$`).
   - Decode the element-tree JSON.
   - Parse the config block for `client_id` (Python-literal syntax — single quotes, capitalised booleans).
2. Socket.IO connection on `/_nicegui_ws/socket.io/?client_id=...&tab_id=...&document_id=...&next_message_id=0&transport=websocket&EIO=4`.
3. Engine.IO v4 state machine:
   - Open frame (`0{"sid":"...","upgrades":["websocket"],"pingInterval":4000,"pingTimeout":2000}`)
   - Connect `2{}` → expect `4{"sid":"..."}` at the Socket.IO layer
   - Pings as `2` → pongs as `3`
4. Socket.IO messages: emit `handshake` with `{client_id, tab_id, document_id, next_message_id}`.
5. On `update`: parse payload, patch element tree, mark screen dirty.
6. On interaction: emit `event` with `{client_id, id, listener_id, type, args}`. **`listener_id` is mandatory** — the server keys handlers by that UUID, not by event type. Look it up in the element's `events` array from the tree.

All of the above is done in `nicegui_wire/sio_client.py` + `nicegui_wire/sniffer.py` + `nicegui_wire/tree.py`. Those three files are the reference implementation a firmware port should mirror.
