"""Parse a NiceGUI index page to extract bootstrap config + initial element tree.

The NiceGUI server (v3.x) returns an HTML document whose <body> ends with a
``<script type="module">`` block containing:

    const app = createApp(parseElements(String.raw`<...json...>`), {
        version: "<x.y.z>",
        prefix: "<prefix>",
        query: <json>,
        extraHeaders: <json>,
        transports: <json>,
    });

The ``String.raw`...` `` argument is HTML-escaped JSON describing every
element already on the page, keyed by integer element ID. The ``query`` object
carries ``client_id`` and ``next_message_id`` — the two things we need for the
Socket.IO handshake.

This module makes no NiceGUI imports. It only reads text.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any


# ``build_response`` in nicegui/client.py HTML-escapes the following five
# characters before stuffing the element tree into ``String.raw\`...\```:
#     &  <  >  `  $
# See HTML_ESCAPE_TABLE. We have to reverse this to get valid JSON back.
_HTML_UNESCAPE = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&#96;": "`",
    "&#36;": "$",
}


def _html_unescape(s: str) -> str:
    for k, v in _HTML_UNESCAPE.items():
        s = s.replace(k, v)
    return s


@dataclass
class BootstrapConfig:
    """Everything we need to connect a Socket.IO client to this NiceGUI page."""

    version: str
    prefix: str
    query: dict[str, Any]  # includes client_id, next_message_id, implicit_handshake
    extra_headers: dict[str, Any]
    transports: list[str]
    elements: dict[int, dict[str, Any]]  # initial element tree, id -> element dict
    raw_script: str = field(repr=False, default="")

    @property
    def client_id(self) -> str:
        cid = self.query.get("client_id")
        if not cid:
            raise ValueError("no client_id in bootstrap query")
        return str(cid)

    @property
    def next_message_id(self) -> int:
        nmi = self.query.get("next_message_id", 0)
        return int(nmi)

    @property
    def implicit_handshake(self) -> bool:
        return bool(self.query.get("implicit_handshake", False))


# Grab the single <script type="module"> that contains createApp(...). There
# are usually two module scripts; only the second one has createApp.
_SCRIPT_RE = re.compile(
    r'<script\s+type="module"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# The config block after parseElements(...) looks like this (Jinja + Python
# repr means string values are double-quoted strings but dict/list values use
# Python repr, i.e. single quotes, True/False/None):
#
#     createApp(parseElements(String.raw`...`), {
#         version: "3.10.0",
#         prefix: "",
#         query: {'client_id': 'd27f...', 'next_message_id': 0, 'implicit_handshake': True},
#         extraHeaders: {},
#         transports: ['websocket', 'polling'],
#     });
#
# Parse each field independently using regex + ast.literal_eval.
_VERSION_RE    = re.compile(r'\bversion\s*:\s*"([^"]*)"')
_PREFIX_RE     = re.compile(r'\bprefix\s*:\s*"([^"]*)"')
_QUERY_RE      = re.compile(r"\bquery\s*:\s*(\{[^{}]*\})")
_HEADERS_RE    = re.compile(r"\bextraHeaders\s*:\s*(\{[^{}]*\})")
_TRANSPORTS_RE = re.compile(r"\btransports\s*:\s*(\[[^\[\]]*\])")


def parse_bootstrap(html: str) -> BootstrapConfig:
    """Extract :class:`BootstrapConfig` from a NiceGUI ``index.html`` response.

    Raises ``ValueError`` if the shape doesn't match what we expect.
    """
    script_body = None
    for m in _SCRIPT_RE.finditer(html):
        body = m.group(1)
        if "createApp" in body and "parseElements" in body:
            script_body = body
            break
    if script_body is None:
        raise ValueError("no <script type=module> with createApp(parseElements(...)) found")

    elements_match = re.search(
        r"parseElements\s*\(\s*String\.raw`(.*?)`\s*\)",
        script_body,
        re.DOTALL,
    )
    if elements_match is None:
        raise ValueError("could not locate parseElements(String.raw`...`) literal")
    elements_raw = elements_match.group(1)
    config = _parse_config_fields(script_body)

    elements_json = _html_unescape(elements_raw)
    try:
        elements_by_id_str = json.loads(elements_json)
    except json.JSONDecodeError as e:
        snippet = elements_json[:200]
        raise ValueError(f"failed to decode initial element tree JSON: {e}; head={snippet!r}") from e

    elements = {int(k): v for k, v in elements_by_id_str.items()}

    return BootstrapConfig(
        version=config.get("version", ""),
        prefix=config.get("prefix", ""),
        query=config.get("query", {}),
        extra_headers=config.get("extraHeaders", {}),
        transports=config.get("transports", []),
        elements=elements,
        raw_script=script_body,
    )


def _parse_config_fields(text: str) -> dict[str, Any]:
    """Extract named config fields from the createApp() config block.

    Fields come from Jinja ``str()``-rendering of Python objects, which is
    Python literal syntax (single-quoted strings, ``True``/``False``/``None``).
    """
    out: dict[str, Any] = {}
    m = _VERSION_RE.search(text)
    if m:
        out["version"] = m.group(1)
    m = _PREFIX_RE.search(text)
    if m:
        out["prefix"] = m.group(1)
    m = _QUERY_RE.search(text)
    if m:
        out["query"] = _safe_literal(m.group(1)) or {}
    m = _HEADERS_RE.search(text)
    if m:
        out["extraHeaders"] = _safe_literal(m.group(1)) or {}
    m = _TRANSPORTS_RE.search(text)
    if m:
        out["transports"] = _safe_literal(m.group(1)) or []
    return out


def _safe_literal(s: str) -> Any:
    """Parse a Python literal. Returns ``None`` on failure."""
    try:
        return ast.literal_eval(s)
    except (ValueError, SyntaxError):
        try:
            return json.loads(s)
        except Exception:
            return None
