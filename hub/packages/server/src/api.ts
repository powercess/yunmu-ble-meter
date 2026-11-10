import { randomUUID } from "node:crypto";
import type { FastifyInstance } from "fastify";
import { z } from "zod";

import {
  CapabilitiesSchema,
  MacSchema,
  PayloadSchema,
  PROTOCOL_VERSION,
  ReadingValuesSchema,
  type Command,
} from "@yunmu/protocol";

import { config } from "./config.js";
import type { CollectorRow, DeviceRow, Store } from "./db.js";
import type { MqttHub } from "./mqtt.js";
import { sseAdd } from "./sse.js";

const CreateDeviceBody = z
  .object({
    mac: MacSchema,
    name: z.string().trim().min(1).max(128).optional(),
    sn: z.string().trim().min(1).max(64).optional(),
    payload: PayloadSchema.optional(),
    xor: z.number().int().min(0).max(255).optional(),
  })
  .refine((v) => (v.payload === undefined) === (v.xor === undefined), {
    message: "payload and xor must be provided together",
  });

const UpdateDeviceBody = z.object({
  name: z.string().trim().min(1).max(128).optional(),
  sn: z.string().trim().min(1).max(64).optional(),
  enabled: z.boolean().optional(),
});

const BindBody = z.object({ collectorId: z.string().min(1) });
const LearnBody = z.object({ collectorId: z.string().min(1).optional() });
const ScanBody = z.object({
  durationSec: z.number().int().min(5).max(3600).default(120),
  collectorIds: z.array(z.string().min(1)).optional(),
});

function deviceView(row: DeviceRow) {
  const parsed = row.values_json ? ReadingValuesSchema.safeParse(JSON.parse(row.values_json)) : null;
  const values = parsed?.success ? parsed.data : null;
  const stale =
    row.enabled === 1 &&
    row.last_seen > 0 &&
    Date.now() - row.last_seen > config.staleAfterMs;
  return {
    id: String(row.id),
    mac: row.mac,
    sn: row.sn,
    name: row.name,
    model: row.model,
    payload: row.payload,
    xor: row.xor,
    verified: row.verified === 1,
    enabled: row.enabled === 1,
    collectorId: row.collector_id,
    state: row.enabled === 1 ? (stale ? "offline" : row.state) : "disabled",
    lastSeen: row.last_seen || null,
    values,
    stale,
  };
}

function collectorView(row: CollectorRow | null) {
  if (!row) return null;
  const online = Date.now() - row.last_seen <= config.collectorOfflineAfterMs;
  const caps = CapabilitiesSchema.safeParse(JSON.parse(row.capabilities_json));
  return {
    id: row.id,
    name: row.name,
    version: row.version,
    platform: row.platform,
    capabilities: caps.success ? caps.data : null,
    epoch: row.epoch,
    status: online ? "online" : "offline",
    lastSeen: row.last_seen || null,
  };
}

