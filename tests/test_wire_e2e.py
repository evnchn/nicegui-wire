"""End-to-end: connect to a real NiceGUI server, fire an event, see an update."""
from __future__ import annotations

import asyncio
import pytest

from nicegui_wire.sniffer import Sniffer


@pytest.mark.asyncio
async def test_click_increments_counter(hello_url):
    s = Sniffer(hello_url, verbose=False)
    await s.client.connect()
    try:
        s.tree.ingest_initial(s.client.bootstrap.elements)
        # Locate the button element (first q-btn) + the counter label.
        btn_id = next(
            nid for nid, node in s.tree.nodes.items() if node.tag == "q-btn"
        )
        label_id = next(
            nid for nid, node in s.tree.nodes.items()
            if node.tag == "div" and (node.text or "").startswith("count")
        )
        before = s.tree.nodes[label_id].text
        await s.fire(btn_id, "click")
        # Wait for the update to arrive.
        for _ in range(40):
            await asyncio.sleep(0.05)
            if s.tree.nodes[label_id].text != before:
                break
        assert s.tree.nodes[label_id].text != before
        assert s.tree.nodes[label_id].text.startswith("count")
    finally:
        await s.client.disconnect()


@pytest.mark.asyncio
async def test_input_value_propagates(hello_url):
    s = Sniffer(hello_url, verbose=False)
    await s.client.connect()
    try:
        s.tree.ingest_initial(s.client.bootstrap.elements)
        # Hello has an input bound to a label reading "you=<value>".
        inp_id = next(
            nid for nid, node in s.tree.nodes.items()
            if node.tag == "nicegui-input"
        )
        bound_label_id = next(
            nid for nid, node in s.tree.nodes.items()
            if node.tag == "div" and "you=" in (node.text or "")
        )
        before = s.tree.nodes[bound_label_id].text
        await s.fire(inp_id, "update:value", args="ducky")
        for _ in range(40):
            await asyncio.sleep(0.05)
            if s.tree.nodes[bound_label_id].text != before:
                break
        assert "ducky" in (s.tree.nodes[bound_label_id].text or "")
    finally:
        await s.client.disconnect()


@pytest.mark.asyncio
async def test_tui_renders_named_slots_of_unsupported_container(hello_url):
    # ui.splitter: unsupported tag, children only in its "before"/"after" slots.
    from textual.widgets import Static
    from nicegui_wire.textual_app import WireTuiApp
    app = WireTuiApp(hello_url)
    async with app.run_test() as pilot:
        for _ in range(40):
            await pilot.pause(0.05)
            texts = {str(w.content) for w in app.query(Static)}
            if {"left pane", "right pane"} <= texts:
                break
        assert {"left pane", "right pane"} <= texts


@pytest.mark.asyncio
async def test_tui_value_update_on_slotted_input_does_not_rebuild(hello_url):
    # The Input widget drops its prepend-slot icon; a value-only update must
    # still patch in place, since a rebuild steals focus while typing.
    from nicegui_wire.textual_app import WireTuiApp
    app = WireTuiApp(hello_url)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        node = next(n for n in app.sniffer.tree.nodes.values()
                    if n.tag == "nicegui-input" and n.props.get("label") == "Slotted")
        rebuilds = []
        app._rebuild = lambda: rebuilds.append(1)
        app._patch_or_rebuild({str(node.id): {**node.raw, "props": {**node.props, "value": "ab"}}})
        assert rebuilds == []
