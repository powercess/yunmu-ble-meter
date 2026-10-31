"""Prometheus metrics for a YunMu BLE meter.

Exposes the meter's live readings and a locally integrated energy total. The
device's own accumulator is 16 bits and wraps, and ``clear`` resets it, so it is
published as a raw gauge while the integrated value is what you want for
long-horizon energy accounting.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .transport_shim import Reading

__all__ = ["MeterMetrics", "collector_loop"]


HELP = {
    "yunmu_voltage_volts": "Line voltage in volts.",
    "yunmu_current_amperes": "Load current in amperes.",
    "yunmu_power_watts": "Active power in watts.",
    "yunmu_power_factor": "Power factor (0..1).",
    "yunmu_threshold_watts": "Configured alarm threshold in watts.",
    "yunmu_counter_total": "Raw 16-bit device accumulator (wraps at 65536, resets on clear).",
    "yunmu_energy_kwh": "Energy integrated locally since the exporter started, in kWh.",
    "yunmu_scrape_success": "1 if the most recent device read succeeded, else 0.",
    "yunmu_last_read_timestamp_seconds": "Unix timestamp of the last successful read.",
    "yunmu_read_duration_seconds": "Duration of the most recent device read.",
}


@dataclass
class MeterMetrics:
    """Thread-safe snapshot of the meter's state, rendered as Prometheus text."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _values: dict[str, float] = field(default_factory=dict, repr=False)
    _last_ts: float = 0.0
    _energy_kwh: float = 0.0
    _last_sample: tuple[float, float] | None = None
    _scrape_ok: float = 0.0
    _read_duration: float = 0.0
    last_error: str = ""

    def record(self, reading: Reading, duration: float) -> None:
        """Fold one successful reading into the exported metrics."""
        now = time.time()
        with self._lock:
            # Integrate only while the sample interval is plausible; a gap means
            # the exporter was down and we cannot attribute the missing energy.
            if self._last_sample is not None:
                prev_ts, prev_w = self._last_sample
                dt = now - prev_ts
                if 0 < dt < 30:
                    self._energy_kwh += prev_w * dt / 3600.0 / 1000.0
            self._last_sample = (now, reading.power)

            self._values = {
                "yunmu_voltage_volts": reading.voltage,
                "yunmu_current_amperes": reading.current,
                "yunmu_power_watts": reading.power,
                "yunmu_power_factor": reading.power_factor,
                "yunmu_threshold_watts": float(reading.threshold),
                "yunmu_counter_total": float(reading.counter),
                "yunmu_energy_kwh": self._energy_kwh,
            }
            self._last_ts = now
            self._scrape_ok = 1.0
            self._read_duration = duration

    def record_failure(self, duration: float) -> None:
        """Mark the most recent read as failed without discarding last values."""
        with self._lock:
            self._scrape_ok = 0.0
            self._read_duration = duration
            # a failed read breaks the integration chain
            self._last_sample = None

    def reset_energy(self) -> None:
        """Zero the locally integrated total (does not touch the device)."""
        with self._lock:
            self._energy_kwh = 0.0

    def render(self) -> str:
        """Render in Prometheus text exposition format."""
        with self._lock:
            values = dict(self._values)
            last_ts = self._last_ts
            ok = self._scrape_ok
            duration = self._read_duration

        values["yunmu_scrape_success"] = ok
        values["yunmu_last_read_timestamp_seconds"] = last_ts
        values["yunmu_read_duration_seconds"] = duration

        lines: list[str] = []
        for name, value in values.items():
            help_text = HELP.get(name)
            if help_text:
                lines.append(f"# HELP {name} {help_text}")
            kind = "counter" if name == "yunmu_energy_kwh" else "gauge"
            lines.append(f"# TYPE {name} {kind}")
            lines.append(f"{name} {_fmt(value)}")
        return "\n".join(lines) + "\n"


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
    """Poll ``meter`` forever, feeding ``metrics``.

    Reconnects on failure. The device refuses rapid reconnects
    (``le-connection-abort-by-local``) and stops advertising once disconnected,
    so retries are deliberately slow.
    """
    connected = False
    while not stop.is_set():
        started = time.monotonic()
        try:
            if not connected:
                meter.connect()
                connected = True
            reading = meter.read()
            metrics.record(reading, time.monotonic() - started)
        except Exception as exc:
            metrics.record_failure(time.monotonic() - started)
            metrics.last_error = f"{type(exc).__name__}: {exc}"
            connected = False
            try:
                meter.close()
            except Exception:
                pass
            stop.wait(10.0)
            continue
        stop.wait(interval)
