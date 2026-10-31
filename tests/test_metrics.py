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


def parse_exposition(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition(" ")
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
