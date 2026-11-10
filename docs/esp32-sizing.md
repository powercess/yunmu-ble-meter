# MCU sizing for the collector workload

Measured on the ESP32-S3 board (ESP32-S3R8 class: 16 MB flash, 8 MB PSRAM)
with `tools/esp32_probe`: BLE continuous scan (NimBLE), Wi-Fi stack up, HTTP
server, and a synthetic protocol task at 40 frame round-trips/s (4 meters ×
10 reads/s).

## Measured on ESP32-S3

| Metric | Value |
|---|---|
| Firmware (Arduino core + Wi-Fi + BLE + WebServer) | **1,114,958 B** (35 % of a 3 MB app partition) |
| Static DRAM (globals) | **49,800 B** |
| Heap free at boot (radios off) | 290,668 B |
| Heap free with all stacks up | 166,316 B |
| Heap consumed by stacks | **124,352 B** |
| Total internal RAM used (static + heap) | **≈ 174 KB of 512 KB** (34 %) |
| PSRAM used | ≈ 4.5 KB of 8 MB |
| CPU total under load | **≈ 0.2 % of 2 cores** (idle0 99.68 %, idle1 99.95 %) |
| Largest free block after load | 124,916 B |
| Stack high-water marks | Wi-Fi 4.5 KB, NimBLE host 2.5 KB, BT controller 2.7 KB, esp_timer 7.7 KB |

The workload is **RAM-bound, not CPU-bound**: BLE scanning, Wi-Fi stack and the
protocol loop leave the two 240 MHz cores >99 % idle.

## Projection to other parts

Same application stacks (NimBLE + Wi-Fi + lwIP + HTTP + protocol), scaled by
SRAM size. CPU scales roughly with core-clock × core count; the measured
S3 load is the baseline. Values are estimates, not measurements.

| Part | SRAM | Radios | RAM use | RAM % | CPU % | Verdict |
|---|---|---|---|---|---|---|
| ESP32-S3 | 512 KB | Wi-Fi 4 + BLE 5 | ~174 KB | 34 % | ~0.2 % | measured; plenty of headroom |
| ESP32 (classic) | 520 KB | Wi-Fi 4 + BT 4.2 | ~190–220 KB¹ | 40–45 % | ~0.3 % | Bluedroid BLE stack is heavier |
| ESP32-C6 | 512 KB | Wi-Fi 6 + BLE 5.2 | ~170 KB | 33 % | ~0.6 % | headroom |
| ESP32-C3 | 400 KB | Wi-Fi 4 + BLE 5 | ~165 KB | **41 %** | ~0.6 % | **practical minimum with TLS/OTA headroom** |
| ESP32-C2 (ESP8684) | 272 KB | Wi-Fi 4 + BLE 5 | ~160–170 KB | **60–63 %** (~70 % of usable) | ~0.9 % | closest to 70–85 % RAM |
| ESP32-H2 | 320 KB | BLE 5.2 + 802.15.4, **no Wi-Fi** | ~110 KB | 34 % | <0.3 % | BLE-only bridge |
| nRF52840 | 256 KB | BLE 5.3, **no Wi-Fi** | ~90 KB | 35 % | ~0.5 % | BLE-only bridge |

¹ Bluedroid instead of NimBLE.

## What 70–85 % actually means here

- **RAM 70–85 %**: only a ~256–272 KB part lands there. **ESP32-C2** is the
  closest Wi-Fi+BLE candidate (~70 % of usable SRAM with this stack). Adding
  TLS + OTA + a second socket pushes it past 85 %, so C2 works only if those are
  trimmed.
- **CPU 70–85 %**: not reachable with this workload on any ESP32-class part —
  it is <1 % even on a single 120 MHz core. To be CPU-bound you would need a
  much smaller MCU or a much heavier workload (many meters, local storage,
  signal processing).

## Recommendation

- **Buy ESP32-C3 (400 KB SRAM, 4 MB flash)** for the production device:
  ~41 % RAM with the measured stacks, rising to ~55–70 % with TLS + OTA + a
  real MQTT/HTTP connection, still with comfortable headroom.
- **ESP32-C2** if BOM cost dominates and you can keep TLS short-lived / no OTA;
  it sits closest to the requested 70–85 % RAM utilization, but has little
  margin.
- **ESP32-S3** (this board) only if you want PSRAM/headroom or plan to run more
  meters; it is over-provisioned for four meters (34 % RAM, 0.2 % CPU).
- If Wi-Fi can move to a gateway, **nRF52840 / ESP32-H2** are BLE-only options
  at ~35 % RAM, with the same caveat: this workload is too light to reach 70 %
  without adding features.

Flash sizing: this firmware is 1.1 MB. With OTA (two app slots) plus NVS, a
**4 MB flash** part is the minimum; 8 MB gives room for logs/certs.

## Reproduce

`tools/esp32_probe` (Arduino, ESP32 core 3.3.12) prints heap and per-task
runtime on serial 115200. It was built on the Raspberry Pi because the Windows
host blocks the xtensa toolchain from creating files; flashing uses Python
`esptool` over COM7.
