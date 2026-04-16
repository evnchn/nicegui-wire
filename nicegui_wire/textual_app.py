"""Textual renderer over the NiceGUI wire.

Unlike ``nicegui-tui`` (the sister project), this renderer never imports
NiceGUI. It consumes the :class:`~nicegui_wire.tree.ElementTree` that
``WireClient`` reconstructs from server-sent ``update`` messages, and
renders those raw tag/props dicts as Textual widgets.

Because we speak the wire, the same code works against any NiceGUI site
— your own, nicegui.io's docs, anything reachable over HTTP + Socket.IO.

Supported tags (v0.0.1):

    q-layout / q-page-container / q-page   invisible passthrough
    div                                    container or label (by class / text)
    q-btn                                  Button
    nicegui-input                          Input
    q-checkbox                             Checkbox
    q-toggle                               Switch
    q-select                               Select (labels from props.options)
    q-separator                            Rule
    q-card                                 Vertical border
    q-markdown / markdown-element          Markdown
    q-tabs / q-tab-panels                  flat fallback to Vertical

Everything else renders as a muted ``[unsupported: <tag>]`` placeholder.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widget import Widget
    from textual.widgets import (
        Button, Checkbox, Input, Label, Markdown, Rule, Select, Static, Switch,
    )
except ImportError as e:
    raise ImportError(
        "textual is required for ngwire tui. Install with `pip install nicegui-wire[tui]`"
    ) from e

from .sniffer import Sniffer
from .tree import Node


log = logging.getLogger("nicegui_wire.tui")


# ---------------------------------------------------------------------------
# Widget factories
# ---------------------------------------------------------------------------


# Classes added by NiceGUI to its div wrappers; used to disambiguate generic
# divs from layout containers.
_CONTAINER_CLASSES = ("nicegui-row", "nicegui-column", "nicegui-grid")


def _is_row(node: Node) -> bool:
    return "nicegui-row" in node.classes or "row" in node.classes


def _is_column(node: Node) -> bool:
    return "nicegui-column" in node.classes or ("column" in node.classes and "q-page" not in node.tag)


def _is_content(node: Node) -> bool:
    return "nicegui-content" in node.classes


def _label_text(node: Node) -> str:
    if node.text is not None:
        return str(node.text)
    lbl = node.props.get("label")
    if lbl:
        return str(lbl)
    return ""


class _Placeholder(Static):
    DEFAULT_CSS = """
    _Placeholder { color: $text-muted; }
    """


def _build(node: Node, children: list[Widget], app: "WireTuiApp") -> Widget | None:
    """Return a Textual widget for the given wire node.

    ``children`` are the already-materialised child widgets; they are passed
    into the constructor so the widget tree is fully assembled before being
    handed to Textual (which requires child widgets to be known at mount
    time, not spliced in via deep ``mount()`` calls on unmounted parents).

    Returns ``None`` if this node should be completely skipped.
    """
    tag = node.tag

    if tag in ("q-layout", "q-page-container", "q-page"):
        w = Vertical(*children) if children else Vertical()
        w.styles.height = "auto"
        return _decorate(w, node)

    if tag == "div":
        if node.text is not None and not children:
            return _decorate(Static(str(node.text)), node)
        if _is_row(node):
            w = Horizontal(*children)
        elif _is_column(node) or _is_content(node):
            w = Vertical(*children)
        else:
            w = Vertical(*children)
        w.styles.height = "auto"
        return _decorate(w, node)

    if tag == "q-btn":
        label = _label_text(node) or "Button"
        btn = Button(label, id=f"ng-{node.id}")
        app._register_button(node.id, btn)
        return _decorate(btn, node)

    if tag == "nicegui-input":
        value = str(node.props.get("value", "") or "")
        placeholder = str(node.props.get("placeholder", "") or "")
        is_password = node.props.get("type") == "password"
        inp = Input(value=value, placeholder=placeholder, password=is_password, id=f"ng-{node.id}")
        label = node.props.get("label")
        if label:
            inp.border_title = str(label)
        app._register_input(node.id, inp)
        return _decorate(inp, node)

    if tag == "q-checkbox":
        value = bool(node.props.get("model-value", False))
        text = _label_text(node)
        cb = Checkbox(text, value=value, id=f"ng-{node.id}")
        app._register_toggle(node.id, cb, "update:modelValue")
        return _decorate(cb, node)

    if tag == "q-toggle":
        value = bool(node.props.get("model-value", False))
        sw = Switch(value=value, id=f"ng-{node.id}")
        if text := _label_text(node):
            sw.tooltip = text
        app._register_toggle(node.id, sw, "update:modelValue")
        return _decorate(sw, node)

    if tag == "q-select":
        options = node.props.get("options", []) or []
        labels = [
            str(o.get("label", o) if isinstance(o, dict) else o)
            for o in options
        ] or [""]
        pairs = [(lbl, i) for i, lbl in enumerate(labels)]
        sel = Select(options=pairs, id=f"ng-{node.id}")
        return _decorate(sel, node)

    if tag in ("q-separator",):
        return _decorate(Rule(), node)

    if tag in ("q-markdown", "markdown-element", "nicegui-markdown"):
        content = str(node.props.get("content", "") or node.text or "")
        md = Markdown(content)
        return _decorate(md, node)

    if tag in ("q-card",):
        v = Vertical(*children) if children else Vertical()
        v.styles.height = "auto"
        v.styles.border = ("round", "grey")
        v.styles.padding = (0, 1)
        return _decorate(v, node)

    ph = _Placeholder(f"[unsupported: {tag}]")
    return _decorate(ph, node)


def _decorate(w: Widget, node: Node) -> Widget:
    """Stash the wire element id on the widget + attach default sizing."""
    w.ng_id = node.id  # type: ignore[attr-defined]
    if not node.classes or "nicegui-content" not in node.classes:
        # leave overall page expanding; otherwise let widgets size to content.
        try:
            w.styles.width = "auto"
            w.styles.height = "auto"
        except Exception:
            pass
    return w


# ---------------------------------------------------------------------------
# The App
# ---------------------------------------------------------------------------


class WireTuiApp(App):
    """Textual app that mirrors a live NiceGUI page over the wire."""

    CSS = """
    Screen { background: $surface; }
    Input  { width: 40; }
    Select { width: 30; }
    Button { margin: 0 1; }
    _Placeholder { color: $text-muted; padding: 0 1; }
    #root { padding: 1 2; }
    """
    BINDINGS = [
        Binding("ctrl+c", "quit", "quit"),
        Binding("ctrl+q", "quit", "quit"),
        Binding("ctrl+r", "refresh_tree", "refresh"),
    ]

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url
        self.sniffer = Sniffer(url, verbose=False)
        self._sniff_task: asyncio.Task | None = None
        self._root_mount: Vertical | None = None
        # element_id -> widget (last mounted)
        self._widgets: dict[int, Widget] = {}
        # input element_id -> Textual Input widget (for outbound "update:value")
        self._inputs: dict[int, Input] = {}
        # button element_id -> Textual Button
        self._buttons: dict[int, Button] = {}
        # toggle element_id -> (widget, listener_type) e.g. "update:modelValue"
        self._toggles: dict[int, tuple[Widget, str]] = {}

    # ------------------------------------------------------------------
    # App lifecycle
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        self._root_mount = VerticalScroll(id="root")
        yield self._root_mount

    async def on_mount(self) -> None:
        # Kick off wire client.
        self.sniffer.client.on_message(self._on_wire_message)
        await self.sniffer.client.connect()
        self.sniffer.tree.ingest_initial(self.sniffer.client.bootstrap.elements)
        self._rebuild()
        self._sniff_task = asyncio.create_task(self._keep_alive())

    async def _keep_alive(self) -> None:
        try:
            await self.sniffer.client.run_until_disconnect()
        finally:
            self.exit()

    async def on_unmount(self) -> None:
        await self.sniffer.client.disconnect()
        if self._sniff_task is not None:
            self._sniff_task.cancel()

    # ------------------------------------------------------------------
    # Tree (re)build
    # ------------------------------------------------------------------

    def _on_wire_message(self, event: str, data: Any) -> None:
        # Socket.IO events fire in the same asyncio loop Textual runs on, so
        # we post back via call_later (not call_from_thread).
        if event == "update" and isinstance(data, dict):
            self.call_later(self._rebuild)
        elif event == "notify" and isinstance(data, dict):
            message = str(data.get("message", ""))
            ntype = str(data.get("type", "info"))
            severity = {"negative": "error", "warning": "warning", "positive": "information"}.get(ntype, "information")
            self.call_later(lambda: self.notify(message, severity=severity, title=ntype))

    def action_refresh_tree(self) -> None:
        self._rebuild()

    def _rebuild(self) -> None:
        if self._root_mount is None:
            return
        root = self.sniffer.tree.root()
        self._root_mount.remove_children()
        self._widgets.clear()
        self._inputs.clear()
        self._buttons.clear()
        self._toggles.clear()
        if root is None:
            return
        widget = self._materialize(root)
        if widget is not None:
            self._root_mount.mount(widget)

    def _materialize(self, node: Node) -> Widget | None:
        # Build bottom-up: children first, then the parent with children as
        # constructor args. Textual containers accept child widgets as varargs
        # so the whole tree can be assembled offline and then mounted in one
        # shot.
        built_children: list[Widget] = []
        for child in self.sniffer.tree.children(node.id):
            cw = self._materialize(child)
            if cw is not None:
                built_children.append(cw)
        w = _build(node, built_children, self)
        if w is None:
            return None
        self._widgets[node.id] = w
        return w

    # ------------------------------------------------------------------
    # Registration hooks for event-interactive widgets
    # ------------------------------------------------------------------

    def _register_button(self, eid: int, w: Button) -> None:
        self._buttons[eid] = w

    def _register_input(self, eid: int, w: Input) -> None:
        self._inputs[eid] = w

    def _register_toggle(self, eid: int, w: Widget, listener: str) -> None:
        self._toggles[eid] = (w, listener)

    # ------------------------------------------------------------------
    # Outbound events (stretch): click + input change
    # ------------------------------------------------------------------

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        eid = self._ng_id_of(event.button)
        if eid is None:
            return
        try:
            await self.sniffer.fire(eid, "click")
        except Exception as exc:
            log.debug("button %s click failed: %s", eid, exc)

    async def on_input_changed(self, event: Input.Changed) -> None:
        eid = self._ng_id_of(event.input)
        if eid is None:
            return
        try:
            await self.sniffer.fire(eid, "update:value", args=event.value)
        except Exception as exc:
            log.debug("input %s update failed: %s", eid, exc)

    async def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        eid = self._ng_id_of(event.checkbox)
        if eid is None:
            return
        try:
            await self.sniffer.fire(eid, "update:modelValue", args=event.value)
        except Exception as exc:
            log.debug("checkbox %s update failed: %s", eid, exc)

    async def on_switch_changed(self, event: Switch.Changed) -> None:
        eid = self._ng_id_of(event.switch)
        if eid is None:
            return
        try:
            await self.sniffer.fire(eid, "update:modelValue", args=event.value)
        except Exception as exc:
            log.debug("switch %s update failed: %s", eid, exc)

    @staticmethod
    def _ng_id_of(widget: Widget) -> int | None:
        val = getattr(widget, "ng_id", None)
        if val is not None:
            return int(val)
        wid = widget.id or ""
        if wid.startswith("ng-"):
            try:
                return int(wid[3:])
            except ValueError:
                pass
        return None


def run(url: str) -> int:  # pragma: no cover
    """CLI entry point for ``ngwire tui URL``."""
    app = WireTuiApp(url)
    app.run()
    return 0
