import { describe, expect, test } from "bun:test";

import {
  CollectorMessageSchema,
  DesiredStateSchema,
  HelloSchema,
  normalizeMac,
  SightingSchema,
  topics,
} from "../src/index";

describe("protocol schemas", () => {
  test("normalizes MACs to canonical lowercase colons", () => {
    expect(normalizeMac("26:04:18:01:12:F4")).toBe("26:04:18:01:12:f4");
    expect(normalizeMac("2604180112f4")).toBe("26:04:18:01:12:f4");
    expect(normalizeMac("26-04-18-01-12-F4")).toBe("26:04:18:01:12:f4");
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
    expect(HelloSchema.parse(hello).collectorId).toBe("gw-01");
    expect(CollectorMessageSchema.safeParse(hello).success).toBe(true);
    expect(
      CollectorMessageSchema.safeParse({
        ...hello,
        capabilities: { maxLinks: 0, maxReadsPerSecond: 14 },
      }).success,
    ).toBe(false);
  });

  test("desired state normalizes MAC and payload", () => {
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
    expect(desired.devices[0].mac).toBe("26:04:18:01:12:f4");
    expect(desired.devices[0].payload).toBe("a4aca4");
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
    expect(result.success).toBe(false);
  });

  test("sighting and topic helpers", () => {
    const sighting = SightingSchema.parse({
      type: "sighting",
      v: 1,
      sessionId: "s1",
      mac: "02:00:00:aa:bb:cc",
      seenAt: Date.now(),
    });
    expect(sighting.mac).toBe("02:00:00:aa:bb:cc");
    expect(topics.desired("gw-01")).toBe("yunmu/v1/collectors/gw-01/desired");
    expect(topics.job("gw-01", "j1")).toBe(
      "yunmu/v1/collectors/gw-01/jobs/j1",
    );
  });
});
