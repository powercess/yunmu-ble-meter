import { z } from "zod";

export const PROTOCOL_VERSION = 1;
export const TOPIC_ROOT = "yunmu/v1";

/** Canonical MAC form: lowercase, colon separated. */
export const MAC_RE = /^[0-9a-f]{2}(:[0-9a-f]{2}){5}$/;

export function normalizeMac(input: string): string {
  const clean = input.replace(/[^0-9a-fA-F]/g, "").toLowerCase();
  if (clean.length !== 12) return input.trim().toLowerCase();
  return clean.match(/.{2}/g)!.join(":");
}

export const MacSchema = z
  .string()
  .transform((s) => normalizeMac(s))
  .pipe(z.string().regex(MAC_RE, "invalid MAC address"));

/** Six hex characters: the 3-byte per-device read payload. */
export const PayloadSchema = z
  .string()
  .transform((s) => s.replace(/[^0-9a-fA-F]/g, "").toLowerCase())
  .pipe(z.string().regex(/^[0-9a-f]{6}$/, "payload must be 6 hex characters"));

export const CapabilitiesSchema = z.object({
  maxLinks: z.number().int().min(1).max(64),
  maxReadsPerSecond: z.number().min(1).max(200),
  canScanWhileConnected: z.boolean().default(false),
  platform: z.enum(["linux", "esp32s3", "simulator"]).default("linux"),
  firmware: z.string().default(""),
});
export type Capabilities = z.infer<typeof CapabilitiesSchema>;

export const HelloSchema = z.object({
  type: z.literal("hello"),
  v: z.literal(PROTOCOL_VERSION),
  collectorId: z.string().min(1).max(64),
  name: z.string().min(1).max(128),
  version: z.string().default(""),
  capabilities: CapabilitiesSchema,
  token: z.string().optional(),
});
export type Hello = z.infer<typeof HelloSchema>;

export const SamplingProfileSchema = z.object({
  interval: z.number().positive().default(0.8),
  readsPerCycle: z.number().int().min(1).max(8).default(2),
});
export type SamplingProfile = z.infer<typeof SamplingProfileSchema>;

export const DesiredDeviceSchema = z.object({
  mac: MacSchema,
  payload: PayloadSchema,
  xor: z.number().int().min(0).max(255),
  profile: SamplingProfileSchema,
  enabled: z.boolean().default(true),
});
export type DesiredDevice = z.infer<typeof DesiredDeviceSchema>;

export const DesiredStateSchema = z.object({
  type: z.literal("desired"),
  v: z.literal(PROTOCOL_VERSION),
  epoch: z.number().int().min(0),
  devices: z.array(DesiredDeviceSchema),
});
export type DesiredState = z.infer<typeof DesiredStateSchema>;

export const DeviceStateSchema = z.enum([
  "online",
  "offline",
  "disabled",
  "learning",
  "error",
]);
export type DeviceState = z.infer<typeof DeviceStateSchema>;

export const ReadingValuesSchema = z.object({
  voltage: z.number(),
  current: z.number(),
  power: z.number(),
  powerFactor: z.number(),
  counter: z.number().int(),
  threshold: z.number().int(),
});
export type ReadingValues = z.infer<typeof ReadingValuesSchema>;

export const ReportedDeviceSchema = z.object({
  mac: MacSchema,
  state: DeviceStateSchema,
  lastReadAt: z.number().nullable().default(null),
  values: ReadingValuesSchema.optional(),
});
export type ReportedDevice = z.infer<typeof ReportedDeviceSchema>;

export const ReportedStateSchema = z.object({
  type: z.literal("reported"),
  v: z.literal(PROTOCOL_VERSION),
  epoch: z.number().int().min(0),
  status: z.enum(["online", "offline", "scanning", "learning"]).default("online"),
  devices: z.array(ReportedDeviceSchema),
  stats: z
    .object({
      links: z.number().int().min(0).default(0),
      readsPerSecond: z.number().min(0).default(0),
      lastError: z.string().default(""),
    })
    .default({ links: 0, readsPerSecond: 0, lastError: "" }),
});
export type ReportedState = z.infer<typeof ReportedStateSchema>;

export const TelemetrySampleSchema = z.object({
  mac: MacSchema,
  ts: z.number().int(),
  voltage: z.number(),
  current: z.number(),
  power: z.number(),
  powerFactor: z.number(),
  counter: z.number().int(),
  threshold: z.number().int(),
});
export type TelemetrySample = z.infer<typeof TelemetrySampleSchema>;

