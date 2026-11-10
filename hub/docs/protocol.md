# Collector ↔ Hub protocol v1

Transport: MQTT 3.1.1/5, JSON payloads, QoS 1. Topic root: `yunmu/v1`.

Collectors connect **outbound** to the hub broker. Every collector has a stable
`collectorId` (provisioned at flash time or first boot).

## Topics

| Topic | Direction | Retain | Purpose |
|---|---|---|---|
| `yunmu/v1/collectors/{id}/hello` | collector → hub | no | announce identity + capabilities |
| `yunmu/v1/collectors/{id}/desired` | hub → collector | **yes** | full device list + epoch |
| `yunmu/v1/collectors/{id}/reported` | collector → hub | no | reconciliation snapshot |
| `yunmu/v1/collectors/{id}/telemetry` | collector → hub | no | batched readings |
| `yunmu/v1/collectors/{id}/events` | collector → hub | no | sightings, logs, LWT offline |
| `yunmu/v1/collectors/{id}/jobs/{jobId}` | collector → hub | no | job progress/result |
| `yunmu/v1/collectors/{id}/cmd` | hub → collector | no | scan / learn command |

## Identity

- **Collector**: `collectorId` is the stable key.
- **Device at collector**: MAC only (`aa:bb:cc:dd:ee:ff`, lowercase).
- **Device at hub**: MAC is the transport key; SN is the business identity;
  display name is presentation only. Renaming never touches collectors.

## hello

```json
{
  "type": "hello", "v": 1,
  "collectorId": "gw-01", "name": "1号采集器", "version": "1.0.0",
  "capabilities": {
    "maxLinks": 5, "maxReadsPerSecond": 14,
    "canScanWhileConnected": false,
    "platform": "linux", "firmware": "yunmu-agent 1.0.0"
  }
}
```

## desired (retained)

```json
{
  "type": "desired", "v": 1, "epoch": 42,
  "devices": [
    {
      "mac": "26:04:18:01:12:f4",
      "payload": "a4aca4",
      "xor": 44,
      "profile": { "interval": 0.8, "readsPerCycle": 2 },
      "enabled": true
    }
  ]
}
```

`epoch` increases on every hub-side change. Agents must ignore stale epochs and
drop devices absent from the latest list.

## reported

```json
{
  "type": "reported", "v": 1, "epoch": 42,
  "status": "online",
  "devices": [
    {
      "mac": "26:04:18:01:12:f4", "state": "online",
      "lastReadAt": 1794280119000,
      "values": {
        "voltage": 224.1, "current": 0.1, "power": 16.6,
        "powerFactor": 0.5, "counter": 2037, "threshold": 2000
      }
    }
  ],
  "stats": { "links": 4, "readsPerSecond": 10, "lastError": "" }
}
```

States: `online | offline | disabled | learning | error`.

## telemetry

```json
{
  "type": "telemetry", "v": 1,
  "samples": [
    { "mac": "26:04:18:01:12:f4", "ts": 1794280119000,
      "voltage": 224.1, "current": 0.1, "power": 16.6,
      "powerFactor": 0.5, "counter": 2037, "threshold": 2000 }
  ]
}
```

`ts` is collector wall-clock epoch ms (NTP required). The hub also records its
receive time.

## events / sightings

```json
{ "type": "event", "v": 1, "kind": "online", "severity": "info",
  "message": "collector connected" }

{ "type": "sighting", "v": 1, "sessionId": "…", "mac": "02:00:00:aa:bb:cc",
  "rssi": -61, "seenAt": 1794280119000, "name": "YUNM…" }
```

Collectors should publish an LWT `event` with `kind: "offline"` so the hub can
mark them offline immediately.

## jobs

```json
{ "type": "job", "v": 1, "jobId": "…", "state": "running" }
{ "type": "job", "v": 1, "jobId": "…", "state": "succeeded",
  "result": { "payload": "a4aca4", "xor": 44 }, "error": "" }
{ "type": "job", "v": 1, "jobId": "…", "state": "failed", "error": "…" }
```

## commands

```json
{ "type": "scan", "v": 1, "sessionId": "…", "endsAt": 1794280239000,
  "macs": ["02:00:00:aa:bb:cc"] }

{ "type": "job", "v": 1,
  "job": { "id": "…", "kind": "learn", "mac": "26:04:18:01:12:f4" } }
```

Scan commands are advisory windows: the meter only advertises briefly after
power-up, so the UI must pair a scan session with a physical power-cycle.

Learn jobs require exclusive access to the meter: the agent must stop sampling
it, disconnect any other link, run the brute-force learner (256 candidates),
verify with a read, then report the result.

## Versioning

`v` is the protocol major version. Additive fields are allowed; consumers must
ignore unknown fields. Breaking changes bump the topic root to `yunmu/v2`.
