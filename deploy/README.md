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
`/etc/prometheus/prometheus.yml`, then:

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

# energy over the last 24 h, as a running total
increase(yunmu_energy_kwh[24h])

# is the meter reachable?
yunmu_scrape_success == 0

# voltage trend, 5-minute average
avg_over_time(yunmu_voltage_volts[5m])
```

`yunmu_energy_kwh` is a monotonic counter maintained by the exporter, so
`rate()` and `increase()` work on it. `yunmu_counter_total` is the device's own
raw accumulator — it wraps at 65536 and must not be rate()'d.

## Notes

- The exporter holds the meter's only BLE connection, so the phone app cannot
  connect while it runs. `sudo systemctl stop yunmu-meter` to hand the link back;
  the meter usually needs a power cycle before it will advertise again.
- The exporter polls every 2 s by default (`--interval`). Whether the device
  refreshes its readings faster than that is an open question: measure it with
  `tools/probe_rate.py` before lowering the interval.