export function registerApi(app: FastifyInstance, store: Store, hub: MqttHub) {
  app.get("/api/health", async () => ({ ok: true, time: Date.now() }));

  app.get("/api/collectors", async () =>
    store.listCollectors().map(collectorView),
  );

  app.get("/api/collectors/:id", async (req, reply) => {
    const { id } = req.params as { id: string };
    const collector = collectorView(store.getCollector(id));
    if (!collector) return reply.code(404).send({ error: "collector not found" });
    const devices = store
      .listDevices()
      .filter((d) => d.collector_id === id)
      .map(deviceView);
    return { ...collector, devices };
  });

  app.get("/api/devices", async () => store.listDevices().map(deviceView));

  app.post("/api/devices", async (req, reply) => {
    const parsed = CreateDeviceBody.safeParse(req.body);
    if (!parsed.success) {
      return reply.code(400).send({ error: parsed.error.issues[0]?.message });
    }
    const body = parsed.data;
    const existing = store.getDeviceByMac(body.mac);
    if (existing) {
      return reply.code(409).send({ error: "device already exists", device: deviceView(existing) });
    }
    const device = store.createDevice(body);
    return reply.code(201).send(deviceView(device));
  });

  app.patch("/api/devices/:id", async (req, reply) => {
    const id = Number((req.params as { id: string }).id);
    const parsed = UpdateDeviceBody.safeParse(req.body);
    if (!parsed.success) {
      return reply.code(400).send({ error: parsed.error.issues[0]?.message });
    }
    const before = store.getDevice(id);
    if (!before) return reply.code(404).send({ error: "device not found" });
    const after = store.updateDevice(id, parsed.data);
    if (!after) return reply.code(404).send({ error: "device not found" });
    if (parsed.data.enabled !== undefined && before.collector_id) {
      hub.publishDesired(before.collector_id);
    }
    return deviceView(after);
  });

  app.post("/api/devices/:id/bind", async (req, reply) => {
    const id = Number((req.params as { id: string }).id);
    const parsed = BindBody.safeParse(req.body);
    if (!parsed.success) return reply.code(400).send({ error: "collectorId required" });
    const device = store.getDevice(id);
    if (!device) return reply.code(404).send({ error: "device not found" });
    const collector = store.getCollector(parsed.data.collectorId);
    if (!collector) return reply.code(404).send({ error: "collector not found" });

    const parsedCaps = CapabilitiesSchema.safeParse(JSON.parse(collector.capabilities_json));
    const maxLinks = parsedCaps.success ? parsedCaps.data.maxLinks : 4;
    const bound = store
      .listDevices()
      .filter((d) => d.collector_id === collector.id && d.enabled === 1).length;
    const alreadyHere = device.collector_id === collector.id;
    if (!alreadyHere && bound >= maxLinks) {
      return reply.code(409).send({ error: "collector link capacity reached" });
    }

    const previous = device.collector_id;
    const updated = store.bindDevice(id, collector.id);
    if (previous && previous !== collector.id) hub.publishDesired(previous);
    hub.publishDesired(collector.id);
    return deviceView(updated!);
  });

  app.delete("/api/devices/:id/bind", async (req, reply) => {
    const id = Number((req.params as { id: string }).id);
    const device = store.getDevice(id);
    if (!device) return reply.code(404).send({ error: "device not found" });
    const collectorId = device.collector_id;
    const updated = store.unbindDevice(id);
    if (collectorId) hub.publishDesired(collectorId);
    return deviceView(updated!);
  });

  app.post("/api/devices/:id/learn", async (req, reply) => {
    const id = Number((req.params as { id: string }).id);
    const parsed = LearnBody.safeParse(req.body ?? {});
    if (!parsed.success) return reply.code(400).send({ error: "invalid body" });
    const device = store.getDevice(id);
    if (!device) return reply.code(404).send({ error: "device not found" });
    const collectorId = parsed.data.collectorId ?? device.collector_id;
    if (!collectorId) {
      return reply.code(400).send({ error: "device is not bound to a collector" });
    }
    if (!store.getCollector(collectorId)) {
      return reply.code(404).send({ error: "collector not found" });
    }
    const jobId = randomUUID();
    store.createJob(jobId, "learn", device.mac, collectorId);
    const command: Command = {
      type: "job",
      v: PROTOCOL_VERSION,
      job: { id: jobId, kind: "learn", mac: device.mac },
    };
    hub.publishCommand(collectorId, command);
    return reply.code(202).send({ jobId, deviceId: String(id), collectorId });
  });

  app.get("/api/jobs", async () => store.listJobs());

  app.get("/api/events", async () => store.listEvents());

  app.post("/api/scan-sessions", async (req, reply) => {
    const parsed = ScanBody.safeParse(req.body ?? {});
    if (!parsed.success) return reply.code(400).send({ error: "invalid body" });
    const durationSec = parsed.data.durationSec;
    const endsAt = Date.now() + durationSec * 1000;
    const sessionId = randomUUID();
    const ids =
      parsed.data.collectorIds ??
      store
        .listCollectors()
        .filter((c) => Date.now() - c.last_seen <= config.collectorOfflineAfterMs)
        .map((c) => c.id);
    if (ids.length === 0) {
      return reply.code(409).send({ error: "no online collectors" });
    }
    store.createScanSession(sessionId, endsAt, { collectorIds: ids });
    for (const collectorId of ids) {
      hub.publishCommand(collectorId, {
        type: "scan",
        v: PROTOCOL_VERSION,
        sessionId,
        endsAt,
      });
    }
    return reply.code(201).send({ id: sessionId, endsAt, collectors: ids });
  });

  app.get("/api/scan-sessions/:id", async (req, reply) => {
    const { id } = req.params as { id: string };
    const session = store.getScanSession(id);
    if (!session) return reply.code(404).send({ error: "session not found" });
    const sightings = store.listSightings(id).map((s) => ({
      mac: s.mac,
      collectorId: s.collector_id,
      rssi: s.rssi,
      seenAt: s.seen_at,
      deviceId: s.device_id === null ? null : String(s.device_id),
    }));
    return {
      id: session.id,
      state: session.state,
      createdAt: session.created_at,
      endsAt: session.ends_at,
      sightings,
    };
  });

  app.get("/api/stream", async (req, reply) => {
    reply.hijack();
    reply.raw.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "Access-Control-Allow-Origin": "*",
    });
    sseAdd(reply.raw);
  });
}
