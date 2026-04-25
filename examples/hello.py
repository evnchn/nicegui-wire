"""Minimal NiceGUI test app for wire sniffing.

Start with::

    python examples/hello.py

Then in another terminal::

    ngwire sniff http://127.0.0.1:8181/
"""
from datetime import datetime

from nicegui import ui


counter = {"n": 0}


@ui.page("/")
def index():
    ui.label("nicegui-wire: Hello World")

    # Server-pushed clock — proves updates aren't bound to user-trigger events.
    clock = ui.label("--:--:--")
    ui.timer(1.0, lambda: clock.set_text(datetime.now().strftime("%H:%M:%S")))

    count_label = ui.label("count = 0")
    with ui.row():
        ui.button("Increment", on_click=lambda: _bump(count_label, +1))
        ui.button("Decrement", on_click=lambda: _bump(count_label, -1))
        ui.button("Notify", on_click=lambda: ui.notify("Hi from NiceGUI!"))

    text = ui.input("Type here", value="")
    ui.label().bind_text_from(text, "value", lambda v: f"You typed: {v!r}")

    cb = ui.checkbox("A checkbox")
    ui.label().bind_text_from(cb, "value", lambda v: f"checkbox = {v}")

    sw = ui.switch("A switch")
    ui.label().bind_text_from(sw, "value", lambda v: f"switch = {v}")


def _bump(label, delta: int) -> None:
    counter["n"] += delta
    label.text = f"count = {counter['n']}"


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(host="127.0.0.1", port=8181, title="ngwire-hello", show=False, reload=False)
