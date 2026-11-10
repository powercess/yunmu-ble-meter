import fs from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

import type {
  DesiredDevice,
  Hello,
  ReadingValues,
  TelemetrySample,
} from "@yunmu/protocol";

import { config } from "./config.js";

export type CollectorRow = {
  id: string;
  name: string;
  version: string;
  platform: string;
  capabilities_json: string;
  status: string;
  last_seen: number;
  epoch: number;
  created_at: number;
};

export type DeviceRow = {
  id: number;
  mac: string;
  sn: string | null;
  name: string;
  model: string;
  payload: string | null;
  xor: number | null;
  verified: number;
  enabled: number;
  collector_id: string | null;
  lease_epoch: number;
  state: string;
  last_seen: number;
  values_json: string | null;
  created_at: number;
  updated_at: number;
};

export type SightingRow = {
  id: number;
  session_id: string;
  mac: string;
  collector_id: string;
  rssi: number | null;
  seen_at: number;
};

export type JobRow = {
  id: string;
  type: string;
  device_mac: string;
  collector_id: string;
  state: string;
  error: string;
  result_json: string | null;
  created_at: number;
  updated_at: number;
};

export type EventRow = {
  id: number;
  collector_id: string;
  kind: string;
  severity: string;
  message: string;
  data_json: string | null;
  created_at: number;
};

const now = Date.now;

function ensureDir(file: string) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
}

export class Store {
  readonly db: DatabaseSync;

  constructor(dbPath = config.dbPath) {
    ensureDir(dbPath);
    this.db = new DatabaseSync(dbPath);
    this.db.exec("PRAGMA journal_mode = WAL;");
    this.db.exec("PRAGMA foreign_keys = ON;");
    this.migrate();
  }

