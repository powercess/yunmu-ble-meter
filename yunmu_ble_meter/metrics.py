"""Prometheus metrics for one or more YunMu BLE meters.

Each meter contributes its own labelled series, so a single exporter can publish
a whole fleet. The device's own accumulator is 16 bits and wraps, and ``clear``
resets it, so it is published as a raw gauge while the locally integrated value
is what you want for long-horizon energy accounting.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .transport_shim import Reading

__all__ = ["MeterMetrics", "DeviceState", "collector_loop", "multi_collector_loop"]


HELP = {
    "yunmu_voltage_volts": "Line voltage in volts.",
    "yunmu_current_amperes": "Load current in amperes.",
    "yunmu_power_watts": "Active power in watts.",
    "yunmu_power_factor": "Power factor (0..1).",
    "yunmu_threshold_watts": "Configured alarm threshold in watts.",
    "yunmu_counter_total": "Raw 16-bit device accumulator (wraps at 65536, resets on clear).",
    "yunmu_energy_kwh": "Energy integrated locally since the exporter started, in kWh.",
    "yunmu_energy_device_kwh": "Cumulative energy reported by the meter itself, in kWh.",
    "yunmu_scrape_success": "1 if the most recent device read succeeded, else 0.",
    "yunmu_last_read_timestamp_seconds": "Unix timestamp of the last successful read.",
    "yunmu_read_duration_seconds": "Duration of the most recent device read.",
}

COUNTERS = {"yunmu_energy_kwh", "yunmu_energy_device_kwh"}

#: order matters for readable exposition output
SERIES_ORDER = [
    "yunmu_voltage_volts",
    "yunmu_current_amperes",
    "yunmu_power_watts",
    "yunmu_power_factor",
    "yunmu_threshold_watts",
    "yunmu_counter_total",
    "yunmu_energy_kwh",
    "yunmu_energy_device_kwh",
    "yunmu_scrape_success",
    "yunmu_last_read_timestamp_seconds",
    "yunmu_read_duration_seconds",
]


@dataclass
class DeviceState:
    """Per-meter snapshot plus the integration state it needs."""

    mac: str
    label: str = ""
    values: dict[str, float] = field(default_factory=dict)
    last_ts: float = 0.0
    energy_kwh: float = 0.0
    scrape_ok: float = 0.0
    read_duration: float = 0.0
    last_error: str = ""
    _last_sample: tuple[float, float] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.label:
            self.label = self.mac


@dataclass
class MeterMetrics:
    """Thread-safe collection of meters, rendered as Prometheus text.

    ``meter`` (single-device) is kept so existing callers and tests keep working;
    it shares the same storage as the multi-device path.
    """

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _devices: dict[str, DeviceState] = field(default_factory=dict, repr=False)
    _single: str | None = None

    # ------------------------------------------------------------ registry

    def register(self, mac: str, label: str = "") -> DeviceState:
        """Register a meter and return its state object."""
        key = mac.upper()
        with self._lock:
            state = self._devices.get(key)
            if state is None:
                state = DeviceState(mac=key, label=label or key)
                self._devices[key] = state
            elif label:
                state.label = label
            if self._single is None:
                self._single = key
            return state

    @property
    def devices(self) -> list[DeviceState]:
        with self._lock:
            return list(self._devices.values())

    def state(self, mac: str) -> DeviceState:
        return self.register(mac)

    # ------------------------------------------------------- single-device

    def record(self, reading: Reading, duration: float) -> None:
        """Fold one reading into the (implicit) single device."""
        with self._lock:
            mac = self._single or ""
            if mac and mac not in self._devices:
                self._devices[mac] = DeviceState(mac=mac)
        self.record_for(mac, reading, duration)

    def record_failure(self, duration: float) -> None:
        """Mark the single device's read as failed."""
        with self._lock:
            mac = self._single or ""
        self.record_failure_for(mac, duration)

    def reset_energy(self) -> None:
        """Zero the locally integrated totals (does not touch any device)."""
        with self._lock:
            states = list(self._devices.values())
        for state in states:
            self.reset_energy_for(state.mac)

    # -------------------------------------------------------- per-device

    def record_for(self, mac: str, reading: Reading, duration: float) -> None:
        """Fold one successful reading for ``mac``."""
        state = self.register(mac)
        now = time.time()
        with self._lock:
            # Integrate only while the sample interval is plausible; a gap means
            # the exporter was down and we cannot attribute the missing energy.
            if state._last_sample is not None:
                prev_ts, prev_w = state._last_sample
                dt = now - prev_ts
                if 0 < dt < 30:
                    state.energy_kwh += prev_w * dt / 3600.0 / 1000.0
            state._last_sample = (now, reading.power)
            state.values = {
                "yunmu_energy_device_kwh": getattr(reading, "device_energy_kwh", 0.0),
                "yunmu_voltage_volts": reading.voltage,
                "yunmu_current_amperes": reading.current,
                "yunmu_power_watts": reading.power,
                "yunmu_power_factor": reading.power_factor,
                "yunmu_threshold_watts": float(reading.threshold),
                "yunmu_counter_total": float(reading.counter),
            }
            state.last_ts = now
            state.scrape_ok = 1.0
            state.read_duration = duration
            state.last_error = ""

    def record_failure_for(self, mac: str, duration: float, error: str = "") -> None:
        """Mark ``mac``'s most recent read as failed without dropping last values."""
        state = self.register(mac)
        with self._lock:
            state.scrape_ok = 0.0
            state.read_duration = duration
            # a failed read breaks the integration chain
            state._last_sample = None
            if error:
                state.last_error = error

    def reset_energy_for(self, mac: str) -> None:
        state = self.register(mac)
        with self._lock:
            state.energy_kwh = 0.0

    # ----------------------------------------------------------- rendering

    def render(self) -> str:
        """Render every registered meter in Prometheus text exposition format."""
        with self._lock:
            snapshot = []
            for state in self._devices.values():
                snapshot.append(
                    (
                        state.label,
                        dict(state.values),
                        state.last_ts,
                        state.scrape_ok,
                        state.read_duration,
                        state.energy_kwh,
                    )
                )

        lines: list[str] = []
        for name in SERIES_ORDER:
            help_text = HELP.get(name)
            if help_text:
                lines.append(f"# HELP {name} {help_text}")
            kind = "counter" if name in COUNTERS else "gauge"
            lines.append(f"# TYPE {name} {kind}")
            for label, values, last_ts, ok, duration, energy in snapshot:
                if name == "yunmu_scrape_success":
                    value = ok
                elif name == "yunmu_last_read_timestamp_seconds":
                    value = last_ts
                elif name == "yunmu_read_duration_seconds":
                    value = duration
                elif name == "yunmu_energy_kwh":
                    value = energy
                else:
                    value = values.get(name)
                if value is None:
                    continue
                lines.append(f'{name}{{device="{_escape(label)}"}} {_fmt(value)}')
        return "\n".join(lines) + "\n" if lines else ""


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt(value: float) -> str:
    """Format a sample value.

    Prometheus accepts both decimal and scientific notation, but plain decimals
    are far easier to read in a scrape dump. Timestamps are integers by nature
    and are emitted without a fraction.
    """
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    text = f"{value:.9f}".rstrip("0").rstrip(".")
    return text or "0"


