"""ElementTree ingestion + update + delete semantics."""
from __future__ import annotations

from nicegui_wire.tree import ElementTree


def test_ingest_initial_and_walk():
    t = ElementTree()
    t.ingest_initial({
        0: {"tag": "root", "children": [1, 2]},
        1: {"tag": "a", "text": "hi"},
        2: {"tag": "b", "text": "bye"},
    })
    ids = [n.id for n, _ in t.walk()]
    assert ids == [0, 1, 2]


def test_update_patches_node():
    t = ElementTree()
    t.ingest_initial({
        0: {"tag": "root", "children": [1]},
        1: {"tag": "label", "text": "old"},
    })
    t.apply_update({"1": {"tag": "label", "text": "new"}, "_id": 5})
    assert t.nodes[1].text == "new"


def test_update_deletes_node():
    t = ElementTree()
    t.ingest_initial({
        0: {"tag": "root", "children": [1, 2]},
        1: {"tag": "x"},
        2: {"tag": "y"},
    })
    t.apply_update({"2": None, "_id": 7})
    assert 2 not in t.nodes
    assert 1 in t.nodes


def test_root_fallback_when_no_zero():
    t = ElementTree()
    t.ingest_initial({
        5: {"tag": "r", "children": [6]},
        6: {"tag": "c"},
    })
    assert t.root().id == 5
