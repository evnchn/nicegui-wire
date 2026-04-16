"""nicegui-wire: network-level NiceGUI client.

Public entry points:
    from nicegui_wire import WireClient, Sniffer
"""
from __future__ import annotations

__version__ = "0.0.1"

from .sio_client import WireClient  # noqa: F401
from .sniffer import Sniffer        # noqa: F401
