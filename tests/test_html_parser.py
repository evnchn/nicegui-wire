"""Parser survives Python-literal config + HTML-escaped element JSON."""
from __future__ import annotations

from nicegui_wire.html_parser import parse_bootstrap


SAMPLE = """
<!doctype html>
<html><body>
<script type="module">
  const app = createApp(parseElements(String.raw`{"0":{"tag":"q-layout","children":[1]},"1":{"tag":"div","text":"hello &amp; world"}}`), {
    version: "3.10.0",
    prefix: "",
    query: {'client_id': 'abc-123', 'next_message_id': 0, 'implicit_handshake': True},
    extraHeaders: {},
    transports: ['websocket', 'polling'],
  });
</script>
</body></html>
"""


def test_parse_sample():
    b = parse_bootstrap(SAMPLE)
    assert b.version == "3.10.0"
    assert b.client_id == "abc-123"
    assert b.next_message_id == 0
    assert b.implicit_handshake is True
    assert b.transports == ["websocket", "polling"]
    assert 0 in b.elements
    assert 1 in b.elements
    assert b.elements[1]["text"] == "hello & world"  # HTML unescape


def test_live_server_bootstrap(hello_url):
    import urllib.request
    html = urllib.request.urlopen(hello_url, timeout=5).read().decode()
    b = parse_bootstrap(html)
    assert b.client_id
    assert len(b.elements) >= 4
    # Root q-layout with children.
    assert b.elements[0]["tag"] == "q-layout"
