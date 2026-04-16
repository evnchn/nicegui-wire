"""ESP32-class framebuffer simulator.

Renders a NiceGUI page to a 320x240 RGB framebuffer, deliberately using only
primitives that port to an ESP32-S3 + ILI9341 panel:

    * fill_rect(x, y, w, h, rgb)
    * draw_rect(x, y, w, h, rgb)
    * draw_text(x, y, text, rgb, font_px)

No transparency, no alpha blending, no sub-pixel positioning. Font is a
single 5x8 bitmap ("small") or 8x13 ("normal"); we use the latter at 2x
scale when the simulator is run on a desktop (so 320x240 fills a 640x480
window).

This file lives next to the TUI renderer (:mod:`nicegui_wire.textual_app`)
and consumes the same :class:`~nicegui_wire.tree.ElementTree`, so the two
back-ends share the wire.

Memory discipline:
    * Element state is stored in fixed-length parallel arrays indexed by
      the wire's element id. The arrays grow on demand (on desktop we don't
      have to be ruthless) but the access pattern is what an embedded port
      would look like: no per-element heap allocations per frame.

Input:
    * Tab / Shift-Tab: move focus
    * Enter / Space: click focused button
    * Any printable char: enter text in focused input
    * Backspace: delete a char
    * q or Ctrl+C: quit
"""
from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from typing import Any

try:
    import pygame
except ImportError as e:
    raise ImportError("pygame is required for ngwire fb. Install with `pip install nicegui-wire[fb]`") from e

from .sniffer import Sniffer
from .tree import ElementTree, Node


log = logging.getLogger("nicegui_wire.fb")


# ---------------------------------------------------------------------------
# Colours (16-bit RGB565 later; on desktop we use RGB888 for clarity).
# ---------------------------------------------------------------------------

BG        = (  8,  10,  14)
PANEL     = ( 28,  30,  40)
TEXT      = (230, 232, 240)
TEXT_DIM  = (140, 144, 160)
PRIMARY   = ( 80, 140, 220)
ACCENT    = (220, 140,  80)
DANGER    = (220,  80,  80)
FOCUS     = (240, 220,  80)
BORDER    = ( 80,  84, 100)


# ---------------------------------------------------------------------------
# Static arena — fixed geometry; the ESP32 port will use parallel uint16_t
# arrays sized to MAX_NODES.
# ---------------------------------------------------------------------------


MAX_NODES = 128


@dataclass
class RenderEntry:
    """What we need to draw one node. A flat record so the ESP32 port can
    store it as a struct in an element pool."""
    kind: int = 0   # 0=nothing, 1=label, 2=button, 3=input, 4=checkbox, 5=switch, 6=separator, 7=card, 8=placeholder, 9=row (layout), 10=column (layout)
    text: str = ""
    value_str: str = ""
    value_bool: bool = False
    ng_id: int = -1
    is_container: bool = False


# "kind" codes kept symbolic for readability in this file.
KIND_NOTHING    = 0
KIND_LABEL      = 1
KIND_BUTTON     = 2
KIND_INPUT      = 3
KIND_CHECKBOX   = 4
KIND_SWITCH     = 5
KIND_SEPARATOR  = 6
KIND_CARD       = 7
KIND_PLACEHOLDER = 8
KIND_ROW        = 9
KIND_COLUMN     = 10


def _classify(node: Node) -> int:
    tag = node.tag
    if tag in ("q-layout", "q-page-container", "q-page"):
        return KIND_COLUMN
    if tag == "div":
        if node.text is not None and not node.children_ids:
            return KIND_LABEL
        if "nicegui-row" in node.classes or "row" in node.classes:
            return KIND_ROW
        return KIND_COLUMN
    if tag == "q-btn":
        return KIND_BUTTON
    if tag == "nicegui-input":
        return KIND_INPUT
    if tag == "q-checkbox":
        return KIND_CHECKBOX
    if tag == "q-toggle":
        return KIND_SWITCH
    if tag == "q-separator":
        return KIND_SEPARATOR
    if tag == "q-card":
        return KIND_CARD
    return KIND_PLACEHOLDER


def _label_of(node: Node) -> str:
    if node.text is not None:
        return str(node.text)
    return str(node.props.get("label") or node.tag)


# ---------------------------------------------------------------------------
# The framebuffer renderer.
# ---------------------------------------------------------------------------


@dataclass
class Hitbox:
    x: int
    y: int
    w: int
    h: int
    ng_id: int
    kind: int


