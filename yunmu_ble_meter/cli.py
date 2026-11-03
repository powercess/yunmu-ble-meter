"""Command line entry point: poll one or more meters and serve metrics.

Device protocol parameters (the per-device read command and payload
de-obfuscation constant) can be supplied three ways, in increasing order of
convenience:

1. Built in — ``protocol.KNOWN_PROFILES`` ships the meters we have captured.
2. Config file — ``--config meters.toml``; this is how you add a meter without
   touching the package.
3. ``--mac`` — ad-hoc meters; they use the built-in profile for that MAC, or the
   generic defaults if unknown (which most meters will ignore silently).

``--learn`` lets an unknown meter work: the payload constant is recovered by
brute force against the first reply (see ``protocol.guess_payload_xor``).
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time

from . import protocol
from .metrics import MeterMetrics, collector_loop, multi_collector_loop


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="yunmu-meter",
        description="Read YunMu YM-S1-BES BLE meters and expose Prometheus metrics.",
    )
    ap.add_argument(
        "--mac",
        action="append",
        default=[],
        metavar="MAC",
        help="device MAC; repeat for several meters (e.g. --mac A --mac B)",
    )
    ap.add_argument(
        "--config",
        metavar="PATH",
        help="TOML file listing meters and their protocol parameters",
    )
    ap.add_argument("--interval", type=float, default=2.0, help="poll interval, seconds")
    ap.add_argument("--port", type=int, default=9103, help="exporter port")
    ap.add_argument("--host", default="0.0.0.0", help="exporter bind address")
    ap.add_argument(
        "--learn",
        action="store_true",
        help="recover the payload de-obfuscation constant for unknown meters",
    )
    ap.add_argument(
        "--print",
        dest="do_print",
        action="store_true",
        help="also print each reading to stdout",
    )
    return ap


def load_config(path: str) -> list[dict]:
    """Read a TOML config: each ``[[meter]]`` has mac/name/read_payload/payload_xor."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - py<3.11
        print("yunmu-meter: --config needs Python 3.11+ (tomllib)", file=sys.stderr)
        raise SystemExit(2)
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    meters = data.get("meter") or []
    if not isinstance(meters, list):
        raise SystemExit("config: expected [[meter]] entries")
    out = []
    for entry in meters:
        mac = str(entry.get("mac", "")).upper()
        if not mac:
            raise SystemExit("config: a [[meter]] entry is missing 'mac'")
        payload = entry.get("read_payload")
        if isinstance(payload, str):
            payload = bytes.fromhex(payload.replace(":", "").replace(" ", ""))
        out.append(
            {
                "mac": mac,
                "name": entry.get("name", ""),
                "read_payload": payload,
                "payload_xor": entry.get("payload_xor"),
            }
        )
    return out


def _install_profiles(entries: list[dict], learn: bool) -> None:
    """Merge config-supplied parameters into the in-process profile table."""
    for entry in entries:
        mac = entry["mac"]
        base = protocol.profile_for(mac)
        read_payload = entry.get("read_payload") or base.read_payload
        xor = entry.get("payload_xor")
        # sentinel: None means "unknown", 0 is a legitimate constant
        observed_xor = xor is not None and not learn
        protocol.KNOWN_PROFILES[mac] = protocol.DeviceProfile(
            mac=mac,
            read_payload=read_payload,
            payload_xor=base.payload_xor if xor is None else int(xor),
            observed_read_payload=entry.get("read_payload") is not None
            or base.observed_read_payload,
            observed_payload_xor=observed_xor or base.observed_payload_xor,
        )


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

    entries = load_config(args.config) if args.config else []
    for mac in args.mac:
        entries.append({"mac": mac.upper(), "name": "", "read_payload": None, "payload_xor": None})
    if not entries:
        print("yunmu-meter: give --mac or --config", file=sys.stderr)
        return 2

    _install_profiles(entries, args.learn)

    metrics = MeterMetrics()
    for entry in entries:
        metrics.register(entry["mac"], entry["name"] or entry["mac"])

    meters = [
        BleMeter(
            entry["mac"],
            reply_timeout=max(5.0, args.interval),
            learn_payload_xor=args.learn,
        )
        for entry in entries
    ]

    exporter = start_exporter(metrics, args.host, args.port)
    stop = threading.Event()

    def on_reading(mac: str, reading) -> None:  # noqa: ANN001
        print(
            f"{mac} {reading.voltage:6.1f} V {reading.power:6.1f} W "
            f"{reading.current:6.3f} A  PF {reading.power_factor:.2f}  "
            f"thr {reading.threshold}  counter {reading.counter}",
            flush=True,
        )

    def shutdown(*_args) -> None:
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(
        f"serving metrics on {exporter.url} ({len(meters)} meter"
        f"{'s' if len(meters) != 1 else ''})",
        flush=True,
    )
    print(f"dashboard on {exporter.dashboard_url}", flush=True)

    if len(meters) == 1:
        target = collector_loop
        worker = threading.Thread(
            target=target, args=(meters[0], metrics, args.interval, stop), daemon=True
        )
    else:
        worker = threading.Thread(
            target=multi_collector_loop,
            args=(meters, metrics, args.interval, stop),
            kwargs={"on_reading": on_reading if args.do_print else None},
            daemon=True,
        )
    worker.start()
    try:
        while not stop.is_set():
            time.sleep(0.5)
    finally:
        stop.set()
        exporter.stop()
        for meter in meters:
            try:
                meter.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