export const TelemetryBatchSchema = z.object({
  type: z.literal("telemetry"),
  v: z.literal(PROTOCOL_VERSION),
  samples: z.array(TelemetrySampleSchema).max(200),
});
export type TelemetryBatch = z.infer<typeof TelemetryBatchSchema>;

export const SightingSchema = z.object({
  type: z.literal("sighting"),
  v: z.literal(PROTOCOL_VERSION),
  sessionId: z.string().min(1),
  mac: MacSchema,
  rssi: z.number().int().optional(),
  seenAt: z.number().int(),
  name: z.string().default(""),
});
export type Sighting = z.infer<typeof SightingSchema>;

export const JobReportSchema = z.object({
  type: z.literal("job"),
  v: z.literal(PROTOCOL_VERSION),
  jobId: z.string().min(1),
  state: z.enum(["running", "succeeded", "failed"]),
  result: z
    .object({
      payload: PayloadSchema,
      xor: z.number().int().min(0).max(255),
    })
    .optional(),
  error: z.string().default(""),
});
export type JobReport = z.infer<typeof JobReportSchema>;

export const EventSchema = z.object({
  type: z.literal("event"),
  v: z.literal(PROTOCOL_VERSION),
  kind: z.string().min(1),
  severity: z.enum(["info", "warn", "error"]).default("info"),
  message: z.string().default(""),
  data: z.record(z.string(), z.unknown()).optional(),
});
export type CollectorEvent = z.infer<typeof EventSchema>;

export const CollectorMessageSchema = z.discriminatedUnion("type", [
  HelloSchema,
  ReportedStateSchema,
  TelemetryBatchSchema,
  SightingSchema,
  JobReportSchema,
  EventSchema,
]);
export type CollectorMessage = z.infer<typeof CollectorMessageSchema>;

/** Server -> collector one-shot commands. */
export const ScanCommandSchema = z.object({
  type: z.literal("scan"),
  v: z.literal(PROTOCOL_VERSION),
  sessionId: z.string().min(1),
  endsAt: z.number().int(),
  macs: z.array(MacSchema).optional(),
});
export type ScanCommand = z.infer<typeof ScanCommandSchema>;

export const LearnJobSchema = z.object({
  id: z.string().min(1),
  kind: z.literal("learn"),
  mac: MacSchema,
});
export type LearnJob = z.infer<typeof LearnJobSchema>;

export const JobCommandSchema = z.object({
  type: z.literal("job"),
  v: z.literal(PROTOCOL_VERSION),
  job: LearnJobSchema,
});
export type JobCommand = z.infer<typeof JobCommandSchema>;

export const CommandSchema = z.discriminatedUnion("type", [
  ScanCommandSchema,
  JobCommandSchema,
]);
export type Command = z.infer<typeof CommandSchema>;

export const topics = {
  hello: (collectorId: string) => `${TOPIC_ROOT}/collectors/${collectorId}/hello`,
  desired: (collectorId: string) =>
    `${TOPIC_ROOT}/collectors/${collectorId}/desired`,
  reported: (collectorId: string) =>
    `${TOPIC_ROOT}/collectors/${collectorId}/reported`,
  telemetry: (collectorId: string) =>
    `${TOPIC_ROOT}/collectors/${collectorId}/telemetry`,
  events: (collectorId: string) =>
    `${TOPIC_ROOT}/collectors/${collectorId}/events`,
  job: (collectorId: string, jobId: string) =>
    `${TOPIC_ROOT}/collectors/${collectorId}/jobs/${jobId}`,
  cmd: (collectorId: string) =>
    `${TOPIC_ROOT}/collectors/${collectorId}/cmd`,
  collectorWildcard: `${TOPIC_ROOT}/collectors/+/+`,
  collectorJobsWildcard: `${TOPIC_ROOT}/collectors/+/jobs/+`,
} as const;

/** Parse a JSON payload coming from a collector topic. */
export function decodeCollectorMessage(
  payload: Buffer | string,
): CollectorMessage | null {
  try {
    const text = typeof payload === "string" ? payload : payload.toString("utf8");
    return CollectorMessageSchema.parse(JSON.parse(text));
  } catch {
    return null;
  }
}

export function decodeCommand(payload: Buffer | string): Command | null {
  try {
    const text = typeof payload === "string" ? payload : payload.toString("utf8");
    return CommandSchema.parse(JSON.parse(text));
  } catch {
    return null;
  }
}

export function encode(message: unknown): string {
  return JSON.stringify(message);
}
