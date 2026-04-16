"""In-memory element tree reconstructed from the wire.

The NiceGUI server sends a full element dict for every create/update and
``null`` for deletes, keyed by integer element ID. An element has roughly
this shape (see ``nicegui/element.py::Element._to_dict``)::

    {
        "id": int,
        "tag": "div" | "q-btn" | "q-input" | ...,
        "props": {...},
        "text": str | None,
        "class": str,          # space-separated classnames
        "style": str,          # CSS string
        "component": str | None,
        "libraries": [...],
        "exposed_libraries": [...],
        "events": [{"listener_type": "update:modelValue", ...}],
        "children": [child_id, ...],   # list of INTEGER ids, not nested
    }

The children list is flat (integer IDs), so the whole tree is keyed by id
and stitched via these references. The root of a page is always element 0.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator


ROOT_ID = 0


@dataclass
class Node:
    """A single element in the wire tree."""

    id: int
    tag: str = ""
    props: dict[str, Any] = field(default_factory=dict)
    text: str | None = None
    classes: list[str] = field(default_factory=list)
    style: str = ""
    component: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    children_ids: list[int] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Node":
        cls_str = d.get("class") or ""
        classes = cls_str.split() if isinstance(cls_str, str) else list(cls_str)
        return cls(
            id=int(d.get("id", -1)),
            tag=str(d.get("tag", "")),
            props=dict(d.get("props", {}) or {}),
            text=d.get("text"),
            classes=classes,
            style=str(d.get("style") or ""),
            component=d.get("component"),
            events=list(d.get("events") or []),
            children_ids=list(d.get("children") or []),
            raw=d,
        )


class ElementTree:
    """Mutable tree rebuilt from ``update`` deltas.

    Notes on semantics:
        * ``nodes[id] = None`` means that id was sent as a deletion.
        * Parent ids are implicit — inferred from the ``children`` list of
          each node. We index them as we ingest.
        * The tree root is id 0. If the HTML bootstrap didn't include an id
          0, the root is the smallest id we've seen that no other node
          lists as a child.
    """

    def __init__(self) -> None:
        self.nodes: dict[int, Node] = {}
        self.parent_of: dict[int, int] = {}

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def ingest_initial(self, elements: dict[int, dict[str, Any]]) -> None:
        """Ingest the initial element tree parsed out of the HTML bootstrap."""
        for eid, d in elements.items():
            self._upsert(int(eid), d)
        self._reindex_parents()

    def apply_update(self, payload: dict[str, Any]) -> None:
        """Apply a server ``update`` event. Keys are string element IDs; values
        are either the new element dict or ``None`` (delete)."""
        for k, v in payload.items():
            if k == "_id":
                continue  # message-id bookkeeping, not an element
            try:
                eid = int(k)
            except (TypeError, ValueError):
                continue
            if v is None:
                self._delete(eid)
            else:
                self._upsert(eid, v)
        self._reindex_parents()

    def _upsert(self, eid: int, d: dict[str, Any]) -> None:
        self.nodes[eid] = Node.from_dict({**d, "id": eid})

    def _delete(self, eid: int) -> None:
        self.nodes.pop(eid, None)
        self.parent_of.pop(eid, None)

    def _reindex_parents(self) -> None:
        self.parent_of.clear()
        for node in self.nodes.values():
            for cid in node.children_ids:
                self.parent_of[cid] = node.id

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def root(self) -> Node | None:
        if ROOT_ID in self.nodes:
            return self.nodes[ROOT_ID]
        # Fallback: pick a node that nobody references as child.
        referenced: set[int] = set()
        for n in self.nodes.values():
            referenced.update(n.children_ids)
        for nid in sorted(self.nodes):
            if nid not in referenced:
                return self.nodes[nid]
        return None

    def children(self, eid: int) -> list[Node]:
        node = self.nodes.get(eid)
        if node is None:
            return []
        return [self.nodes[c] for c in node.children_ids if c in self.nodes]

    def walk(self, start_id: int | None = None) -> Iterator[tuple[Node, int]]:
        """Depth-first walk yielding ``(node, depth)``."""
        start = self.nodes.get(start_id) if start_id is not None else self.root()
        if start is None:
            return
        stack: list[tuple[Node, int]] = [(start, 0)]
        seen: set[int] = set()
        while stack:
            node, depth = stack.pop()
            if node.id in seen:
                continue
            seen.add(node.id)
            yield node, depth
            for cid in reversed(node.children_ids):
                child = self.nodes.get(cid)
                if child:
                    stack.append((child, depth + 1))

    def render_text(self, start_id: int | None = None, indent: str = "  ") -> str:
        """Very dumb pretty-print for debugging."""
        out: list[str] = []
        for node, depth in self.walk(start_id):
            label = node.tag or node.component or "?"
            extras: list[str] = [f"#{node.id}"]
            if node.text:
                extras.append(repr(node.text[:40]))
            if node.props:
                interesting = {
                    k: v for k, v in node.props.items()
                    if k in ("label", "value", "model-value", "placeholder", "href", "name")
                }
                if interesting:
                    extras.append(str(interesting))
            out.append(f"{indent * depth}{label} {' '.join(extras)}")
        return "\n".join(out)
