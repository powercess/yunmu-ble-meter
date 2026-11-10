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
    "yunmu_sample_age_seconds": "Seconds since the last successful read of this meter.",
    "yunmu_read_total": "Successful reads of this meter.",
    "yunmu_device_tick_total": "Device updates observed, i.e. times the reported fields changed.",
    "yunmu_sampling_gaps_total": "Reads that came more than one device hold (1 s) after the previous read.",
    "yunmu_tick_interval_seconds": "Smoothed seconds between observed device updates.",
    "yunmu_max_read_gap_seconds": "Longest interval between two consecutive reads since start.",
}

COUNTERS = {
    "yunmu_energy_kwh",
    "yunmu_energy_device_kwh",
    "yunmu_read_total",
    "yunmu_device_tick_total",
    "yunmu_sampling_gaps_total",
}

#: The meter holds its reported values for exactly one second and replaces the
#: whole record at once (measured 1.005 s median gap, phase-locked). Polling
#: faster than this yields duplicate reads; polling slower drops updates.
TICK_NOMINAL = 1.0

#: Wall time one read occupies the adapter, measured on a Pi 4 at the default
#: 22.5 ms connection interval (p50 67.7 ms). Serial reads are the only option
#: because the adapter holds one link at a time, so this bounds the fleet.
READ_COST_SECONDS = 0.07

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
    "yunmu_sample_age_seconds",
    "yunmu_read_total",
    "yunmu_device_tick_total",
    "yunmu_sampling_gaps_total",
    "yunmu_tick_interval_seconds",
    "yunmu_max_read_gap_seconds",
]

#: Fields that move when the device publishes a new record. The counter is
#: excluded: it is a 60-second accumulator and would look like a tick.
TICK_FIELDS = ("voltage", "power", "current", "power_factor")


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
    read_total: int = 0
    device_tick_total: int = 0
    sampling_gaps_total: int = 0
    max_read_gap: float = 0.0
    tick_interval: float = 0.0
    _last_sample: tuple[float, float] | None = field(default=None, repr=False)
    #: fields of the previous successful reading, for update detection
    _last_fields: tuple | None = field(default=None, repr=False)
    _last_tick_ts: float = field(default=0.0, repr=False)
    _last_read_ts: float = field(default=0.0, repr=False)

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
        """Fold one successful reading for ``mac``.

        Besides the values themselves this records two cadence signals. A change
        in :data:`TICK_FIELDS` marks one device update, which is timed into
        ``yunmu_tick_interval_seconds``. Separately, any read that arrives more
        than one :data:`TICK_NOMINAL` after the previous read is counted in
        ``yunmu_sampling_gaps_total``: that is the window in which a published
        record can be replaced unread, and unlike value changes it can be checked
        whether or not the load is moving.
        """
        state = self.register(mac)
        now = time.time()
        fields = tuple(getattr(reading, name) for name in TICK_FIELDS)
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
            if state._last_fields is not None and fields != state._last_fields:
                if state._last_tick_ts:
                    gap = now - state._last_tick_ts
                    # smoothed: two probes per tick give alternating gaps
                    state.tick_interval = (
                        gap if not state.tick_interval else 0.7 * state.tick_interval + 0.3 * gap
                    )
                state._last_tick_ts = now
                state.device_tick_total += 1
            state._last_fields = fields

            # Sampling cadence is the only property that can be checked
            # regardless of load. A gap longer than one device hold means a
            # record could have been published and replaced without being read.
            if state._last_read_ts:
                gap = now - state._last_read_ts
                state.max_read_gap = max(state.max_read_gap, gap)
                if gap > TICK_NOMINAL:
                    state.sampling_gaps_total += 1
            state._last_read_ts = now
            state.read_total += 1
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
            now = time.time()
            for state in self._devices.values():
                snapshot.append(
                    (
                        state.label,
                        dict(state.values),
                        state.last_ts,
                        state.scrape_ok,
                        state.read_duration,
                        state.energy_kwh,
                        (now - state.last_ts) if state.last_ts else 0.0,
                        state.read_total,
                        state.device_tick_total,
                        state.sampling_gaps_total,
                        state.tick_interval,
                        state.max_read_gap,
                    )
                )

        lines: list[str] = []
        for name in SERIES_ORDER:
            help_text = HELP.get(name)
            if help_text:
                lines.append(f"# HELP {name} {help_text}")
            kind = "counter" if name in COUNTERS else "gauge"
            lines.append(f"# TYPE {name} {kind}")
            for (
                label,
                values,
                last_ts,
                ok,
                duration,
                energy,
                age,
                reads,
                ticks,
                gaps,
                tick_interval,
                max_gap,
            ) in snapshot:
                if name == "yunmu_scrape_success":
                    value = ok
                elif name == "yunmu_last_read_timestamp_seconds":
                    value = last_ts
                elif name == "yunmu_read_duration_seconds":
                    value = duration
                elif name == "yunmu_energy_kwh":
                    value = energy
                elif name == "yunmu_sample_age_seconds":
                    value = age if last_ts else None
                elif name == "yunmu_read_total":
                    value = reads
                elif name == "yunmu_device_tick_total":
                    value = ticks
                elif name == "yunmu_sampling_gaps_total":
                    value = gaps
                elif name == "yunmu_tick_interval_seconds":
                    value = tick_interval or None
                elif name == "yunmu_max_read_gap_seconds":
                    value = max_gap
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


