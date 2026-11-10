import mqtt from "mqtt";

import {
  PROTOCOL_VERSION,
  decodeCommand,
  DesiredStateSchema,
  topics,
  type DesiredDevice,
  type ReadingValues,
} from "@yunmu/protocol";

const url = process.env.MQTT_URL ?? "mqtt://127.0.0.1:1883";
const collectorId = process.env.COLLECTOR_ID ?? "sim-01";
const collectorName = process.env.COLLECTOR_NAME ?? "仿真采集器";
const scanNewDevices = Number(process.env.SCAN_NEW_DEVICES ?? 2);
const staleAfterMs = 30_000;

const state = {
  desired: [] as DesiredDevice[],
  values: new Map<string, ReadingValues>(),
  lastRead: new Map<string, number>(),
};

const client = mqtt.connect(url, {
  clientId: `yunmu-sim-${collectorId}-${Math.random().toString(16).slice(2, 6)}`,
  clean: true,
  reconnectPeriod: 2000,
  will: {
    topic: topics.events(collectorId),
    payload: JSON.stringify({
      type: "event",
      v: PROTOCOL_VERSION,
      kind: "offline",
      severity: "warn",
      message: "simulator link lost",
    }),
    qos: 1,
    retain: false,
  },
});

const publishEvent = (
  kind: string,
  message: string,
  severity: "info" | "warn" | "error" = "info",
) => {
  client.publish(
    topics.events(collectorId),
    JSON.stringify({
      type: "event",
      v: PROTOCOL_VERSION,
      kind,
      severity,
      message,
    }),
    { qos: 1 },
  );
};

const publishReported = () => {
  const devices = state.desired.map((device) => ({
    mac: device.mac,
    state: device.enabled ? ("online" as const) : ("disabled" as const),
    lastReadAt: state.lastRead.get(device.mac) ?? null,
    values: state.values.get(device.mac),
  }));
  client.publish(
    topics.reported(collectorId),
    JSON.stringify({
      type: "reported",
      v: PROTOCOL_VERSION,
      epoch: 0,
      status: "online",
      devices,
      stats: {
        links: devices.length,
        readsPerSecond: devices.length,
        lastError: "",
      },
    }),
    { qos: 1 },
  );
};

const publishHello = () => {
  client.publish(
    topics.hello(collectorId),
    JSON.stringify({
      type: "hello",
      v: PROTOCOL_VERSION,
      collectorId,
      name: collectorName,
      version: "0.1.0-sim",
      capabilities: {
        maxLinks: 5,
        maxReadsPerSecond: 14,
        canScanWhileConnected: false,
        platform: "simulator",
        firmware: "sim",
      },
    }),
    { qos: 1 },
  );
};

client.on("connect", () => {
  console.log(`[sim] connected to ${url} as ${collectorId}`);
  publishHello();
  publishEvent("online", "simulator connected");
  client.subscribe([topics.desired(collectorId), topics.cmd(collectorId)], {
    qos: 1,
  });
  publishReported();
});

client.on("message", (topic, payload) => {
  if (topic === topics.desired(collectorId)) {
    const parsed = DesiredStateSchema.safeParse(JSON.parse(payload.toString()));
    if (parsed.success) {
      state.desired = parsed.data.devices;
      console.log(`[sim] desired epoch ${parsed.data.epoch}: ${state.desired.length} devices`);
      publishReported();
    }
    return;
  }
  if (topic === topics.cmd(collectorId)) {
    const command = decodeCommand(payload);
    if (!command) return;

    if (command.type === "scan") {
      console.log(`[sim] scan session ${command.sessionId}`);
      publishEvent("scan", `scan session ${command.sessionId} started`);
      setTimeout(() => {
        const macs =
          command.macs ??
          Array.from({ length: scanNewDevices }, () =>
            [
              "02",
              "00",
              "00",
              Math.floor(Math.random() * 256).toString(16).padStart(2, "0"),
              Math.floor(Math.random() * 256).toString(16).padStart(2, "0"),
              Math.floor(Math.random() * 256).toString(16).padStart(2, "0"),
            ].join(":"),
          );
        for (const mac of macs) {
          client.publish(
            topics.events(collectorId),
            JSON.stringify({
              type: "sighting",
              v: PROTOCOL_VERSION,
              sessionId: command.sessionId,
              mac,
              rssi: -40 - Math.floor(Math.random() * 40),
              seenAt: Date.now(),
              name: `YUNM-${mac.slice(-5).replace(/:/g, "")}`,
            }),
            { qos: 1 },
          );
        }
      }, 800);
      return;
    }

    if (command.type === "job") {
      const { id, mac } = command.job;
      console.log(`[sim] learn job ${id} for ${mac}`);
      client.publish(
        topics.job(collectorId, id),
        JSON.stringify({
          type: "job",
          v: PROTOCOL_VERSION,
          jobId: id,
          state: "running",
        }),
        { qos: 1 },
      );
      setTimeout(() => {
        client.publish(
          topics.job(collectorId, id),
          JSON.stringify({
            type: "job",
            v: PROTOCOL_VERSION,
            jobId: id,
            state: "succeeded",
            result: { payload: "a4aca4", xor: 0x2c },
            error: "",
          }),
          { qos: 1 },
        );
        publishEvent("learn", `learned ${mac}`);
      }, 1500);
    }
  }
});

setInterval(() => {
  publishReported();
}, 2000);

setInterval(() => {
  const now = Date.now();
  const samples = state.desired
    .filter((device) => device.enabled)
    .map((device) => {
      const voltage = 220 + Math.random() * 10;
      const current = Math.random() * 2;
      const powerFactor = 0.4 + Math.random() * 0.6;
      const power = voltage * current * powerFactor;
      const counter = Math.floor((now / 60_000) % 65_536);
      const values: ReadingValues = {
        voltage: Number(voltage.toFixed(1)),
        current: Number(current.toFixed(3)),
        power: Number(power.toFixed(1)),
        powerFactor: Number(powerFactor.toFixed(2)),
        counter,
        threshold: 2000,
      };
      state.values.set(device.mac, values);
      state.lastRead.set(device.mac, now);
      return {
        mac: device.mac,
        ts: now,
        voltage: values.voltage,
        current: values.current,
        power: values.power,
        powerFactor: values.powerFactor,
        counter: values.counter,
        threshold: values.threshold,
      };
    });
  if (samples.length > 0) {
    client.publish(
      topics.telemetry(collectorId),
      JSON.stringify({ type: "telemetry", v: PROTOCOL_VERSION, samples }),
      { qos: 1 },
    );
  }
}, 1000);

setInterval(() => {
  const now = Date.now();
  for (const [mac, last] of state.lastRead) {
    if (now - last > staleAfterMs) {
      publishEvent("stale", `device ${mac} stale`, "warn");
      state.lastRead.delete(mac);
    }
  }
}, staleAfterMs);
