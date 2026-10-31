"""Command line entry point: poll one meter and serve Prometheus metrics."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time

from .metrics import MeterMetrics, collector_loop


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="yunmu-meter",
        description="Read a YunMu YM-S1-BES BLE meter and expose Prometheus metrics.",
    )
    ap.add_argument("--mac", required=True, help="device MAC, e.g. 26:04:18:01:12:F4")
    ap.add_argument("--interval", type=float, default=2.0, help="poll interval, seconds")
    ap.add_argument("--port", type=int, default=9103, help="exporter port")
    ap.add_argument("--host", default="0.0.0.0", help="exporter bind address")
    ap.add_argument(
        "--print",
        dest="do_print",
        action="store_true",
        help="also print each reading to stdout",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        from .exporter import start_exporter
        from .transport import BleMeter, DeviceNotFound
    except ImportError as exc:
        print(
            "yunmu-ble-meter: the BLE transport needs dbus-python and PyGObject.\n"
            "Install them with: sudo apt install python3-dbus python3-gi\n"
            f"({exc})",
            file=sys.stderr,
        )
        return 2

    meter = BleMeter(args.mac, reply_timeout=max(5.0, args.interval))
    metrics = MeterMetrics()
    exporter = start_exporter(metrics, args.host, args.port)
    stop = threading.Event()

    if args.do_print:
        original_record = metrics.record

        def record_and_print(reading, duration):  # type: ignore[no-redef]
            original_record(reading, duration)
            print(
                f"{reading.voltage:6.1f} V {reading.power:6.1f} W "
                f"{reading.current:6.3f} A  PF {reading.power_factor:.2f}  "
                f"counter {reading.counter}",
                flush=True,
            )

        metrics.record = record_and_print  # type: ignore[method-assign]

    def shutdown(*_args) -> None:
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"serving metrics on {exporter.url}", flush=True)
    worker = threading.Thread(
        target=collector_loop, args=(meter, metrics, args.interval, stop), daemon=True
    )
    worker.start()
    try:
        while not stop.is_set():
            time.sleep(0.5)
    finally:
        stop.set()
        exporter.stop()
        meter.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
