"""A JSON-line sniffer for NiceGUI wire traffic.

Run::

    python -m nicegui_wire.cli sniff http://localhost:8080/

and every inbound server message is printed as one JSONL record. Also
prints the bootstrap config and the initial element tree.

Optionally writes to a file so you can replay it for renderer development.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from typing import IO, Any

from .sio_client import WireClient
from .tree import ElementTree


class Sniffer:
    """Glue code tying :class:`WireClient` to a live :class:`ElementTree`
    plus optional JSONL logging.

    The sniffer is intentionally passive: it does NOT send events back to
    the server. The ``/event`` sender lives on :class:`WireClient` directly.
    """

    def __init__(
        self,
        url: str,
        *,
        out: IO[str] | None = None,
        verbose: bool = True,
    ) -> None:
        self.url = url
        self.out = out
        self.verbose = verbose
        self.tree = ElementTree()
        self.client = WireClient(url)
        self.client.on_message(self._on_message)
        self._start_time: float = 0.0

    async def run(self) -> None:
        self._start_time = time.time()
        await self.client.connect()
        assert self.client.bootstrap is not None
        self._record("bootstrap", {
            "version": self.client.bootstrap.version,
            "prefix":  self.client.bootstrap.prefix,
            "elements_count": len(self.client.bootstrap.elements),
            "query_keys": sorted(self.client.bootstrap.query.keys()),
        })
        # Seed the tree from the HTML bootstrap.
        self.tree.ingest_initial(self.client.bootstrap.elements)
        self._record("initial_tree", {
            "ids": sorted(self.tree.nodes.keys()),
        })
        if self.verbose:
            print("--- initial tree ---", file=sys.stderr)
            print(self.tree.render_text(), file=sys.stderr)
            print("--- live messages below ---", file=sys.stderr)

        try:
            await self.client.run_until_disconnect()
        finally:
            await self.client.disconnect()

    def _on_message(self, event: str, data: Any) -> None:
        if event == "update" and isinstance(data, dict):
            self.tree.apply_update(data)
        self._record(event, data)

    async def fire(self, element_id: int, event_type: str, args: Any = None) -> None:
        """Send an ``event`` back to the server for the given element.

        Looks up the listener_id from the current tree. Raises ``KeyError``
        if the element doesn't have a listener for this event type.
        """
        node = self.tree.nodes.get(element_id)
        if node is None:
            raise KeyError(f"element {element_id} not in tree")
        listener_id: str | None = None
        for ev in node.events:
            if ev.get("type") == event_type:
                listener_id = ev.get("listener_id")
                break
        if listener_id is None:
            types = [e.get("type") for e in node.events]
            raise KeyError(f"element {element_id} has no listener for {event_type!r} (has {types})")
        await self.client.send_event(element_id, event_type, listener_id=listener_id, args=args)

    def _record(self, event: str, data: Any) -> None:
        entry = {
            "t": round(time.time() - self._start_time, 3),
            "event": event,
            "data": data,
        }
        line = json.dumps(entry, default=str)
        if self.verbose:
            # Keep stderr human-ish.
            summary = _summarize(event, data)
            print(f"[{entry['t']:>6}s] {event:<20} {summary}", file=sys.stderr)
        if self.out is not None:
            self.out.write(line + "\n")
            self.out.flush()


def _summarize(event: str, data: Any) -> str:
    if event == "update" and isinstance(data, dict):
        keys = [k for k in data.keys() if k != "_id"]
        return f"{len(keys)} elements: {keys[:10]}{'...' if len(keys) > 10 else ''}"
    if event == "notify" and isinstance(data, dict):
        return f"{data.get('type')}: {data.get('message')!r}"
    if event == "load_js_components" and isinstance(data, dict):
        comps = data.get("components", [])
        return f"{len(comps)} components"
    if isinstance(data, dict):
        return "{" + ", ".join(list(data.keys())[:5]) + "}"
    return repr(data)[:60]


async def _amain(url: str, outfile: str | None, verbose: bool) -> None:
    fh = open(outfile, "w") if outfile else None
    try:
        s = Sniffer(url, out=fh, verbose=verbose)
        await s.run()
    finally:
        if fh is not None:
            fh.close()


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    import argparse
    parser = argparse.ArgumentParser(prog="ngwire-sniff")
    parser.add_argument("url")
    parser.add_argument("-o", "--output", help="write JSONL to this file")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-v", "--verbose-log", action="store_true",
                        help="enable DEBUG logging on the Socket.IO client")
    args = parser.parse_args(argv)

    if args.verbose_log:
        logging.basicConfig(level=logging.DEBUG)
    try:
        asyncio.run(_amain(args.url, args.output, verbose=not args.quiet))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