def collector_loop(
    meter,
    metrics: MeterMetrics,
    interval: float,
    stop: threading.Event,
    *,
    on_reading=None,
) -> None:
    """Poll a single ``meter`` forever, feeding ``metrics``.

    Reconnects on failure. The device refuses rapid reconnects
    (``le-connection-abort-by-local``) and stops advertising once disconnected,
    so retries are deliberately slow.
    """
    multi_collector_loop([meter], metrics, interval, stop, on_reading=on_reading)


def _due_times(interval: float, per_cycle: int) -> list[float]:
    """Offsets inside one sampling period at which to read.

    The device replaces its record once per second, so one read per second can
    alias against it and silently drop or double-count updates. Sampling twice
    per tick means an update can only be missed if it lands inside the gap
    between the two probes *and* the next cycle's first probe is late — which
    ``yunmu_sampling_gaps_total`` then reports.
    """
    step = interval / per_cycle
    return [i * step for i in range(per_cycle)]


def multi_collector_loop(
    meters: list,
    metrics: MeterMetrics,
    interval: float,
    stop: threading.Event,
    *,
    reconnect_backoff: float = 15.0,
    on_reading=None,
    reads_per_cycle: int = 2,
) -> None:
    """Poll several meters from one thread.

    One BLE adapter can only hold one link per meter at a time, and the
    controller serialises links, so meters are polled round-robin rather than
    concurrently. A meter that fails is skipped for ``reconnect_backoff``
    seconds instead of stalling the whole cycle.

    ``interval`` is the sampling period; with ``reads_per_cycle`` it sets the
    spacing between probes. The defaults (0.8 s / 2) put a probe every 0.4 s,
    i.e. two per 1 s device record, which is the cheapest cadence that still
    observes every record. Reads are scheduled on absolute deadlines so a slow
    read does not accumulate drift.
    """
    offline: set[str] = set()
    skip_until: dict[str, float] = {}
    failures: dict[str, int] = {}
    per_cycle = max(1, int(reads_per_cycle))
    offsets = _due_times(interval, per_cycle)
    next_slot = time.monotonic()

    while not stop.is_set():
        for offset in offsets:
            due = next_slot + offset
            wait = due - time.monotonic()
            if wait > 0 and stop.wait(wait):
                return
            for meter in meters:
                if stop.is_set():
                    return
                mac = getattr(meter, "mac", "")
                now = time.monotonic()
                if now < skip_until.get(mac, 0.0):
                    continue
                started = now
                try:
                    if mac in offline:
                        meter.connect()
                        offline.discard(mac)
                    reading = meter.read()
                    failures[mac] = 0
                    metrics.record_for(mac, reading, time.monotonic() - started)
                    if on_reading is not None:
                        on_reading(mac, reading)
                except Exception as exc:
                    metrics.record_failure_for(
                        mac, time.monotonic() - started, f"{type(exc).__name__}: {exc}"
                    )
                    offline.add(mac)
                    try:
                        meter.close()
                    except Exception:
                        pass
                    failures[mac] = failures.get(mac, 0) + 1
                    # A long-lived D-Bus connection can wedge after repeated
                    # link failures; rebuild it instead of retrying forever.
                    if failures[mac] >= 3:
                        reset = getattr(meter, "reset", None)
                        if callable(reset):
                            try:
                                reset()
                            except Exception:
                                pass
                        failures[mac] = 0
                    skip_until[mac] = time.monotonic() + reconnect_backoff
        next_slot += interval
        behind = time.monotonic() - next_slot
        if behind > interval:
            # Fell more than a full cycle behind (unplugged adapter, many
            # meters): resynchronise instead of replaying the backlog.
            next_slot = time.monotonic()
