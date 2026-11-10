# yunmu-ble-meter

Reverse-engineered BLE protocol, client, and Prometheus exporter for the
**YunMu (云牧) YM-S1-BES** BLE metering socket — the device sold as
「蓝牙计量插座」 through the 控控乐 WeChat mini-program.

The vendor ships no local API: the mini-program fetches every command as an
opaque hex blob from the cloud and merely relays the bytes. This library
reimplements the wire protocol so the meter can be read directly over BLE,
without the phone, the app, or the vendor's servers.

```
$ yunmu-meter --mac 26:04:18:01:12:F4
224.7 V   2.9 W   0.032 A   PF 0.40   counter 43
```

## What it does

- **Protocol codec** — framing, MAC-derived obfuscation, CRC16, thresholds.
  Pure stdlib, importable anywhere.
- **BLE client** — talks to BlueZ over D-Bus; connects, polls, and releases the
  link so the phone app can be used again.
- **Prometheus exporter** — `/metrics` endpoint with live readings plus a
  locally integrated energy counter, so energy accounting does not depend on the
  device's own accumulator.

## Rust implementation (edge)

`rust/` contains a single-binary replacement for the Python collector, aimed at
edge devices: BlueZ via zbus, a current-thread tokio runtime, a hand-written
Prometheus endpoint and a minimal TOML parser. No Python runtime, no D-Bus
bindings, no HTTP framework. Build with `cargo build --release`; see
[`rust/README.md`](rust/README.md) and `deploy/yunmu-meter-rs.service`.

MCU sizing for the embedded port (ESP32-S3 probe, projections to C3/C2/H2):
[`docs/esp32-sizing.md`](docs/esp32-sizing.md).

Resource measurement helper: `tools/measure_resources.sh PID SECONDS`; measured
comparison (RSS 28.5 MB → 2.96 MB, CPU 10.4 % → 1.53 %) in
[`docs/benchmark.md`](docs/benchmark.md).

## Cloud path (reverse engineered)

The vendor cloud was also reversed from captured traffic, including the
request/response encryption:

- `docs/cloud-api.md` — REST/plugin endpoints, AES-CBC envelope, `code` →
  BLE payload encoding, response upload/decoding, signatures.
- `docs/payload-derivation.md` — dataset and the (negative) search for the
  per-device payload constant; it appears to be a factory secret, not derived
  from SN/MAC.
- `scripts/cloud_crypto.py` — encrypt/decrypt a captured `bleCommandV1` body
  (credentials from `YUNMU_API_KEY` / `YUNMU_API_SECRET`).
- `scripts/search_payload_rule.py` — brute-force harness used for the search.

## What it does not do

- **Switch the relay.** This model exposes no on/off control through the app and
  no command was ever observed. There is nothing to replay.
- **Reset the device's accumulator.** The clear command is accepted but not
  observed to zero the counter (see *Known gaps*).
- **Read cloud-side totals.** Cumulative cost and runtime live in the vendor's
  database, not on the device.
- **Write arbitrary payloads.** The firmware only accepts the handful of
  commands its cloud issues, and those commands are **per-device**: each meter
  has its own read payload and its own payload de-obfuscation constant. A frame
  built with another meter's values is dropped silently, with no error.

## Install

The transport needs the distro's D-Bus bindings (they do not build cleanly in a
plain virtualenv):

```bash
sudo apt install -y python3-dbus python3-gi bluez
pip install yunmu-ble-meter          # or: pip install -e .
```

Requires a working local Bluetooth adapter with the device in range.

## Use as a library

```python
from yunmu_ble_meter import BleMeter

with BleMeter("26:04:18:01:12:F4") as meter:
    r = meter.read()
    print(f"{r.voltage} V  {r.power} W  {r.current} A  PF {r.power_factor}")
```

Lower-level access, with no BLE involved:

```python
from yunmu_ble_meter import protocol

frame = protocol.build_read("26:04:18:01:12:F4")
assert frame.hex() == "a5a4494aede5edca3b"

resp = protocol.parse_response(wire_bytes, "26:04:18:01:12:F4")
print(resp.voltage, resp.power, resp.counter)
```

## Run the exporter

```bash
yunmu-meter --mac 26:04:18:01:12:F4 --interval 2 --port 9103
curl -s localhost:9103/metrics | grep yunmu_
```

```yaml
# prometheus.yml
scrape_configs:
  - job_name: yunmu-meter
    scrape_interval: 5s
    static_configs:
      - targets: ["raspi.local:9103"]
```

### Per-device parameters

Most of the wire format is derived from the MAC, but two values are issued by
the vendor cloud per device and cannot be computed locally:

| value | meaning |
|---|---|
| `read_payload` | 3-byte payload of the read request |
| `payload_xor` | constant that de-obfuscates the response payload |

