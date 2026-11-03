# BLE impersonator (ESP32-S3)

A YunMu meter answers only the command payload the vendor cloud issued for that
specific device. Every meter uses a different 3-byte read payload *and* a
different payload de-obfuscation constant, and a wrong value is dropped by the
firmware without any error — the request just times out.

This sketch recovers both values without rooting the phone: it impersonates the
meter at the BLE address level, so the mini-program delivers the cloud-issued
frame straight to the serial console.

## How it works

The mini-program identifies devices by name only (`YUNM` + MAC bytes reversed),
but it connects to the cloud-stored MAC. So the sketch does both:

- `esp_iface_mac_addr_set(TARGET, ESP_MAC_BT)` spoofs the public BLE address
- the advertised name is derived from the same MAC

It reproduces the vendor GATT service (`49535343-…`) with a notify and a
write-without-response characteristic. When the app connects and issues a read,
the sketch prints the raw bytes plus the XOR-decoded frame.

## Usage

1. Set `TARGET` and `DEV_NAME` to the meter you are probing (see below).
2. Flash it (`arduino-cli upload -p COM7 --fqbn esp32:esp32:esp32s3`).
3. **Power off the real meter** so the app cannot reach it.
4. In the mini-program open that device and tap 更新数据.
5. The console prints:

```
### WRITE #1 ###
RAW len=9 e9e805065f575f28d7
XOR len=9 eced00035a525a2dd2
```

The `XOR` line is a standard V1 frame: `EC ED | len | payload | CRC`. The
payload is the device's read command.

## Deriving the two per-device values

Given the captured reply from the *real* meter, the payload de-obfuscation
constant is found by trying every value until the logical structure validates
(`plain[0]` is a known reply type and the inner checksum matches):

```python
for x in range(256):
    plain = bytes(b ^ x for b in payload)
    if plain[0] == 0x08 and plain[2 + plain[1]] == sum(plain[:2 + plain[1]]) & 0xFF:
        print(hex(x))
```

Record both in `yunmu_ble_meter/protocol.py` → `KNOWN_PROFILES`.

## MAC ↔ name

```
name = "YUNM" + "".join("%02X" % b for b in reversed(mac_bytes))
```
