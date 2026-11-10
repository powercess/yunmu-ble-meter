# ESP32 resource probe

Firmware used to size an MCU for the collector workload. It brings up the same
building blocks as the edge service and reports heap, task stacks and per-core
idle ratios over serial (115200):

1. **REF** — 5 s with radios off: idle-counter reference.
2. **LOAD** — 10 s with Wi-Fi stack, BLE continuous scan, HTTP server and a
   synthetic protocol task at 40 frame round-trips/s (4 meters × 10 reads/s).

Expected output:

```
REF  idle0=... idle1=... over=5000ms
LOAD idle0=... idle1=... over=10000ms
SYNTH ops=...
HEAP free=... min=... internal_free=... internal_largest=...
PSRAM free=...
TASK ...
```

CPU utilisation is estimated as `1 - idle_load/idle_ref` per core.

## Build

Board: ESP32-S3 with 16 MB flash and 8 MB PSRAM (ESP32-S3R8 /
ESP32-S3-WROOM-1-N16R8 class).

```bash
arduino-cli compile \
  --fqbn esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,PartitionScheme=app3M_fat9M_16MB \
  tools/esp32_probe
```

On this Windows host the bundled xtensa GCC cannot create files (blocked by a
system policy), so the firmware is built on the Raspberry Pi with the same
Arduino ESP32 core and flashed from Windows with Python esptool.

## Flash / restore

Flash (offsets for the Arduino 16 MB layout):

```bash
esptool -p COM7 write-flash --flash-mode dio --flash-freq 80m --flash-size 16MB \
  0x0 bootloader.bin 0x8000 partitions.bin 0xe000 boot_app0.bin 0x10000 esp32_probe.ino.bin
```

The original `fake_meter` image was backed up before flashing:

```bash
esptool -p COM7 write-flash 0x0 esp_boot.bin 0x8000 pt.bin 0xe000 esp_ota.bin 0x10000 app0.bin
```
