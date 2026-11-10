# yunmu-meter-rs

Lightweight replacement for the Python collector, intended for edge devices.
Single static binary, no interpreter or Python packages required.

## Design

- **BlueZ D-Bus directly** via `zbus`, one private connection per meter.
- **Single-threaded tokio runtime**; meters are polled round-robin on one task.
- **Hand-written Prometheus endpoint** on a std `TcpListener` (no HTTP framework).
- **Minimal TOML subset parser** for the existing `/etc/yunmu-meter/meters.toml`.
- Same protocol code as the Python implementation: frame magic, MAC-derived
  CRC16 and frame XOR, per-device read payload and response XOR.
- Watchdog: exits when no meter produced a reading for `--watchdog-seconds`
  so systemd can restart a wedged process.
- Rebuilds a meter's D-Bus connection after 3 consecutive failures.

## Build

```bash
cargo build --release
ls -lh target/release/yunmu-meter
```

Release profile uses `opt-level = "z"`, LTO, `panic = "abort"` and strips
symbols.

## Run

```bash
./yunmu-meter --config /etc/yunmu-meter/meters.toml --interval 0.8 --port 9103
```

Options: `--host`, `--port`, `--interval`, `--reads-per-cycle`,
`--watchdog-seconds`.

Systemd unit: [`deploy/yunmu-meter-rs.service`](../deploy/yunmu-meter-rs.service).

## Metrics

`/metrics` exposes the same series as the Python exporter (`yunmu_voltage_volts`,
`yunmu_current_amperes`, `yunmu_power_watts`, `yunmu_power_factor`,
`yunmu_threshold_watts`, `yunmu_counter_total`, `yunmu_energy_device_kwh`,
`yunmu_scrape_success`, `yunmu_last_read_timestamp_seconds`,
`yunmu_sample_age_seconds`, `yunmu_read_duration_seconds`). `/api/state` returns
the JSON table used by the Python dashboard.
