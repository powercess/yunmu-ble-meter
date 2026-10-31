"""YunMu (云牧) BLE metering socket client.

Reverse-engineered protocol + BlueZ transport + Prometheus exporter for the
YunMu YM-S1-BES BLE energy meter.

The protocol codec (:mod:`yunmu_ble_meter.protocol`) has no dependencies and is
importable anywhere, which makes it usable for offline analysis. The transport
needs ``dbus-python`` and ``PyGObject`` from the distro; those imports are
deferred so that ``from yunmu_ble_meter import protocol`` works on any machine.
"""

from __future__ import annotations

from . import protocol
from .protocol import (
    DecodeError,
    ReplyType,
    Response,
    advertised_name,
    build_clear,
    build_frame,
    build_read,
    build_set_threshold,
    mac_from_advertised_name,
    parse_frame,
    parse_response,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "protocol",
    "DecodeError",
    "ReplyType",
    "Response",
    "advertised_name",
    "build_clear",
    "build_frame",
    "build_read",
    "build_set_threshold",
    "mac_from_advertised_name",
    "parse_frame",
    "parse_response",
    # deferred, need dbus-python
    "BleMeter",
    "DeviceNotFound",
    "TransportError",
    "Reading",
    "MeterMetrics",
    "Exporter",
    "start_exporter",
    "collector_loop",
    "main",
]

_LAZY = {
    "BleMeter": ("transport", "BleMeter"),
    "DeviceNotFound": ("transport", "DeviceNotFound"),
    "TransportError": ("transport", "TransportError"),
    "Reading": ("transport", "Reading"),
    "MeterMetrics": ("metrics", "MeterMetrics"),
    "collector_loop": ("metrics", "collector_loop"),
    "Exporter": ("exporter", "Exporter"),
    "start_exporter": ("exporter", "start_exporter"),
    "main": ("cli", "main"),
}


def __getattr__(name: str):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    from importlib import import_module

    module = import_module(f".{module_name}", __name__)
    value = getattr(module, attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
