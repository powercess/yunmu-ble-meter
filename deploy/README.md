# Deployment

Target: Raspberry Pi with a working BlueZ adapter, the meter in range.

## 1. Exporter

```bash
sudo mkdir -p /opt/yunmu-ble-meter /etc/yunmu-meter
sudo tar -xzf yunmu-ble-meter.tgz -C /opt/yunmu-ble-meter     # the yunmu_ble_meter/ package
sudo cp meters.example.toml /etc/yunmu-meter/meters.toml      # then edit it
sudo cp yunmu-meter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now yunmu-meter
```

The service reads its meters from `/etc/yunmu-meter/meters.toml`. Each meter
needs its MAC plus the two per-device protocol values (`read_payload`,
`payload_xor`); see `tools/fake_meter/README.md` for how to capture them. Find
the MAC from the advertised name: `YUNMF41201180426` decodes to
`26:04:18:01:12:F4` (name is `YUNM` + MAC bytes reversed).

Check it:

```bash
curl -s localhost:9103/ | head            # live HTML dashboard
curl -s localhost:9103/metrics | grep yunmu_
```

## 2. Prometheus

Append the job from `prometheus-scrape.yml` to `scrape_configs` in
`/etc/prometheus/prometheus.yml`.

The job scrapes at `1s`. The meter publishes one new record per second, so a
slower interval stores a decimated copy of what the exporter already collected:
at the usual `5s` default only one record in five survives. Scraping is a read of
the exporter's memory, not of the BLE link, so 1 s costs nothing on the device
side.

Then validate and reload:

```bash
sudo promtool check config /etc/prometheus/prometheus.yml
sudo systemctl reload prometheus
```

Verify the target is up:

```bash
curl -s 'localhost:9090/api/v1/targets?state=active' | grep -o '"health":"[^"]*"'
```

## 3. Useful queries

```promql
# current draw
yunmu_power_watts

# energy over the last 24 h, as a running total (local integral: resets when
# the exporter restarts, so prefer the device-side counter for long horizons)
increase(yunmu_energy_kwh[24h])

# device-side cumulative energy, survives exporter restarts
yunmu_energy_device_kwh

# sampling health: must stay 0, otherwise the exporter fell behind
rate(yunmu_sampling_gaps_total[10m])

# device updates observed per second: ~1 while the load is moving
rate(yunmu_device_tick_total[5m])

# is the meter reachable?
yunmu_scrape_success == 0

# voltage trend, 5-minute average
avg_over_time(yunmu_voltage_volts[5m])
```

`yunmu_energy_kwh` is a monotonic counter maintained by the exporter, so
`rate()` and `increase()` work on it, but it restarts from zero whenever the
process does, which `increase()` renders as a discontinuity.
`yunmu_energy_device_kwh` comes from the meter's own accumulator and survives
restarts. `yunmu_counter_total` is that accumulator's raw count — it wraps at
65536 and must not be rate()'d.

## Notes

- The exporter holds the meter's only BLE connection, so the phone app cannot
  connect while it runs. `sudo systemctl stop yunmu-meter` to hand the link back;
  the meter usually needs a power cycle before it will advertise again.
- The exporter samples at `--interval 0.8` with two probes per cycle (2.5
  reads/s) so every 1 Hz device record is observed. The meter publishes exactly
  one new record per second, so sampling faster adds airtime without adding
  resolution; `tools/probe_rate.py` re-measures both numbers.
