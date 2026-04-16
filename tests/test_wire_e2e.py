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