Captured values live in `protocol.KNOWN_PROFILES`; use `protocol.profile_for(mac)`
for a defaulted profile. `BleMeter` applies them automatically.

A wrong `payload_xor` still passes the frame CRC — only the logical fields turn
to garbage — so it cannot be detected from the CRC alone. See
[`tools/fake_meter`](tools/fake_meter/README.md) for how to capture both values
for a new meter.

### Metrics

| Metric | Type | Meaning |
|---|---|---|
| `yunmu_voltage_volts` | gauge | Line voltage |
| `yunmu_current_amperes` | gauge | Load current |
| `yunmu_power_watts` | gauge | Active power |
| `yunmu_power_factor` | gauge | Power factor (0..1) |
| `yunmu_threshold_watts` | gauge | Configured alarm threshold |
| `yunmu_counter_total` | gauge | Raw 16-bit device accumulator |
| `yunmu_energy_kwh` | counter | Energy integrated locally since start |
| `yunmu_energy_device_kwh` | counter | Energy the meter reports for itself |
| `yunmu_scrape_success` | gauge | 1 if the last read succeeded |
| `yunmu_last_read_timestamp_seconds` | gauge | Unix time of last good read |
| `yunmu_read_duration_seconds` | gauge | Duration of the last read |
| `yunmu_sample_age_seconds` | gauge | Seconds since the last good read |
| `yunmu_read_total` | counter | Successful reads so far |
| `yunmu_device_tick_total` | counter | Device updates observed (value changes) |
| `yunmu_sampling_gaps_total` | counter | Reads that came >1 s after the previous read |
| `yunmu_tick_interval_seconds` | gauge | Smoothed gap between observed updates |
| `yunmu_max_read_gap_seconds` | gauge | Longest interval between two reads since start |

`yunmu_counter_total` wraps at 65536 and is reset by `clear`, so it is a gauge,
not a counter. For long-horizon accounting use `yunmu_energy_kwh`.

`yunmu_device_tick_total` counts device updates actually observed; at 2.5 reads/s
against a 1 Hz device it should advance at ~1/s. It can only be non-zero while
the readings change, so a meter on a constant load reports a flat counter even
when sampling is perfect — do not alert on it.

`yunmu_sampling_gaps_total` is the load-independent health signal: it counts
reads that arrived more than one device hold (1 s) after the previous read, which
is the only window in which a published record can be replaced unread. It should
stay flat. `yunmu_max_read_gap_seconds` is the worst such interval. Alert on
`rate(yunmu_sampling_gaps_total[10m]) > 0`.

## Multiple meters

One process polls any number of meters round-robin from a single adapter:

```bash
yunmu-meter --config /etc/yunmu-meter/meters.toml --interval 0.8
```

Each meter gets its own labelled series, so Prometheus sees the whole fleet:

```
yunmu_voltage_volts{device="1号机"} 232.3
yunmu_voltage_volts{device="2号机"} 231.8
yunmu_power_watts{device="2号机"} 6.5
```

The dashboard at `/` shows the same data as a live table; `/metrics` is the
Prometheus endpoint and `/api/state` the JSON behind the page.

### Adding a meter

Copy `deploy/meters.example.toml` and add a `[[meter]]` block:

```toml
[[meter]]
mac = "26:01:15:04:2F:96"
name = "2号机"
read_payload = "5a525a"
payload_xor = 0x0D
```

Both protocol values are captured per device — see
[`tools/fake_meter`](tools/fake_meter/README.md). If you only know the read
command, run with `--learn` and the payload constant is recovered from the first
reply automatically (it is cached in memory, so put it in the config once known).

`BleMeter` isolates each device: notifications are filtered by object path and
GATT characteristics are looked up inside the device's own subtree, so several
collectors can share one adapter. Links are serialised internally because the
controller refuses a new link while the previous one is tearing down.

### Unknown meter: brute-force the two constants

`tools/learn_meter.py` recovers both per-device values over BLE when no capture
is available. The read payload is always `[s, s ^ 0x08, s]`, so only 256
candidates exist; the correct one is the only candidate whose reply frame passes
the MAC-derived CRC. The response XOR is then recovered from that reply.

```bash
sudo systemctl stop yunmu-meter        # exclusive use of the adapter
PYTHONPATH=/opt/yunmu-ble-meter python3 tools/learn_meter.py \
    --mac 26:01:15:04:2F:96 --timeout 0.7 --settle 0.2
sudo systemctl start yunmu-meter
```

The tool prints the matching `[[meter]]` block. Measured: 64 s for a device whose
constant is `0x5A` (worst case about 256 x (timeout + settle)).


## Systemd

