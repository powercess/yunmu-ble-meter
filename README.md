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
  locally integrated energy counter.

## What it does not do

- **Switch the relay.** This model exposes no on/off control through the app and
  no command was ever observed. There is nothing to replay.
- **Read cloud-side totals.** Cumulative cost and runtime live in the vendor's
  database, not on the device.
- **Write arbitrary payloads.** The firmware only accepts the handful of
  commands its cloud issues; anything else is rejected at the GATT layer.

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
| `yunmu_scrape_success` | gauge | 1 if the last read succeeded |
| `yunmu_last_read_timestamp_seconds` | gauge | Unix time of last good read |
| `yunmu_read_duration_seconds` | gauge | Duration of the last read |

`yunmu_counter_total` wraps at 65536 and is reset by `clear`, so it is a gauge,
not a counter. For long-horizon accounting use `yunmu_energy_kwh`.

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

## Known gaps

- **Energy scale factor unverified.** The accumulator is known to step
  monotonically (318 steps over 7 h of mixed load), giving an estimate of
  ≈0.019 Wh/step, but this has not been calibrated against a known load. Verify
  before relying on `yunmu_counter_total` as energy.
- **Config tail byte.** The last payload byte of a threshold write is not
  derivable from the value; captured samples live in
  `protocol.CONFIG_TAIL_SAMPLES`. Byte-exact replays should pass `tail`
  explicitly.

## Scope

Interoperability work on a device owned by the author, for the purpose of
reading one's own meter. Not affiliated with the vendor.
