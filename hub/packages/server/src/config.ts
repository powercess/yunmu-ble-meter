import path from "node:path";

const num = (v: string | undefined, fallback: number) => {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : fallback;
};

export const config = {
  host: process.env.HOST ?? "0.0.0.0",
  port: num(process.env.PORT, 8080),
  mqttPort: num(process.env.MQTT_PORT, 1883),
  externalMqttUrl: process.env.MQTT_URL ?? "",
  dataDir: process.env.DATA_DIR ?? path.resolve("data"),
  dbPath:
    process.env.DB_PATH ??
    path.resolve(process.env.DATA_DIR ?? "data", "yunmu-hub.db"),
  webDist: process.env.WEB_DIST ?? path.resolve("../web/dist"),
  staleAfterMs: num(process.env.STALE_SECONDS, 30) * 1000,
  collectorOfflineAfterMs: num(process.env.COLLECTOR_OFFLINE_SECONDS, 15) * 1000,
};
