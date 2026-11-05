"""Tests for the metrics aggregation and Prometheus rendering.

These exercise the exporter without touching BLE: readings are fed in directly,
which is also how the integration maths can be pinned deterministically.
"""

from __future__ import annotations

import pytest

from yunmu_ble_meter.metrics import MeterMetrics
from yunmu_ble_meter.protocol import ReplyType
from yunmu_ble_meter.transport_shim import Reading


def make_reading(**overrides) -> Reading:
    values = dict(
        counter=320,
        voltage=230.4,
        power=4.5,
        current=0.054,
        power_factor=0.36,
        threshold=2000,
        raw=b"\x08\x1d" + b"\x00" * 30,
    )
    values.update(overrides)
    return Reading(**values)


def parse_exposition(text: str, device: str | None = None) -> dict[str, float]:
    """Parse exposition text, keying by metric name.

    Series carry a ``device`` label; when several devices are present the caller
    passes ``device`` to select one, otherwise the first sample for each name is
    kept (which is what the single-device tests expect).
    """
    import re

    out: dict[str, float] = {}
    pattern = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})? (\S+)$')
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = pattern.match(line)
        if not m:
            continue
        name, _, labels, value = m.groups()
        if device is not None:
            dm = re.search(r'device="([^"]*)"', labels or "")
            if not dm or dm.group(1) != device:
                continue
        elif name in out:
            continue
        out[name] = float(value)
    return out


def test_render_contains_expected_series():
    metrics = MeterMetrics()
    metrics.record(make_reading(), duration=0.012)
    values = parse_exposition(metrics.render())

    assert values["yunmu_voltage_volts"] == pytest.approx(230.4)
    assert values["yunmu_power_watts"] == pytest.approx(4.5)
    assert values["yunmu_current_amperes"] == pytest.approx(0.054)
    assert values["yunmu_power_factor"] == pytest.approx(0.36)
    assert values["yunmu_threshold_watts"] == 2000
    assert values["yunmu_counter_total"] == 320
    assert values["yunmu_scrape_success"] == 1


def test_render_has_help_and_type_lines():
    metrics = MeterMetrics()
    metrics.record(make_reading(), duration=0.01)
    text = metrics.render()
    assert "# HELP yunmu_power_watts" in text
    assert "# TYPE yunmu_power_watts gauge" in text
    assert "# TYPE yunmu_energy_kwh counter" in text


def test_energy_integrates_power_over_time(monkeypatch):
    """4.5 W held for an hour must accumulate 0.0045 kWh."""
    metrics = MeterMetrics()
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr("time.time", lambda: clock["t"])

    metrics.record(make_reading(power=4.5), duration=0.01)
    for _ in range(3600):
        clock["t"] += 1.0
        metrics.record(make_reading(power=4.5), duration=0.01)

    values = parse_exposition(metrics.render())
    assert values["yunmu_energy_kwh"] == pytest.approx(4.5 * 3600 / 3600 / 1000, rel=0.01)


def test_failure_marks_scrape_and_breaks_integration(monkeypatch):
    metrics = MeterMetrics()
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr("time.time", lambda: clock["t"])

    metrics.record(make_reading(power=10.0), duration=0.01)
    clock["t"] += 2.0
    metrics.record_failure(duration=0.5)
    values = parse_exposition(metrics.render())
    assert values["yunmu_scrape_success"] == 0

    # A reading after a failure must not bridge the gap: the 600 s of 1000 W
    # that were never sampled must not be attributed.
    clock["t"] += 600.0
    metrics.record(make_reading(power=1000.0), duration=0.01)
    values = parse_exposition(metrics.render())
    assert values["yunmu_energy_kwh"] == 0.0


def test_reset_energy_zeroes_the_counter():
    metrics = MeterMetrics()
    metrics.record(make_reading(), duration=0.01)
    metrics.reset_energy()
    values = parse_exposition(metrics.render())
    assert values["yunmu_energy_kwh"] == 0


def test_failure_keeps_last_known_values():
    metrics = MeterMetrics()
    metrics.record(make_reading(power=7.5), duration=0.01)
    metrics.record_failure(duration=0.4)
    values = parse_exposition(metrics.render())
    assert values["yunmu_power_watts"] == pytest.approx(7.5)
    assert values["yunmu_scrape_success"] == 0