def collector_loop(meter, metrics: MeterMetrics, interval: float, stop: threading.Event) -> None:
    """Poll a single ``meter`` forever, feeding ``metrics``.

    Reconnects on failure. The device refuses rapid reconnects
    (``le-connection-abort-by-local``) and stops advertising once disconnected,
    so retries are deliberately slow.
    """
    mac = getattr(meter, "mac", "")
    connected = False
    while not stop.is_set():
        started = time.monotonic()
        try:
            if not connected:
                meter.connect()
                connected = True
            reading = meter.read()
            metrics.record_for(mac, reading, time.monotonic() - started)
        except Exception as exc:
            metrics.record_failure_for(
                mac, time.monotonic() - started, f"{type(exc).__name__}: {exc}"
            )
            connected = False
            try:
                meter.close()
            except Exception:
                pass
            stop.wait(10.0)
            continue
        stop.wait(interval)


def multi_collector_loop(
    meters: list,
    metrics: MeterMetrics,
    interval: float,
    stop: threading.Event,
    *,
    reconnect_backoff: float = 15.0,
    on_reading=None,
) -> None:
    """Poll several meters from one thread.

    One BLE adapter can only hold one link per meter at a time, and the
    controller serialises links, so meters are polled round-robin rather than
    concurrently. A meter that fails is skipped for ``reconnect_backoff``
    seconds instead of stalling the whole cycle.
    """
    from .transport import BleMeter, TransportError  # local import: needs dbus

    skip_until: dict[str, float] = {}
    while not stop.is_set():
        cycle_start = time.time()
        for meter in meters:
            if stop.is_set():
                break
            mac = getattr(meter, "mac", "")
            if time.time() < skip_until.get(mac, 0.0):
                continue
            started = time.monotonic()
            try:
                if not getattr(meter, "_connected", False):
                    meter.connect()
                    meter._connected = True
                reading = meter.read()
                metrics.record_for(mac, reading, time.monotonic() - started)
                if on_reading is not None:
                    on_reading(mac, reading)
            except Exception as exc:
                metrics.record_failure_for(
                    mac, time.monotonic() - started, f"{type(exc).__name__}: {exc}"
                )
                try:
                    meter._connected = False
                    meter.close()
                except Exception:
                    pass
                skip_until[mac] = time.time() + reconnect_backoff
            time.sleep(0.1)
        elapsed = time.time() - cycle_start
        remaining = interval - elapsed
        if remaining > 0:
            stop.wait(remaining)
