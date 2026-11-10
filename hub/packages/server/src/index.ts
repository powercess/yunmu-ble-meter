import fs from "node:fs";
import path from "node:path";
import Fastify from "fastify";
import cors from "@fastify/cors";
import fastifyStatic from "@fastify/static";

import { registerApi } from "./api.js";
import { config } from "./config.js";
import { Store } from "./db.js";
import { startMqtt } from "./mqtt.js";
import { sseHeartbeat } from "./sse.js";

const app = Fastify({ logger: true });
await app.register(cors, { origin: true });

const store = new Store();
const hub = await startMqtt(store);
registerApi(app, store, hub);

const webDist = path.resolve(config.webDist);
const indexHtml = path.join(webDist, "index.html");
if (fs.existsSync(indexHtml)) {
  await app.register(fastifyStatic, { root: webDist });
  app.setNotFoundHandler((req, reply) => {
    if (req.raw.url?.startsWith("/api/")) {
      return reply.code(404).send({ error: "not found" });
    }
    return reply.sendFile("index.html");
  });
} else {
  app.get("/", async () => ({
    name: "yunmu-hub",
    message: "web build not found; run pnpm --filter @yunmu/web build",
  }));
}

const staleTimer = setInterval(
  () => store.markStaleDevices(config.staleAfterMs),
  5_000,
);
const heartbeatTimer = setInterval(sseHeartbeat, 15_000);

await app.listen({ host: config.host, port: config.port });
app.log.info(
  {
    http: `http://${config.host}:${config.port}`,
    mqtt: config.externalMqttUrl || `mqtt://${config.host}:${config.mqttPort}`,
    db: config.dbPath,
  },
  "yunmu-hub ready",
);

const shutdown = async (signal: string) => {
  app.log.info({ signal }, "shutting down");
  clearInterval(staleTimer);
  clearInterval(heartbeatTimer);
  await app.close();
  await hub.close();
  process.exit(0);
};
process.on("SIGINT", () => void shutdown("SIGINT"));
process.on("SIGTERM", () => void shutdown("SIGTERM"));