# ------------------------------------------------------ multi-device


MAC_A = "26:04:18:01:12:F4"
MAC_B = "26:01:15:04:2F:96"


def test_multiple_devices_render_separate_series():
    metrics = MeterMetrics()
    metrics.register(MAC_A)
    metrics.register(MAC_B)
    metrics.record_for(MAC_A, make_reading(voltage=232.1, power=0.0), 0.01)
    metrics.record_for(MAC_B, make_reading(voltage=231.6, power=6.4), 0.02)

    text = metrics.render()
    a = parse_exposition(text, device=MAC_A)
    b = parse_exposition(text, device=MAC_B)

    assert a["yunmu_voltage_volts"] == pytest.approx(232.1)
    assert b["yunmu_voltage_volts"] == pytest.approx(231.6)
    assert a["yunmu_power_watts"] == pytest.approx(0.0)
    assert b["yunmu_power_watts"] == pytest.approx(6.4)
    assert a["yunmu_scrape_success"] == 1
    assert b["yunmu_scrape_success"] == 1


def test_help_and_type_emitted_once_per_metric():
    metrics = MeterMetrics()
    for mac in (MAC_A, MAC_B):
        metrics.record_for(mac, make_reading(), 0.01)
    text = metrics.render()
    assert text.count("# HELP yunmu_voltage_volts") == 1
    assert text.count("# TYPE yunmu_voltage_volts") == 1
    assert text.count('yunmu_voltage_volts{device="') == 2


def test_failure_is_scoped_to_one_device():
    metrics = MeterMetrics()
    metrics.record_for(MAC_A, make_reading(power=5.0), 0.01)
    metrics.record_for(MAC_B, make_reading(power=7.0), 0.01)
    metrics.record_failure_for(MAC_B, 0.5, "boom")

    text = metrics.render()
    assert parse_exposition(text, device=MAC_A)["yunmu_scrape_success"] == 1
    assert parse_exposition(text, device=MAC_B)["yunmu_scrape_success"] == 0
    # last known values are retained for the failing device
    assert parse_exposition(text, device=MAC_B)["yunmu_power_watts"] == pytest.approx(7.0)


def test_energy_integrates_independently_per_device(monkeypatch):
    metrics = MeterMetrics()
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr("time.time", lambda: clock["t"])

    metrics.record_for(MAC_A, make_reading(power=100.0), 0.01)
    metrics.record_for(MAC_B, make_reading(power=200.0), 0.01)
    for _ in range(3600):
        clock["t"] += 1.0
        metrics.record_for(MAC_A, make_reading(power=100.0), 0.01)
        metrics.record_for(MAC_B, make_reading(power=200.0), 0.01)

    text = metrics.render()
    a = parse_exposition(text, device=MAC_A)["yunmu_energy_kwh"]
    b = parse_exposition(text, device=MAC_B)["yunmu_energy_kwh"]
    assert a == pytest.approx(100 * 3600 / 3600 / 1000, rel=0.01)
    assert b == pytest.approx(200 * 3600 / 3600 / 1000, rel=0.01)


def test_unknown_device_yields_no_series():
    metrics = MeterMetrics()
    metrics.record_for(MAC_A, make_reading(), 0.01)
    assert parse_exposition(metrics.render(), device="AA:BB:CC:DD:EE:FF") == {}


def test_label_is_escaped():
    metrics = MeterMetrics()
    metrics.register(MAC_A, label='weird"label')
    metrics.record_for(MAC_A, make_reading(), 0.01)
    text = metrics.render()
    assert 'device="weird\\"label"' in text


# ------------------------------------------------- device update cadence
#
# The meter replaces its whole record once a second (measured 1.005 s median,
# phase-locked), so "one sample per update" is the requirement these tests pin.


def _tick_series(metrics, clock, mac, ticks, read_hz):
    """Feed ``ticks`` device updates sampled at ``read_hz``, advancing the clock."""
    step = 1.0 / read_hz
    for i in range(int(ticks / step)):
        # the device publishes a new record once per second of elapsed time
        metrics.record_for(mac, make_reading(power=float(1 + int(i * step))), 0.01)
        clock["t"] += step