  private migrate() {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS collectors (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        version TEXT NOT NULL DEFAULT '',
        platform TEXT NOT NULL DEFAULT '',
        capabilities_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'offline',
        last_seen INTEGER NOT NULL DEFAULT 0,
        epoch INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS devices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mac TEXT NOT NULL UNIQUE,
        sn TEXT,
        name TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT 'YM-S1-BES',
        payload TEXT,
        xor INTEGER,
        verified INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        collector_id TEXT,
        lease_epoch INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'unknown',
        last_seen INTEGER NOT NULL DEFAULT 0,
        values_json TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS telemetry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_mac TEXT NOT NULL,
        collector_id TEXT NOT NULL,
        ts INTEGER NOT NULL,
        voltage REAL NOT NULL,
        current REAL NOT NULL,
        power REAL NOT NULL,
        power_factor REAL NOT NULL,
        counter INTEGER NOT NULL,
        threshold_watts INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_telemetry_mac_ts
        ON telemetry (device_mac, ts DESC);
      CREATE TABLE IF NOT EXISTS scan_sessions (
        id TEXT PRIMARY KEY,
        state TEXT NOT NULL DEFAULT 'active',
        scope_json TEXT NOT NULL DEFAULT '{}',
        created_at INTEGER NOT NULL,
        ends_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS sightings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        mac TEXT NOT NULL,
        collector_id TEXT NOT NULL,
        rssi INTEGER,
        seen_at INTEGER NOT NULL,
        UNIQUE(session_id, mac, collector_id)
      );
      CREATE INDEX IF NOT EXISTS idx_sightings_session
        ON sightings (session_id, seen_at DESC);
      CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        device_mac TEXT NOT NULL,
        collector_id TEXT NOT NULL,
        state TEXT NOT NULL,
        error TEXT NOT NULL DEFAULT '',
        result_json TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        collector_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        severity TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        data_json TEXT,
        created_at INTEGER NOT NULL
      );
    `);
  }

  // ------------------------------------------------------------- collectors

  upsertCollector(hello: Hello): CollectorRow {
    const ts = now();
    this.db
      .prepare(
        `INSERT INTO collectors
           (id, name, version, platform, capabilities_json, status, last_seen, epoch, created_at)
         VALUES (?, ?, ?, ?, ?, 'online', ?, 1, ?)
         ON CONFLICT(id) DO UPDATE SET
           name = excluded.name,
           version = excluded.version,
           platform = excluded.platform,
           capabilities_json = excluded.capabilities_json,
           status = 'online',
           last_seen = excluded.last_seen`,
      )
      .run(
        hello.collectorId,
        hello.name,
        hello.version,
        hello.capabilities.platform,
        JSON.stringify(hello.capabilities),
        ts,
        ts,
      );
    return this.getCollector(hello.collectorId)!;
  }

  touchCollector(id: string, status = "online"): void {
    this.db
      .prepare("UPDATE collectors SET status = ?, last_seen = ? WHERE id = ?")
      .run(status, now(), id);
  }

  listCollectors(): CollectorRow[] {
    return this.db
      .prepare("SELECT * FROM collectors ORDER BY name")
      .all() as unknown as CollectorRow[];
  }

  getCollector(id: string): CollectorRow | null {
    return (
      (this.db
        .prepare("SELECT * FROM collectors WHERE id = ?")
        .get(id) as unknown as CollectorRow | undefined) ?? null
    );
  }

  bumpEpoch(id: string): number {
    const row = this.db
      .prepare("UPDATE collectors SET epoch = epoch + 1 WHERE id = ? RETURNING epoch")
      .get(id) as unknown as { epoch: number } | undefined;
    return row?.epoch ?? 0;
  }

  // ---------------------------------------------------------------- devices

  listDevices(): DeviceRow[] {
    return this.db
      .prepare("SELECT * FROM devices ORDER BY name")
      .all() as unknown as DeviceRow[];
  }

  getDevice(id: number): DeviceRow | null {
    return (
      (this.db
        .prepare("SELECT * FROM devices WHERE id = ?")
        .get(id) as unknown as DeviceRow | undefined) ?? null
    );
  }

  getDeviceByMac(mac: string): DeviceRow | null {
    return (
      (this.db
        .prepare("SELECT * FROM devices WHERE mac = ?")
        .get(mac) as unknown as DeviceRow | undefined) ?? null
    );
  }

  createDevice(input: {
    mac: string;
    name?: string;
    sn?: string;
    payload?: string;
    xor?: number;
  }): DeviceRow {
    const ts = now();
    const name = input.name?.trim() || input.mac;
    this.db
      .prepare(
        `INSERT INTO devices
           (mac, sn, name, payload, xor, verified, enabled, state, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, 0, 1, 'unknown', ?, ?)
         ON CONFLICT(mac) DO UPDATE SET
           name = excluded.name,
           sn = COALESCE(excluded.sn, devices.sn),
           payload = COALESCE(excluded.payload, devices.payload),
           xor = COALESCE(excluded.xor, devices.xor),
           updated_at = excluded.updated_at`,
      )
      .run(
        input.mac,
        input.sn ?? null,
        name,
        input.payload ?? null,
        input.xor ?? null,
        ts,
        ts,
      );
    return this.getDeviceByMac(input.mac)!;
  }

  updateDevice(
    id: number,
    fields: { name?: string; sn?: string; enabled?: boolean },
  ): DeviceRow | null {
    const device = this.getDevice(id);
    if (!device) return null;
    this.db
      .prepare(
        `UPDATE devices SET
           name = COALESCE(?, name),
           sn = COALESCE(?, sn),
           enabled = COALESCE(?, enabled),
           updated_at = ?
         WHERE id = ?`,
      )
      .run(
        fields.name ?? null,
        fields.sn ?? null,
        fields.enabled === undefined ? null : fields.enabled ? 1 : 0,
        now(),
        id,
      );
    return this.getDevice(id);
  }

  setDevicePayload(mac: string, payload: string, xor: number): void {
    this.db
      .prepare(
        "UPDATE devices SET payload = ?, xor = ?, verified = 1, updated_at = ? WHERE mac = ?",
      )
      .run(payload, xor, now(), mac);
  }

  bindDevice(id: number, collectorId: string): DeviceRow | null {
    const collector = this.getCollector(collectorId);
    if (!collector) return null;
    this.db
      .prepare(
        "UPDATE devices SET collector_id = ?, lease_epoch = ?, updated_at = ? WHERE id = ?",
      )
      .run(collectorId, collector.epoch, now(), id);
    return this.getDevice(id);
  }

  unbindDevice(id: number): DeviceRow | null {
    this.db
      .prepare(
        "UPDATE devices SET collector_id = NULL, lease_epoch = 0, state = 'unknown', updated_at = ? WHERE id = ?",
      )
      .run(now(), id);
    return this.getDevice(id);
  }

  recordReportedDevice(
    mac: string,
    collectorId: string,
    state: string,
    lastReadAt: number | null,
    values?: ReadingValues,
  ): void {
    const device = this.getDeviceByMac(mac);
    if (!device) return;
    this.db
      .prepare(
        `UPDATE devices SET
           state = ?, last_seen = ?, values_json = COALESCE(?, values_json),
           collector_id = COALESCE(collector_id, ?), updated_at = ?
         WHERE mac = ?`,
      )
      .run(
        state,
        lastReadAt ?? device.last_seen,
        values ? JSON.stringify(values) : null,
        collectorId,
        now(),
        mac,
      );
  }

  insertTelemetry(
    collectorId: string,
    samples: TelemetrySample[],
  ): void {
    if (samples.length === 0) return;
    const stmt = this.db.prepare(
      `INSERT INTO telemetry
         (device_mac, collector_id, ts, voltage, current, power, power_factor, counter, threshold_watts)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    );
    this.db.exec("BEGIN");
    try {
      for (const s of samples) {
        stmt.run(
          s.mac,
          collectorId,
          s.ts,
          s.voltage,
          s.current,
          s.power,
          s.powerFactor,
          s.counter,
          s.threshold,
        );
        this.db
          .prepare(
            `UPDATE devices SET
               state = 'online', last_seen = ?, values_json = ?, updated_at = ?
             WHERE mac = ?`,
          )
          .run(
            s.ts,
            JSON.stringify({
              voltage: s.voltage,
              current: s.current,
              power: s.power,
              powerFactor: s.powerFactor,
              counter: s.counter,
              threshold: s.threshold,
            }),
            now(),
            s.mac,
          );
      }
      this.db.exec("COMMIT");
    } catch (err) {
      this.db.exec("ROLLBACK");
      throw err;
    }
  }

  /** Devices with a verified payload/xor assigned to a collector. */
  desiredDevices(collectorId: string): DesiredDevice[] {
    const rows = this.db
      .prepare(
        `SELECT mac, payload, xor FROM devices
         WHERE collector_id = ? AND enabled = 1 AND payload IS NOT NULL AND xor IS NOT NULL`,
      )
      .all(collectorId) as unknown as {
      mac: string;
      payload: string;
      xor: number;
    }[];
    return rows.map((r) => ({
      mac: r.mac,
      payload: r.payload,
      xor: r.xor,
      profile: { interval: 0.8, readsPerCycle: 2 },
      enabled: true,
    }));
  }

  // ------------------------------------------------------------------- scan

  createScanSession(id: string, endsAt: number, scope: unknown): void {
    this.db
      .prepare(
        "INSERT INTO scan_sessions (id, state, scope_json, created_at, ends_at) VALUES (?, 'active', ?, ?, ?)",
      )
      .run(id, JSON.stringify(scope ?? {}), now(), endsAt);
  }

  endScanSession(id: string): void {
    this.db
      .prepare("UPDATE scan_sessions SET state = 'ended' WHERE id = ?")
      .run(id);
  }

  getScanSession(id: string) {
    return this.db
      .prepare("SELECT * FROM scan_sessions WHERE id = ?")
      .get(id) as unknown as
      | {
          id: string;
          state: string;
          scope_json: string;
          created_at: number;
          ends_at: number;
        }
      | undefined;
  }

  addSighting(input: {
    sessionId: string;
    mac: string;
    collectorId: string;
    rssi?: number;
    seenAt: number;
  }): void {
    this.db
      .prepare(
        `INSERT INTO sightings (session_id, mac, collector_id, rssi, seen_at)
         VALUES (?, ?, ?, ?, ?)
         ON CONFLICT(session_id, mac, collector_id) DO UPDATE SET
           rssi = excluded.rssi,
           seen_at = excluded.seen_at`,
      )
      .run(
        input.sessionId,
        input.mac,
        input.collectorId,
        input.rssi ?? null,
        input.seenAt,
      );
  }

  listSightings(sessionId: string): (SightingRow & { device_id: number | null })[] {
    const rows = this.db
      .prepare(
        `SELECT s.*, d.id AS device_id
         FROM sightings s LEFT JOIN devices d ON d.mac = s.mac
         WHERE s.session_id = ?
         ORDER BY s.seen_at DESC`,
      )
      .all(sessionId) as unknown as (SightingRow & {
      device_id: number | null;
    })[];
    return rows;
  }

  // ------------------------------------------------------------------- jobs

  createJob(id: string, type: string, mac: string, collectorId: string): JobRow {
    const ts = now();
    this.db
      .prepare(
        "INSERT INTO jobs (id, type, device_mac, collector_id, state, created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?)",
      )
      .run(id, type, mac, collectorId, ts, ts);
    return this.getJob(id)!;
  }

  updateJob(
    id: string,
    state: string,
    error = "",
    result?: unknown,
  ): JobRow | null {
    this.db
      .prepare(
        "UPDATE jobs SET state = ?, error = ?, result_json = COALESCE(?, result_json), updated_at = ? WHERE id = ?",
      )
      .run(
        state,
        error,
        result === undefined ? null : JSON.stringify(result),
        now(),
        id,
      );
    return this.getJob(id);
  }

  getJob(id: string): JobRow | null {
    return (
      (this.db
        .prepare("SELECT * FROM jobs WHERE id = ?")
        .get(id) as unknown as JobRow | undefined) ?? null
    );
  }

  listJobs(limit = 100): JobRow[] {
    return this.db
      .prepare("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?")
      .all(limit) as unknown as JobRow[];
  }

  // ----------------------------------------------------------------- events

  insertEvent(input: {
    collectorId: string;
    kind: string;
    severity: string;
    message: string;
    data?: unknown;
  }): void {
    this.db
      .prepare(
        "INSERT INTO events (collector_id, kind, severity, message, data_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
      )
      .run(
        input.collectorId,
        input.kind,
        input.severity,
        input.message,
        input.data === undefined ? null : JSON.stringify(input.data),
        now(),
      );
  }

  listEvents(limit = 200): EventRow[] {
    return this.db
      .prepare("SELECT * FROM events ORDER BY created_at DESC LIMIT ?")
      .all(limit) as unknown as EventRow[];
  }

  markStaleDevices(staleAfterMs: number): void {
    const cutoff = now() - staleAfterMs;
    this.db
      .prepare(
        "UPDATE devices SET state = 'offline' WHERE enabled = 1 AND state = 'online' AND last_seen < ?",
      )
      .run(cutoff);
  }

  latestValues(mac: string): unknown {
    const row = this.db
      .prepare("SELECT values_json FROM devices WHERE mac = ?")
      .get(mac) as unknown as { values_json: string | null } | undefined;
    return row?.values_json ? JSON.parse(row.values_json) : null;
  }
}
