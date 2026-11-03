"""Tests for the HTTP exporter: routing, JSON payload, dashboard integrity."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from yunmu_ble_meter.exporter import Exporter, _state_payload, render_dashboard
from yunmu_ble_meter.metrics import MeterMetrics
from yunmu_ble_meter.transport_shim import Reading


def make_reading(**overrides) -> Reading:
    values = dict(
        counter=320,
        voltage=230.4,
        power=4.5,
        current=0.054,
        power_factor=0.36,
        threshold=2000,
        raw=b"",
    )
    values.update(overrides)
    return Reading(**values)


MAC = "26:04:18:01:12:F4"


def test_state_payload_shape():
    metrics = MeterMetrics()
    metrics.register(MAC, "1号机")
    metrics.record_for(MAC, make_reading(), 0.01)
    payload = _state_payload(metrics)
    assert len(payload["devices"]) == 1
    dev = payload["devices"][0]
    assert dev["mac"] == MAC
    assert dev["label"] == "1号机"
    assert dev["ok"] is True
    assert dev["voltage"] == pytest.approx(230.4)
    assert dev["error"] == ""


def test_state_payload_includes_failures():
    metrics = MeterMetrics()
    metrics.register(MAC, "1号机")
    metrics.record_failure_for(MAC, 0.5, "boom")
    dev = _state_payload(metrics)["devices"][0]
    assert dev["ok"] is False
    assert dev["error"] == "boom"
    assert dev["voltage"] is None


def test_dashboard_embeds_valid_javascript():
    """The page's inline script must parse.

    A stray newline inside a JS string literal is invisible in the HTML but
    kills the whole script, leaving a blank dashboard.
    """
    html = render_dashboard(MeterMetrics())
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts, "dashboard has no inline script"
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available to syntax-check the script")
    for i, src in enumerate(scripts):
        proc = subprocess.run(
            [node, "--check", "-"],
            input=src,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"script {i} is invalid: {proc.stderr}"


def test_dashboard_has_dashboard_container():
    html = render_dashboard(MeterMetrics())
    assert 'id="app"' in html
    assert "/api/state" in html


def test_exporter_routes(tmp_path):
    metrics = MeterMetrics()
    metrics.register(MAC, "1号机")
    metrics.record_for(MAC, make_reading(), 0.01)

    import urllib.request

    exporter = Exporter(metrics, host="127.0.0.1", port=0)
    exporter.start()
    try:
        port = exporter._server.server_address[1]
        base = f"http://127.0.0.1:{port}"

        with urllib.request.urlopen(f"{base}/metrics") as r:
            assert r.status == 200
            body = r.read().decode()
            assert 'yunmu_voltage_volts{device="1号机"}' in body

        with urllib.request.urlopen(f"{base}/api/state") as r:
            assert r.status == 200
            data = json.loads(r.read().decode())
            assert data["devices"][0]["voltage"] == pytest.approx(230.4)

        with urllib.request.urlopen(f"{base}/") as r:
            assert r.status == 200
            assert "/api/state" in r.read().decode()

        with pytest.raises(Exception):
            urllib.request.urlopen(f"{base}/nope")
    finally:
        exporter.stop()
