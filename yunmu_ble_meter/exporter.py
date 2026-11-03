"""HTTP exporter: Prometheus metrics plus a small live dashboard.

``/metrics`` serves the Prometheus text exposition; ``/`` serves an HTML page
that renders the same data as a table refreshed by the browser. Both read from
the same :class:`~yunmu_ble_meter.metrics.MeterMetrics`, so there is no second
source of truth.
"""

from __future__ import annotations

import html
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .metrics import MeterMetrics

__all__ = ["start_exporter", "Exporter", "render_dashboard"]

_PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>YunMu 计量插座</title>
<style>
  :root { color-scheme: dark light; }
  body { font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
         margin: 0; padding: 24px; background: #0f1115; color: #e6e6e6; }
  h1 { font-size: 18px; font-weight: 600; margin: 0 0 4px; }
  .sub { color: #8b95a5; font-size: 12px; margin-bottom: 20px; }
  table { border-collapse: collapse; width: 100%; max-width: 1100px; }
  th, td { padding: 9px 12px; text-align: right; border-bottom: 1px solid #23262d;
           white-space: nowrap; }
  th { color: #8b95a5; font-weight: 500; font-size: 12px;
       text-transform: uppercase; letter-spacing: .04em; }
  th:first-child, td:first-child { text-align: left; }
  td.mono { font-variant-numeric: tabular-nums; }
  .off { color: #ff6b6b; }
  .on { color: #4ade80; }
  .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%;
         margin-right: 6px; vertical-align: middle; }
  .err { color: #ff6b6b; font-size: 12px; margin-top: 16px; white-space: pre-wrap; }
  .empty { color: #8b95a5; padding: 24px 0; }
  footer { color: #5d6470; font-size: 11px; margin-top: 24px; }
</style>
</head>
<body>
<h1>YunMu 计量插座</h1>
<div class="sub">每 2 秒刷新 &middot; <span id="count">0</span> 台设备 &middot;
  <a href="/metrics" style="color:#6ea8fe">Prometheus /metrics</a></div>
<div id="app"></div>
<footer>数据来源：树莓派 Bluetooth LE 直连读取</footer>
<script>
const COLS = [
  ["voltage", "电压", "V", 1],
  ["current", "电流", "A", 3],
  ["power", "功率", "W", 1],
  ["pf", "功率因数", "", 2],
  ["threshold", "阈值", "W", 0],
  ["counter", "计数", "", 0],
  ["energy", "积分电能", "kWh", 3],
];
function fmt(v, d) {
  if (v === null || v === undefined) return "—";
  return d === 0 ? String(Math.round(v)) : Number(v).toFixed(d);
}
function render(data) {
  const app = document.getElementById("app");
  document.getElementById("count").textContent = data.devices.length;
  if (!data.devices.length) {
    app.innerHTML = '<div class="empty">还没有采集到数据。</div>';
    return;
  }
  let h = "<table><thead><tr><th>设备</th><th>状态</th>";
  for (const [, label, unit] of COLS) h += `<th>${label}${unit ? " (" + unit + ")" : ""}</th>`;
  h += "<th>最后更新</th></tr></thead><tbody>";
  for (const d of data.devices) {
    const up = d.ok;
    h += `<tr><td>${d.label}<div style="color:#5d6470;font-size:11px">${d.mac}</div></td>`;
    h += `<td class="${up ? "on" : "off"}"><span class="dot" style="background:currentColor"></span>${up ? "正常" : "离线"}</td>`;
    for (const [key, , , digits] of COLS) h += `<td class="mono">${fmt(d[key], digits)}</td>`;
    h += `<td class="mono" style="color:#8b95a5">${d.age === null ? "—" : d.age + "s 前"}</td></tr>`;
  }
  h += "</tbody></table>";
  const errs = data.devices.filter(d => d.error);
  if (errs.length) {
    h += '<div class="err">' + errs.map(d => d.label + ": " + d.error).join("\n") + "</div>";
  }
  app.innerHTML = h;
}
async function tick() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    render(await r.json());
  } catch (e) { /* keep last render */ }
}
tick();
setInterval(tick, 2000);
</script>
</body>
</html>
"""


def _state_payload(metrics: MeterMetrics) -> dict:
    now = time.time()
    out = []
    for state in metrics.devices:
        values = dict(state.values)
        out.append(
            {
                "mac": state.mac,
                "label": state.label,
                "ok": state.scrape_ok >= 1.0,
                "voltage": values.get("yunmu_voltage_volts"),
                "current": values.get("yunmu_current_amperes"),
                "power": values.get("yunmu_power_watts"),
                "pf": values.get("yunmu_power_factor"),
                "threshold": values.get("yunmu_threshold_watts"),
                "counter": values.get("yunmu_counter_total"),
                "energy": state.energy_kwh,
                "age": None if not state.last_ts else round(now - state.last_ts, 1),
                "error": state.last_error or "",
            }
        )
    out.sort(key=lambda d: d["label"])
    return {"devices": out}


def render_dashboard(metrics: MeterMetrics) -> str:
    """Return the dashboard HTML. Data is fetched by the page itself."""
    return _PAGE


class _Handler(BaseHTTPRequestHandler):
    metrics: MeterMetrics

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?")[0]
        if path == "/metrics":
            body = self.metrics.render().encode()
            self._send(200, "text/plain; version=0.0.4; charset=utf-8", body)
        elif path == "/api/state":
            body = json.dumps(_state_payload(self.metrics), ensure_ascii=False).encode()
            self._send(200, "application/json; charset=utf-8", body)
        elif path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", render_dashboard(self.metrics).encode())
        else:
            self.send_error(404)

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


class Exporter:
    """A Prometheus scrape endpoint bound to ``host:port``."""

    def __init__(self, metrics: MeterMetrics, host: str = "0.0.0.0", port: int = 9103) -> None:
        self.metrics = metrics
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler = type("_BoundHandler", (_Handler,), {"metrics": self.metrics})
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="yunmu-exporter", daemon=True
        )
        self._thread.start()

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{host}:{self.port}/metrics"

    @property
    def dashboard_url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{host}:{self.port}/"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def start_exporter(metrics: MeterMetrics, host: str = "0.0.0.0", port: int = 9103) -> Exporter:
    """Convenience wrapper around :class:`Exporter`."""
    exporter = Exporter(metrics, host, port)
    exporter.start()
    return exporter
