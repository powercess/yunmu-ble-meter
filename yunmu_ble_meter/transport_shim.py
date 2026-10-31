"""Import-safe stand-ins used by tests when ``dbus-python`` is unavailable.

The transport module itself is only importable on a machine with BlueZ bindings;
the dataclasses it defines are consumed by the metrics layer, so they live in a
dependency-free module and are re-exported from :mod:`yunmu_ble_meter.transport`.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Reading"]


@dataclass
class Reading:
    """One decoded device reading."""

    counter: int
    """16-bit device accumulator. Wraps at 65536; resets to 0 on ``clear``."""

    voltage: float
    """Volts."""

    power: float
    """Active power, watts."""

    current: float
    """Amperes."""

    power_factor: float
    """0..1."""

    threshold: int
    """Alarm threshold, watts, as echoed by the device."""

    raw: bytes
    """The full 32-byte plaintext response, for debugging."""
