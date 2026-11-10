import assert from "node:assert/strict";
import test from "node:test";

import {
  CollectorMessageSchema,
  DesiredStateSchema,
  HelloSchema,
  normalizeMac,
  SightingSchema,
  topics,
} from "../dist/index.js";

test("normalizes MACs to canonical lowercase colons", () => {
  assert.equal(normalizeMac("26:04:18:01:12:F4"), "26:04:18:01:12:f4");
  assert.equal(normalizeMac("2604180112f4"), "26:04:18:01:12:f4");
  assert.equal(normalizeMac("26-04-18-01-12-F4"), "26:04:18:01:12:f4");
});

test("hello must carry capabilities", () => {
  const hello = {
    type: "hello",
    v: 1,
    collectorId: "gw-01",
    name: "collector 1",
    version: "1.0.0",
    capabilities: {
      maxLinks: 5,
      maxReadsPerSecond: 14,
      canScanWhileConnected: false,
      platform: "linux",
      firmware: "test",
    },
  };
  assert.equal(HelloSchema.parse(hello).collectorId, "gw-01");
  assert.equal(CollectorMessageSchema.safeParse(hello).success, true);

  assert.equal(
    CollectorMessageSchema.safeParse({
      ...hello,
      capabilities: { maxLinks: 0, maxReadsPerSecond: 14 },
    }).success,
    false,
  );
});

test("desired state normalizes MAC and validates payload", () => {
  const desired = DesiredStateSchema.parse({
    type: "desired",
    v: 1,
    epoch: 3,
    devices: [
      {
        mac: "26:04:18:01:12:F4",
        payload: "A4ACA4",
        xor: 44,
        profile: { interval: 0.8, readsPerCycle: 2 },
        enabled: true,
      },
    ],
  });
  assert.equal(desired.devices[0].mac, "26:04:18:01:12:f4");
  assert.equal(desired.devices[0].payload, "a4aca4");
});

test("rejects malformed payloads", () => {
  const result = DesiredStateSchema.safeParse({
    type: "desired",
    v: 1,
    epoch: 1,
    devices: [
      {
        mac: "26:04:18:01:12:f4",
        payload: "zz",
        xor: 1,
        profile: { interval: 1, readsPerCycle: 1 },
        enabled: true,
      },
    ],
  });
  assert.equal(result.success, false);
});

test("sighting and topic helpers", () => {
  const sighting = SightingSchema.parse({
    type: "sighting",
    v: 1,
    sessionId: "s1",
    mac: "02:00:00:aa:bb:cc",
    seenAt: Date.now(),
  });
  assert.equal(sighting.mac, "02:00:00:aa:bb:cc");
  assert.equal(topics.desired("gw-01"), "yunmu/v1/collectors/gw-01/desired");
  assert.equal(
    topics.job("gw-01", "j1"),
    "yunmu/v1/collectors/gw-01/jobs/j1",
  );
});