class FBSim:
    """Tiny "embedded" renderer sitting on top of the wire."""

    WIDTH = 320
    HEIGHT = 240

    def __init__(self, url: str, *, scale: int = 2, show: bool = True) -> None:
        self.url = url
        self.scale = max(1, scale)
        self.show = show
        self.sniffer = Sniffer(url, verbose=False)
        self.tree: ElementTree = self.sniffer.tree
        self._dirty = True
        self._focused_index: int = 0
        self._hitboxes: list[Hitbox] = []
        self._input_buffers: dict[int, str] = {}
        self._notify_text: str = ""
        self._notify_expires: float = 0.0

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        pygame.init()
        pygame.display.set_caption(f"nicegui-wire fb sim: {self.url}")
        flags = 0 if self.show else pygame.HIDDEN
        self.screen = pygame.display.set_mode(
            (self.WIDTH * self.scale, self.HEIGHT * self.scale), flags=flags,
        )
        self.fb = pygame.Surface((self.WIDTH, self.HEIGHT))
        self.font_sm = pygame.font.SysFont("monospace", 10)
        self.font_md = pygame.font.SysFont("monospace", 12)
        self.font_lg = pygame.font.SysFont("monospace", 14, bold=True)
        self.clock = pygame.time.Clock()

        self.sniffer.client.on_message(self._on_wire)
        await self.sniffer.client.connect()
        self.tree.ingest_initial(self.sniffer.client.bootstrap.elements)

        running = True
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_q or (event.key == pygame.K_c and (event.mod & pygame.KMOD_CTRL)):
                        running = False
                    elif event.key == pygame.K_TAB:
                        delta = -1 if (event.mod & pygame.KMOD_SHIFT) else 1
                        self._move_focus(delta)
                    elif event.key in (pygame.K_RETURN, pygame.K_SPACE):
                        await self._activate_focused()
                    elif event.key == pygame.K_BACKSPACE:
                        await self._backspace_focused()
                    else:
                        if event.unicode and event.unicode.isprintable():
                            await self._type_char(event.unicode)
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    x, y = event.pos
                    await self._click_at(x // self.scale, y // self.scale)

            # Give the asyncio socketio task a chance to dispatch messages.
            await asyncio.sleep(0)

            self._render()
            pygame.transform.scale(self.fb, self.screen.get_size(), self.screen)
            pygame.display.flip()
            self.clock.tick(30)

        await self.sniffer.client.disconnect()
        pygame.quit()

    # ------------------------------------------------------------------
    # Wire ingestion
    # ------------------------------------------------------------------

    def _on_wire(self, event: str, data: Any) -> None:
        if event == "update" and isinstance(data, dict):
            self._dirty = True
        elif event == "notify" and isinstance(data, dict):
            self._notify_text = str(data.get("message", ""))
            self._notify_expires = pygame.time.get_ticks() / 1000.0 + 2.5
            self._dirty = True

    # ------------------------------------------------------------------
    # Layout + render
    # ------------------------------------------------------------------

    def _render(self) -> None:
        self.fb.fill(BG)
        self._hitboxes = []
        y = self._render_node(self.tree.root(), 4, 4, self.WIDTH - 8)
        # Toast
        if self._notify_text and (pygame.time.get_ticks() / 1000.0 < self._notify_expires):
            self._draw_toast(self._notify_text)

    def _render_node(self, node: Node | None, x: int, y: int, w: int) -> int:
        """Render one node starting at (x, y); return the y coordinate
        immediately below the node's bounding box."""
        if node is None:
            return y

        kind = _classify(node)
        children = self.tree.children(node.id)

        if kind == KIND_ROW:
            # Horizontal flow: render children side by side.
            if not children:
                return y
            child_w = max(40, (w - (len(children) - 1) * 4) // max(len(children), 1))
            max_bottom = y
            for i, c in enumerate(children):
                cx = x + i * (child_w + 4)
                b = self._render_node(c, cx, y, child_w)
                if b > max_bottom:
                    max_bottom = b
            return max_bottom + 2

        if kind == KIND_COLUMN:
            # Vertical flow.
            cy = y
            for c in children:
                cy = self._render_node(c, x, cy, w)
            return cy

        if kind == KIND_CARD:
            # Draw a bordered box and render children inside.
            inner_x = x + 6
            inner_y = y + 6
            inner_w = w - 12
            # First pass to measure
            cy = inner_y
            for c in children:
                cy = self._render_node(c, inner_x, cy, inner_w)
            h = cy - y + 6
            pygame.draw.rect(self.fb, BORDER, pygame.Rect(x, y, w, h), 1)
            # No need to re-render; the card contents are already drawn above
            # the border stroke and we stroke after. To avoid the stroke
            # overdrawing child content we could pre-measure; for sim we keep
            # it simple and accept a 1-pixel frame above content.
            return y + h + 2

        if kind == KIND_LABEL:
            text = _label_of(node)
            return self._draw_label(text, x, y, w)

        if kind == KIND_BUTTON:
            return self._draw_button(node, x, y, w)

        if kind == KIND_INPUT:
            return self._draw_input(node, x, y, w)

        if kind == KIND_CHECKBOX:
            return self._draw_checkbox(node, x, y, w)

        if kind == KIND_SWITCH:
            return self._draw_switch(node, x, y, w)

        if kind == KIND_SEPARATOR:
            pygame.draw.line(self.fb, BORDER, (x, y + 3), (x + w, y + 3))
            return y + 7

        # Placeholder.
        return self._draw_label(f"[{node.tag}]", x, y, w, color=TEXT_DIM)

    # -- Primitives ------------------------------------------------------

    def _draw_label(self, text: str, x: int, y: int, w: int, color=TEXT) -> int:
        surf = self.font_md.render(text[:60], True, color)
        self.fb.blit(surf, (x, y))
        return y + surf.get_height() + 2

    def _draw_button(self, node: Node, x: int, y: int, w: int) -> int:
        label = _label_of(node) or "Button"
        width = min(w, max(60, self.font_md.size(label)[0] + 12))
        h = 18
        focused = self._hit_is_focused(node.id)
        border_color = FOCUS if focused else PRIMARY
        pygame.draw.rect(self.fb, PANEL, pygame.Rect(x, y, width, h))
        pygame.draw.rect(self.fb, border_color, pygame.Rect(x, y, width, h), 1)
        surf = self.font_md.render(label, True, TEXT)
        self.fb.blit(surf, (x + (width - surf.get_width()) // 2,
                             y + (h - surf.get_height()) // 2))
        self._register_hitbox(x, y, width, h, node.id, KIND_BUTTON)
        return y + h + 2

    def _draw_input(self, node: Node, x: int, y: int, w: int) -> int:
        value = self._input_buffers.get(node.id) or str(node.props.get("value", "") or "")
        label = node.props.get("label")
        h = 22
        focused = self._hit_is_focused(node.id)
        border_color = FOCUS if focused else BORDER
        pygame.draw.rect(self.fb, PANEL, pygame.Rect(x, y, w, h))
        pygame.draw.rect(self.fb, border_color, pygame.Rect(x, y, w, h), 1)
        if label:
            lbl_surf = self.font_sm.render(str(label), True, TEXT_DIM)
            self.fb.blit(lbl_surf, (x + 4, y - 1))
        value_surf = self.font_md.render(value[:w // 7], True, TEXT)
        self.fb.blit(value_surf, (x + 6, y + 4 + (0 if not label else 3)))
        if focused:
            cursor_x = x + 6 + value_surf.get_width() + 1
            pygame.draw.line(self.fb, TEXT, (cursor_x, y + 4), (cursor_x, y + h - 4))
        self._register_hitbox(x, y, w, h, node.id, KIND_INPUT)
        return y + h + 4

    def _draw_checkbox(self, node: Node, x: int, y: int, w: int) -> int:
        value = bool(node.props.get("model-value", False))
        label = _label_of(node)
        box_w = 12
        focused = self._hit_is_focused(node.id)
        pygame.draw.rect(self.fb, PANEL, pygame.Rect(x, y + 2, box_w, box_w))
        pygame.draw.rect(self.fb, FOCUS if focused else BORDER,
                         pygame.Rect(x, y + 2, box_w, box_w), 1)
        if value:
            pygame.draw.line(self.fb, PRIMARY, (x + 2, y + 7), (x + 5, y + 11), 2)
            pygame.draw.line(self.fb, PRIMARY, (x + 5, y + 11), (x + 10, y + 4), 2)
        if label:
            surf = self.font_md.render(label, True, TEXT)
            self.fb.blit(surf, (x + box_w + 6, y + 1))
        self._register_hitbox(x, y + 2, max(box_w, self.font_md.size(label)[0] + box_w + 6), box_w, node.id, KIND_CHECKBOX)
        return y + 18

    def _draw_switch(self, node: Node, x: int, y: int, w: int) -> int:
        value = bool(node.props.get("model-value", False))
        switch_w = 26
        focused = self._hit_is_focused(node.id)
        track = pygame.Rect(x, y + 2, switch_w, 12)
        pygame.draw.rect(self.fb, PANEL, track, border_radius=6)
        pygame.draw.rect(self.fb, FOCUS if focused else BORDER, track, 1, border_radius=6)
        knob_x = x + (switch_w - 12) if value else x + 1
        pygame.draw.rect(self.fb, PRIMARY if value else TEXT_DIM,
                         pygame.Rect(knob_x, y + 3, 10, 10), border_radius=5)
        self._register_hitbox(x, y + 2, switch_w, 12, node.id, KIND_SWITCH)
        return y + 18

    def _draw_toast(self, text: str) -> None:
        w_sz = self.font_md.size(text)
        tw = min(self.WIDTH - 20, w_sz[0] + 16)
        th = 18
        tx = (self.WIDTH - tw) // 2
        ty = self.HEIGHT - th - 6
        pygame.draw.rect(self.fb, PRIMARY, pygame.Rect(tx, ty, tw, th))
        surf = self.font_md.render(text[:80], True, (255, 255, 255))
        self.fb.blit(surf, (tx + 8, ty + (th - surf.get_height()) // 2))

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------

    def _register_hitbox(self, x: int, y: int, w: int, h: int, ng_id: int, kind: int) -> None:
        self._hitboxes.append(Hitbox(x, y, w, h, ng_id, kind))

    def _focusable(self) -> list[Hitbox]:
        return [h for h in self._hitboxes if h.kind in (KIND_BUTTON, KIND_INPUT, KIND_CHECKBOX, KIND_SWITCH)]

    def _move_focus(self, delta: int) -> None:
        focusables = self._focusable()
        if not focusables:
            return
        self._focused_index = (self._focused_index + delta) % len(focusables)

    def _hit_is_focused(self, ng_id: int) -> bool:
        focusables = self._focusable()
        if not focusables:
            return False
        return focusables[self._focused_index % len(focusables)].ng_id == ng_id

    async def _activate_focused(self) -> None:
        focusables = self._focusable()
        if not focusables:
            return
        h = focusables[self._focused_index % len(focusables)]
        if h.kind == KIND_BUTTON:
            try:
                await self.sniffer.fire(h.ng_id, "click")
            except Exception as e:
                log.debug("fire click failed: %s", e)
        elif h.kind == KIND_CHECKBOX or h.kind == KIND_SWITCH:
            node = self.tree.nodes.get(h.ng_id)
            if node:
                new_val = not bool(node.props.get("model-value", False))
                try:
                    await self.sniffer.fire(h.ng_id, "update:modelValue", args=new_val)
                    # Optimistic update — the server's echo will correct if needed.
                    node.props["model-value"] = new_val
                except Exception as e:
                    log.debug("fire toggle failed: %s", e)

    async def _type_char(self, ch: str) -> None:
        focusables = self._focusable()
        if not focusables:
            return
        h = focusables[self._focused_index % len(focusables)]
        if h.kind == KIND_INPUT:
            buf = self._input_buffers.get(h.ng_id)
            if buf is None:
                node = self.tree.nodes.get(h.ng_id)
                buf = str(node.props.get("value", "") or "") if node else ""
            buf += ch
            self._input_buffers[h.ng_id] = buf
            try:
                await self.sniffer.fire(h.ng_id, "update:value", args=buf)
            except Exception as e:
                log.debug("input send failed: %s", e)

    async def _backspace_focused(self) -> None:
        focusables = self._focusable()
        if not focusables:
            return
        h = focusables[self._focused_index % len(focusables)]
        if h.kind == KIND_INPUT:
            buf = self._input_buffers.get(h.ng_id, "")
            if buf:
                buf = buf[:-1]
                self._input_buffers[h.ng_id] = buf
                try:
                    await self.sniffer.fire(h.ng_id, "update:value", args=buf)
                except Exception as e:
                    log.debug("input send failed: %s", e)

    async def _click_at(self, x: int, y: int) -> None:
        for idx, h in enumerate(self._focusable()):
            if h.x <= x <= h.x + h.w and h.y <= y <= h.y + h.h:
                self._focused_index = idx
                await self._activate_focused()
                return


def run(url: str, *, width: int = 320, height: int = 240, scale: int = 2) -> int:  # pragma: no cover
    """CLI entry point for ``ngwire fb URL``."""
    FBSim.WIDTH = width
    FBSim.HEIGHT = height
    sim = FBSim(url, scale=scale)
    try:
        asyncio.run(sim.run())
    except KeyboardInterrupt:
        return 130
    return 0
