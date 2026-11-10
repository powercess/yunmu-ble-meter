#!/usr/bin/env python3
"""Learn a meter's per-device read payload and response XOR by brute force.

The vendor cloud issues a fixed 3-byte read command per meter. Four meters
captured so far all follow the shape ``[s, s ^ 0x08, s]``, so only 256
candidates exist for the unknown byte ``s``.  For each candidate the read frame
is sent; the correct one is the only one that produces a reply whose frame CRC
validates (CRC is MAC-derived, independent of the payload).

Once a reply is captured, the response XOR constant is recovered offline by
``protocol.guess_payload_xor`` (checks reply type and inner checksum).

The tool takes exclusive use of the adapter: stop the exporter first
(``systemctl stop yunmu-meter``), run it, then start the exporter again.
"""

from __future__ import annotations

import argparse
import sys
import time

from yunmu_ble_meter import protocol
from yunmu_ble_meter.transport import BleMeter, DeviceNotFound, TransportError


def try_candidate(meter: BleMeter, mac: str, s: int, timeout: float):
    """Send one candidate; return the reply XOR guess or None."""
    frame = protocol.build_frame(bytes([s, s ^ 0x08, s]), mac)
    try:
        reply = meter.transact(frame, timeout)
    except TransportError:
        return None
    try:
        parsed = protocol.parse_frame(reply, mac)
    except protocol.DecodeError:
        return None
    return protocol.guess_payload_xor(parsed.payload)


def learn(mac: str, *, timeout: float, settle: float, scan_timeout: float) -> int:
    meter = BleMeter(mac, write_settle=settle, reply_timeout=timeout)
    print(f"connecting to {mac} ...", flush=True)
    meter.connect(scan_timeout=scan_timeout)
    try:
        started = time.monotonic()
        for s in range(256):
            xor = try_candidate(meter, mac, s, timeout)
            if xor is None:
                if s % 16 == 0:
                    print(f"  ... {s}/256 ({time.monotonic() - started:.0f}s)", flush=True)
                continue
            # A late notification from the previous candidate could be mistaken
            # for a reply; require the same candidate to succeed twice.
            confirm = try_candidate(meter, mac, s, timeout)
            if confirm != xor:
                print(f"  candidate 0x{s:02X} looked valid but did not confirm; continuing",
                      flush=True)
                continue
            elapsed = time.monotonic() - started
            print(f"FOUND payload=0x{s:02X} (wire {s:02x} {s ^ 0x08:02x} {s:02x}) "
                  f"payload_xor=0x{xor:02X} in {elapsed:.0f}s", flush=True)
            print(f"\nTOML:\n[[meter]]\nmac = \"{mac}\"\n"
                  f"read_payload = \"{s:02x}{s ^ 0x08:02x}{s:02x}\"\n"
                  f"payload_xor = 0x{xor:02X}", flush=True)
            return 0
        print("no candidate produced a valid reply", flush=True)
        return 1
    finally:
        meter.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mac", required=True)
    ap.add_argument("--timeout", type=float, default=0.5,
                    help="reply wait per candidate, seconds (default 0.5)")
    ap.add_argument("--settle", type=float, default=0.15,
                    help="write settle per candidate, seconds (default 0.15)")
    ap.add_argument("--scan-timeout", type=float, default=30.0)
    args = ap.parse_args(argv)
    try:
        return learn(args.mac.upper(), timeout=args.timeout, settle=args.settle,
                     scan_timeout=args.scan_timeout)
    except DeviceNotFound as exc:
        print(f"device not found: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
