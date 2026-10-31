"""HTTP exporter exposing meter metrics for Prometheus to scrape."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .metrics import MeterMetrics

__all__ = ["start_exporter", "Exporter"]


class _Handler(BaseHTTPRequestHandler):
    metrics: MeterMetrics

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path.split("?")[0] not in ("/metrics", "/"):
            self.send_error(404)
            return
        body = self.metrics.render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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
