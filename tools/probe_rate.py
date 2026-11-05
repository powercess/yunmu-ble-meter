"""Measure how fast a YunMu YM-S1-BES can actually be read, and how often it
refreshes the values it reports.

The repository claims "the device itself updates roughly once a second"
(``deploy/README.md``). That claim was never pinned down, and it is the ceiling
that makes polling faster pointless. This tool measures it directly, and
separates three questions that are easy to confuse:

1. **Poll ceiling** -- how many transactions per second the link can carry at
   all. A function of the BLE connection interval, MTU and the firmware's reply
   path, not of the measurement rate.
2. **Native push rate** -- whether the device sends notifications on its own
   once subscribed, with no request from us.
3. **Novelty rate** -- how often the *values* actually change while polling as
   fast as possible. Under a steady load the readings are quantised, so values
   can legitimately repeat; the gap histogram distinguishes "firmware is slow"
   from "value is unchanged because nothing happened".

Run it on the host that has the Bluetooth adapter::

    python -m tools.probe_rate --mac 26:04:18:01:12:F4
    python -m tools.probe_rate --mac ... --listen 60 --ceiling 30
    python -m tools.probe_rate --mac ... --selftest      # no radio needed

Needs dbus-python and PyGObject, same as the transport.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass, field

__all__ = ["Read", "analyze", "format_report", "probe", "synthetic", "main"]


# ------------------------------------------------------------------ records


@dataclass
class Read:
    """One request/response round trip."""

    t_send: float = 0.0
    t_recv: float = 0.0
    values: tuple = ()
    counter: int = 0
    retries: int = 0

    @property
    def latency(self) -> float:
        return self.t_recv - self.t_send


@dataclass
class Analysis:
    """Derived numbers. All rates are per second."""

    n: int = 0
    span: float = 0.0
    poll_hz: float = 0.0
    latency_p50: float = 0.0
    latency_p90: float = 0.0
    latency_p99: float = 0.0
    latency_min: float = 0.0
    latency_max: float = 0.0
    retries: int = 0
    changes: int = 0
    novelty_hz: float = 0.0
    min_change_gap: float | None = None
    median_change_gap: float | None = None
    gap_buckets: dict[str, int] = field(default_factory=dict)
    counter_start: int = 0
    counter_end: int = 0
    distinct_values: int = 0
    implied_firmware_hz: float | None = None


def _bucket(gap: float) -> str:
    edges = [0.02, 0.05, 0.1, 0.2, 0.5, 0.9, 1.1, 1.9, 2.1, 5.0]
    for edge in edges:
        if gap < edge:
            return f"<{edge:g}s"
    return f">={edges[-1]:g}s"


def analyze(reads: list[Read]) -> Analysis:
    """Turn a list of round trips into rates. Pure: no radio, no clock reads."""
    a = Analysis(n=len(reads))
    if not reads:
        return a

    a.span = reads[-1].t_recv - reads[0].t_send
    a.poll_hz = (len(reads) - 1) / a.span if a.span > 0 else 0.0

    lat = sorted(r.latency for r in reads)
    pick = lambda p: lat[min(len(lat) - 1, int(round(p * (len(lat) - 1))))]
    a.latency_min, a.latency_p50, a.latency_p90, a.latency_p99 = (
        lat[0],
        pick(0.50),
        pick(0.90),
        pick(0.99),
    )
    a.latency_max = lat[-1]
    a.retries = sum(r.retries for r in reads)

    # Transitions of the reported tuple == samples the firmware actually produced.
    gaps: list[float] = []
    prev = None
    prev_t = 0.0
    seen: set[tuple] = set()
    for r in reads:
        seen.add(r.values)
        if prev is not None and r.values != prev:
            a.changes += 1
            gaps.append(r.t_recv - prev_t)
        if prev is None or r.values != prev:
            prev, prev_t = r.values, r.t_recv
    a.distinct_values = len(seen)
    a.novelty_hz = a.changes / a.span if a.span > 0 else 0.0

    for g in gaps:
        key = _bucket(g)
        a.gap_buckets[key] = a.gap_buckets.get(key, 0) + 1
    if gaps:
        a.min_change_gap = min(gaps)
        a.median_change_gap = statistics.median(gaps)

    a.counter_start = reads[0].counter
    a.counter_end = reads[-1].counter

    # If value transitions cluster at a period larger than the poll period, the
    # firmware is the limit; the median gap is the best estimator of it.
    if a.median_change_gap and a.median_change_gap > 0:
        a.implied_firmware_hz = 1.0 / a.median_change_gap
    return a


# ------------------------------------------------------------------- radio


def _pump_until_frame(meter, timeout: float, granularity: float = 0.001):
    ctx = meter._loop.get_context()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        ctx.iteration(False)
        if meter._frames:
            return time.monotonic()
        time.sleep(granularity)
    return None


def _write_when_ready(meter, frame: bytes, settle: float) -> int:
    """Issue one GATT write, waiting for the link to accept it. Returns retries."""
    retries = 0
    deadline = time.monotonic() + settle
    while time.monotonic() < deadline and not meter._write_ready():
        meter._pump(0.01)
    for attempt in range(4):
        try:
            meter._write(frame)
            return retries
        except Exception:
            retries += 1
            if attempt == 3:
                raise
            meter._pump(0.15 * (attempt + 1))
    return retries


def phase_idle_listen(meter, seconds: float):
    """Listen without sending anything: does the device push on its own?"""
    from yunmu_ble_meter import protocol

    stamps: list[float] = []
    ctx = meter._loop.get_context()
    end = time.monotonic() + seconds
    seen = 0
    while time.monotonic() < end:
        ctx.iteration(False)
        if len(meter._frames) > seen:
            while seen < len(meter._frames):
                stamps.append(time.monotonic())
                seen += 1
        time.sleep(0.001)

    types = []
    for raw in meter._frames:
        try:
            types.append(protocol.parse_response(raw, meter.mac, meter.profile.payload_xor).type)
        except protocol.DecodeError:
            types.append("undecodable")
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    return {
        "seconds": seconds,
        "frames": len(stamps),
        "hz": len(stamps) / seconds if seconds else 0.0,
        "types": types,
        "median_gap": statistics.median(gaps) if gaps else None,
    }


def phase_reads(meter, count: int, timeout: float) -> list[Read]:
    """Back-to-back reads with no sleep: the poll ceiling under this interval."""
    from yunmu_ble_meter import protocol

    frame = protocol.build_read(meter.mac, meter.profile.read_payload)
    out: list[Read] = []
    for _ in range(count):
        meter._frames.clear()
        meter._buffer.clear()
        t_send = time.monotonic()
        retries = _write_when_ready(meter, frame, meter.write_settle)
        t_recv = _pump_until_frame(meter, timeout)
        if t_recv is None:
            break
        reply = meter._frames[0]
        resp = protocol.parse_response(reply, meter.mac, meter.profile.payload_xor)
        out.append(
            Read(
                t_send=t_send,
                t_recv=t_recv,
                values=(
                    resp.voltage,
                    resp.power,
                    resp.current,
                    resp.power_factor,
                    resp.threshold,
                ),
                counter=resp.counter,
                retries=retries,
            )
        )
    return out


def phase_pipeline(meter, timeout: float) -> dict:
    """Send two reads back to back: does the firmware queue them or drop one?"""
    from yunmu_ble_meter import protocol

    frame = protocol.build_read(meter.mac, meter.profile.read_payload)
    meter._frames.clear()
    meter._buffer.clear()
    t0 = time.monotonic()
    _write_when_ready(meter, frame, meter.write_settle)
    _write_when_ready(meter, frame, meter.write_settle)
    end = time.monotonic() + timeout
    ctx = meter._loop.get_context()
    while time.monotonic() < end and len(meter._frames) < 2:
        ctx.iteration(False)
        time.sleep(0.001)
    return {"writes": 2, "replies": len(meter._frames), "window": time.monotonic() - t0}


def probe(mac: str, *, listen: float, reads: int, timeout: float, learn: bool) -> dict:
    from yunmu_ble_meter.transport import BleMeter

    meter = BleMeter(mac, learn_payload_xor=learn, reply_timeout=timeout)
    out: dict = {}
    try:
        meter.connect()
        out["profile"] = {
            "read_payload": meter.profile.read_payload.hex(),
            "payload_xor": f"0x{meter.profile.payload_xor:02X}",
        }
        if listen > 0:
            out["idle"] = phase_idle_listen(meter, listen)
        recs = phase_reads(meter, reads, timeout)
        out["analysis"] = analyze(recs)
        out["pipeline"] = phase_pipeline(meter, timeout)
        out["samples"] = [
            {
                "latency_ms": round(r.latency * 1000, 1),
                "v": r.values[0],
                "w": r.values[1],
                "a": r.values[2],
                "pf": r.values[3],
                "cnt": r.counter,
            }
            for r in recs
        ]
    finally:
        try:
            meter.close()
        except Exception:
            pass
    return out


# ------------------------------------------------------------------ reporting


def format_report(res: dict) -> str:
    a: Analysis = res.get("analysis") or Analysis()
    lines = ["YunMu YM-S1-BES rate probe", ""]

    prof = res.get("profile")
    if prof:
        lines.append(f"read_payload  {prof['read_payload']}   payload_xor {prof['payload_xor']}")

    idle = res.get("idle")
    if idle:
        lines += [
            "",
            f"[1] native push, {idle['seconds']:.0f}s with no request",
            f"    frames   {idle['frames']}  ({idle['hz']:.2f}/s)"
            + (f"   median gap {idle['median_gap']:.2f}s" if idle["median_gap"] else ""),
        ]
        if idle["frames"] == 0:
            lines.append("    -> request/response only: the device never streams on its own")
        else:
            lines.append(f"    types    {idle['types'][:12]}")

    lines += [
        "",
        f"[2] poll ceiling, {a.n} back-to-back reads over {a.span:.2f}s",
        f"    rate     {a.poll_hz:.1f} reads/s",
        f"    latency  min {a.latency_min*1000:.1f}  p50 {a.latency_p50*1000:.1f}"
        f"  p90 {a.latency_p90*1000:.1f}  p99 {a.latency_p99*1000:.1f}"
        f"  max {a.latency_max*1000:.1f} ms",
    ]
    if a.retries:
        lines.append(f"    writes   {a.retries} had to be retried (link not ready)")

    lines += [
        "",
        f"[3] value novelty, same window",
        f"    distinct reported tuples  {a.distinct_values}",
        f"    transitions               {a.changes}  ({a.novelty_hz:.2f}/s)",
    ]
    if a.median_change_gap:
        lines.append(
            f"    change gap                min {a.min_change_gap:.3f}s"
            f"  median {a.median_change_gap:.3f}s"
            f"  -> firmware ~{a.implied_firmware_hz:.2f} Hz"
        )
        buckets = "  ".join(f"{k}:{v}" for k, v in sorted(a.gap_buckets.items()))
        lines.append(f"    gap histogram             {buckets}")
    else:
        lines.append("    change gap                no transition observed: load was static")

    lines += [
        "",
        f"    counter                   {a.counter_start} -> {a.counter_end}"
        f"  (+{a.counter_end - a.counter_start})",
    ]

    pipe = res.get("pipeline")
    if pipe:
        lines.append(
            f"    pipelining                2 writes -> {pipe['replies']} replies"
            f" in {pipe['window']*1000:.0f} ms"
        )

    lines += ["", "Reading the result:"]
    if a.median_change_gap and a.poll_hz > 0:
        ratio = a.poll_hz * a.median_change_gap
        if ratio > 1.5:
            lines.append(
                f"  You are polling {ratio:.1f}x faster than the values change."
                " Everything above the change rate is a duplicate read."
            )
        else:
            lines.append("  Poll rate and update rate are the same order: the link is the limit.")
    lines.append(
        "  A gap histogram clustered at one period means the firmware runs at that period;"
        " gaps scattered across small buckets under a varying load mean the firmware is"
        " faster than the quantisation of the reported fields."
    )
    return "\n".join(lines)


# ------------------------------------------------------------------ selftest


def synthetic(period: float, poll_hz: float, jitter: float = 0.004) -> list[Read]:
    """Build a read series whose values change once per ``period`` seconds."""
    import random

    rnd = random.Random(7)
    reads: list[Read] = []
    t = 0.0
    n = int(20 * poll_hz)
    for i in range(n):
        t += 1.0 / poll_hz
        step = int(t / period)
        reads.append(
            Read(
                t_send=t,
                t_recv=t + 0.03 + rnd.random() * jitter,
                values=(2304, 45 + step, 54, 36, 2000),
                counter=607 + step,
            )
        )
    return reads


def selftest() -> int:
    print("selftest: firmware 1 Hz, poll 20 Hz")
    a = analyze(synthetic(1.0, 20.0))
    print(format_report({"analysis": a}))
    print()
    print("selftest: firmware 5 Hz, poll 20 Hz")
    b = analyze(synthetic(0.2, 20.0))
    print(
        f"  implied firmware {b.implied_firmware_hz:.2f} Hz (expect ~5.00),"
        f" novelty {b.novelty_hz:.2f}/s, poll {b.poll_hz:.1f}/s"
    )
    ok = abs(b.implied_firmware_hz - 5.0) < 0.6 and abs(b.poll_hz - 20.0) < 1.0
    print("  PASS" if ok else "  FAIL")
    return 0 if ok else 1


# ---------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="probe_rate",
        description="Measure the read ceiling and the value refresh rate of a YunMu meter.",
    )
    ap.add_argument("--mac", help="device MAC")
    ap.add_argument("--listen", type=float, default=30.0,
                    help="seconds to listen without sending (0 to skip)")
    ap.add_argument("--reads", type=int, default=400, help="back-to-back reads to attempt")
    ap.add_argument("--timeout", type=float, default=2.0, help="per-read reply timeout")
    ap.add_argument("--learn", action="store_true", help="recover payload_xor from the reply")
    ap.add_argument("--json", action="store_true", help="dump raw measurements as JSON")
    ap.add_argument("--selftest", action="store_true", help="run without a radio")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.mac:
        print("probe_rate: --mac is required (or use --selftest)", file=sys.stderr)
        return 2

    res = probe(
        args.mac,
        listen=args.listen,
        reads=args.reads,
        timeout=args.timeout,
        learn=args.learn,
    )
    if args.json:
        import json

        a = res.get("analysis")
        if a is not None:
            res["analysis"] = a.__dict__
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(res))
    print()
    print("Note: run it twice -- once at idle, once while toggling a known load --")
    print("because a static load produces no value transitions to time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