def test_sampling_slower_than_the_tick_is_reported(monkeypatch):
    """One read per 3 s must show up as scheduling gaps, not as silent loss."""
    metrics = MeterMetrics()
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr("time.time", lambda: clock["t"])

    _tick_series(metrics, clock, MAC_A, ticks=10, read_hz=1.0 / 3.0)
    v = parse_exposition(metrics.render(), device=MAC_A)
    # 10 s of wall time at one read per 3 s: reads at 0/3/6 (then the loop ends)
    assert v["yunmu_read_total"] == 3
    assert v["yunmu_sampling_gaps_total"] == 2
    assert v["yunmu_max_read_gap_seconds"] == pytest.approx(3.0, abs=0.05)


def test_device_tick_counts_each_value_change(monkeypatch):
    metrics = MeterMetrics()
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr("time.time", lambda: clock["t"])

    _tick_series(metrics, clock, MAC_A, ticks=5, read_hz=4.0)
    v = parse_exposition(metrics.render(), device=MAC_A)
    assert v["yunmu_device_tick_total"] == pytest.approx(5, abs=1)
    assert v["yunmu_sampling_gaps_total"] == 0
    assert v["yunmu_tick_interval_seconds"] == pytest.approx(1.0, abs=0.05)


def test_sampling_faster_than_the_tick_leaves_no_gap(monkeypatch):
    """Two probes per device update: every read lands within one hold."""
    metrics = MeterMetrics()
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr("time.time", lambda: clock["t"])

    _tick_series(metrics, clock, MAC_A, ticks=20, read_hz=2.0)
    v = parse_exposition(metrics.render(), device=MAC_A)
    assert v["yunmu_sampling_gaps_total"] == 0
    assert v["yunmu_max_read_gap_seconds"] == pytest.approx(0.5, abs=0.05)
    assert v["yunmu_device_tick_total"] == pytest.approx(20, abs=1)


def test_stale_values_are_not_counted_as_ticks(monkeypatch):
    """Repeated identical reads are one update, not many."""
    metrics = MeterMetrics()
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr("time.time", lambda: clock["t"])

    for _ in range(50):
        metrics.record_for(MAC_A, make_reading(power=4.5), 0.01)
        clock["t"] += 0.4

    v = parse_exposition(metrics.render(), device=MAC_A)
    assert v["yunmu_device_tick_total"] == 0
    assert v["yunmu_read_total"] == 50


def test_counter_change_alone_is_not_a_tick(monkeypatch):
    """The accumulator steps every 60 s and must not look like a device update."""
    metrics = MeterMetrics()
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr("time.time", lambda: clock["t"])

    for i in range(40):
        metrics.record_for(MAC_A, make_reading(counter=600 + i), 0.01)
        clock["t"] += 0.5

    v = parse_exposition(metrics.render(), device=MAC_A)
    assert v["yunmu_device_tick_total"] == 0


def test_a_gap_longer_than_one_hold_is_counted(monkeypatch):
    """A 5 s break in reads exceeds one device hold, so it is reported."""
    metrics = MeterMetrics()
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr("time.time", lambda: clock["t"])

    metrics.record_for(MAC_A, make_reading(power=1.0), 0.01)
    clock["t"] += 1.0
    metrics.record_for(MAC_A, make_reading(power=2.0), 0.01)
    clock["t"] += 5.0
    metrics.record_for(MAC_A, make_reading(power=3.0), 0.01)

    v = parse_exposition(metrics.render(), device=MAC_A)
    assert v["yunmu_device_tick_total"] == 2
    assert v["yunmu_sampling_gaps_total"] == 1
    assert v["yunmu_max_read_gap_seconds"] == pytest.approx(5.0, abs=0.01)


def test_sample_age_reports_staleness(monkeypatch):
    metrics = MeterMetrics()
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr("time.time", lambda: clock["t"])

    metrics.record_for(MAC_A, make_reading(), 0.01)
    clock["t"] += 3.5
    assert parse_exposition(metrics.render(), device=MAC_A)[
        "yunmu_sample_age_seconds"
    ] == pytest.approx(3.5, abs=0.01)