```bash
sudo cp deploy/yunmu-meter.service /etc/systemd/system/
sudo systemctl enable --now yunmu-meter
```

## Device quirks worth knowing

These cost real debugging time; they are load-bearing.

| Behaviour | Consequence |
|---|---|
| **Single connection.** The meter accepts one central and stops advertising while connected. | A running exporter locks out the phone app. Stop it to use the app, then power-cycle the meter to make it reachable again. |
| **Advertises only briefly.** Discovery works for a short window after power-up; after a disconnect there is often no advertisement at all. | An existing BlueZ connection must be reused rather than re-scanned. Hence the D-Bus transport instead of a scan-based client. |
| **Connects by MAC, not by name.** The app uses a cloud-stored address. | Impersonation requires spoofing the BLE public address, not just the advertised name. |
| **Rejects rapid reconnects** with `le-connection-abort-by-local`. | Retries must back off; see `collector_loop`. |
| **XOR key collisions.** `0xA5 ^ 0x36 == 0x93`, so a wrong key yields a plausible magic. | Frames are accepted only when the CRC verifies, never on magic alone. |
| **Reply length varies.** Reads carry 29 data bytes, clear-acks 13. | The counter sits at `data[7:9]` for reads and `data[6:8]` for acks. |

## Protocol summary

```
frame    EC ED | len(BE,2) | payload | CRC16(BE)        (V1 plain)
         整帧 XOR Σ(MAC bytes) & 0xFF
CRC16    reflected, init=(m5+m4+10), poly=((m5+11)<<8|m4)
payload  XOR 0x2C, trailing checksum = Σ & 0xFF

read     payload A4 AC A4
clear    payload AB AE AD 52 A4
config   payload 83 AA <hi> <lo> AC 9E AC AC <tail>
         value = ((hi<<8)|lo) ^ 0xACAC
```

Full write-up, including the reverse-engineering trail and the field
calibration evidence: [`docs/protocol.md`](docs/protocol.md).

## Measuring the refresh rate

`tools/probe_rate.py` separates three things that are easy to conflate:

- **poll ceiling** — reads/second the link can carry at all (connection
  interval, MTU and the firmware's reply path), independent of measurement rate;
- **native push** — whether the device streams notifications on its own once
  subscribed, with no request;
- **novelty rate** — how often the *values* actually change while polling flat
  out. Under a static load the reported fields are quantised, so identical
  readings are expected; the change-gap histogram tells "firmware is slow" apart
  from "nothing happened to measure".

```bash
python -m tools.probe_rate --mac 26:04:18:01:12:F4          # needs the BLE host
python -m tools.probe_rate --mac ... --listen 60 --reads 400
python -m tools.probe_rate --selftest                       # no radio
```

Run it twice — once at idle, once while toggling a known load. A static load has
no value transitions to time, and the median change gap is the best available
estimate of the firmware's own update period.

### Measured on a YM-S1-BES (2026-11, Pi 4, BlueZ 5.82)

| question | result |
|---|---|
| Does it push on its own? | **No.** 45 s subscribed with zero requests: 0 notifications. Request/response only. |
| Negotiated ATT MTU | **23** — the device answers `Exchange MTU` with 23, so a 38-byte reply is always 2 notify PDUs (20 + 18). |
| Negotiated connection interval | **22.5 ms** (requested 60 ms) |
| Serial read rate @ 22.5 ms | 14 reads/s, p50 latency 67 ms |
| Serial read rate @ forced 7.5 ms | **34.4 reads/s**, p50 29.4 ms, p95 30.6 ms |
| Reply depth | the firmware queues **3** outstanding reads, then drops the rest; recovers immediately |
| Burst rate (3 in flight) | ~44 reads/s |
| Value tick | **1.005 s median** gap, phase-locked (changes land at a stable phase offset inside each second) |
| Accumulator tick | advances on a **60.0 s** period (±16 ms over 130 s), not continuously |

The one-second tick is a **sample-and-hold**: every field updates at the same
instant, once per second. Nothing is gained by polling faster than 1 Hz for
fresh values — at 34 reads/s you get 33 identical repeats per real update.
Polling faster only helps when you need the *same* value sooner after the tick,
i.e. latency, not resolution.

### Capturing every update

Reading once per second is not enough. The device tick (firmware, phase-locked)
and the exporter tick (host clock, drifting) are independent, so a 1.0 s period
beats against the device: every so often one update is skipped and the next is
sampled twice. The fix is a period that is not equal to the tick, with more than
one probe inside it.

Defaults: `--interval 0.8 --reads-per-cycle 2` → probes at t and t+0.4, i.e.
**2.5 reads/s** against a 1 Hz device. Any update can only be missed if it falls
inside the 0.4 s gap between probes *and* the next cycle's first probe is late.

| `--interval` | `--reads-per-cycle` | reads/s | misses possible |
|---|---|---|---|
| 1.0 | 1 | 1.0 | **yes — beats with the device** |
| 0.8 | 2 (default) | 2.5 | only under jitter |
| 0.4 | 1 | 2.5 | equivalent, steadier cadence |
| 0.2 | 1 | 5.0 | none in practice, 2x the airtime |

Reads are scheduled on absolute deadlines, so a slow read does not accumulate
drift, and falling more than a full cycle behind resynchronises instead of
replaying a backlog.

Two counters report the result. `yunmu_device_tick_total` counts value changes
and therefore only moves while the load moves — it is a corroboration signal, not
a health signal. `yunmu_sampling_gaps_total` counts reads that came more than one
hold (1 s) after the previous read, which is load-independent and is what to alert
on. Miss detection deliberately does *not* try to infer "how many updates were
lost" from observed gaps: a meter on a constant load produces no value changes at
all, so any such estimate would be fiction.

Connection interval is worth setting explicitly for latency: 22.5 → 7.5 ms
halves round-trip time (67 → 29 ms) and doubles sustainable reads, but does not
change the 1 Hz update rate. It also costs airtime on a shared 2.4 GHz band.

## Device-side energy

`yunmu_energy_device_kwh` is the meter's own cumulative total, scaled from its
16-bit accumulator by `protocol.WH_PER_COUNT` (0.26 Wh/count, calibrated against
the vendor app: 0.16 kWh at count 607). Unlike `yunmu_energy_kwh` it survives an
exporter restart and is only zeroed when the meter's total is cleared in the app.

The accumulator wraps at 65536 counts (~17 kWh).

Measured behaviour (2026-11, 130 s at ~15 reads/s each):

| meter | mean power | counter | verdict |
|---|---|---|---|
| idle, no load | 0.000 W | 624 → 624 (0 steps) | does not free-run |
| ~3.6 W load | 3.646 W | 185 → 187 (+2) | **exactly 60.0 s per step** (±16 ms) |

The accumulator is a 60-second accumulator, not a per-sample one: it integrates
over a fixed 60 s window and emits the result as a whole count. It does not
advance without load, so it is measuring real energy rather than free-running.

The implied scale at this load is 0.066 Wh/count (0.13 Wh delivered per 2
counts), which is **4x smaller than `WH_PER_COUNT = 0.26`**. High-load
observations in *Known gaps* go the other way (counter advancing *slower* than
0.26 predicts). Two deviations in opposite directions cannot both be explained
by a single scale factor, so `WH_PER_COUNT` should be treated as unvalidated
outside the range it was fitted to. Use `yunmu_energy_kwh` for accounting and
`yunmu_counter_total` only as a coarse liveness signal.

## Known gaps

- **High-load accumulator lag.** Above roughly 250 W the accumulator advances
  much slower than the measured power implies (300 W for 6 minutes moved it 5
  counts, which is ~6 Wh/count instead of 0.26). Below that it tracks correctly.
  The scale above is therefore fitted to normal household loads; do not trust
  `yunmu_energy_device_kwh` deltas during sustained high-power use.
- **Energy scale factor unverified.** The accumulator is known to step
  monotonically (318 steps over 7 h of mixed load), giving an estimate of
  ≈0.019 Wh/step, but this has not been calibrated against a known load. Verify
  before relying on `yunmu_counter_total` as energy.
- **Config tail byte.** The last payload byte of a threshold write is not
  derivable from the value; captured samples live in
  `protocol.CONFIG_TAIL_SAMPLES`. Byte-exact replays should pass `tail`
  explicitly.
- **`clear()` is ack-only, not verified reset.** Replaying the exact frame the
  vendor app sends yields a valid `0x07` acknowledgement, but the counter read
  back afterwards was unchanged in testing. The app's own clear did drop the
  displayed total, so something beyond the BLE frame is involved (most likely
  the companion cloud call). Do not rely on `clear()` to zero the accumulator;
  track energy locally via `yunmu_energy_kwh` instead.

- **Per-device payload source unknown.** The read payload byte `s` and the
  response XOR are fixed per meter, but no relation to SN, MAC or cloud ids was
  found (exhaustive CRC8, all-polynomial CRC16, common hashes; see
  `docs/payload-derivation.md`). They behave like factory secrets stored in the
  vendor cloud; a new meter has to be captured once.
- **Cloud command issuance cannot be forged locally.** `bleCommandV1` request
  signatures are issued by the vendor REST API and bound to device + timestamp.
  A fresh timestamp with a captured signature returns `400 无效请求`; only
  captured/replayed values work.

## Scope

Interoperability work on a device owned by the author, for the purpose of
reading one's own meter. Not affiliated with the vendor.
