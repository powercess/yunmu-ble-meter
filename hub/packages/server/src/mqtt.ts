import net from "node:net";
import { createRequire } from "node:module";
import mqtt, { type MqttClient } from "mqtt";

import {
  PROTOCOL_VERSION,
  decodeCollectorMessage,
  topics,
  type Command,
  type CollectorMessage,
} from "@yunmu/protocol";

import { config } from "./config.js";
import type { Store } from "./db.js";
import { sseBroadcast } from "./sse.js";

type BrokerInstance = {
  handle: (socket: net.Socket) => void;
  close: (callback?: () => void) => void;
};

type Broker = { close: () => Promise<void> };

async function startBroker(port: number): Promise<Broker> {
  const require = createRequire(import.meta.url);
  const aedesModule: { createBroker: () => BrokerInstance } = require("aedes");
  const aedes = aedesModule.createBroker();
  const server = net.createServer(aedes.handle);
  const listening = Promise.withResolvers<void>();
  server.once("error", listening.reject);
  server.listen(port, listening.resolve);
  await listening.promise;
  return {
    close: async () => {
      const closed = Promise.withResolvers<void>();
      server.close(() => aedes.close(closed.resolve));
      await closed.promise;
    },
  };
}

export type MqttHub = {
  publishDesired: (collectorId: string) => void;
  publishCommand: (collectorId: string, command: Command) => void;
  close: () => Promise<void>;
};

export async function startMqtt(store: Store): Promise<MqttHub> {
  const broker = config.externalMqttUrl
    ? null
    : await startBroker(config.mqttPort);
  const url = config.externalMqttUrl || `mqtt://127.0.0.1:${config.mqttPort}`;
  const client: MqttClient = mqtt.connect(url, {
    clientId: `yunmu-hub-${Math.random().toString(16).slice(2, 10)}`,
    clean: true,
    reconnectPeriod: 2000,
  });

  const publishDesired = (collectorId: string) => {
    const collector = store.getCollector(collectorId);
    if (!collector) return;
    const epoch = store.bumpEpoch(collectorId);
    const payload = JSON.stringify({
      type: "desired",
      v: PROTOCOL_VERSION,
      epoch,
      devices: store.desiredDevices(collectorId),
    });
    client.publish(topics.desired(collectorId), payload, {
      qos: 1,
      retain: true,
    });
  };

  const publishCommand = (collectorId: string, command: Command) => {
    client.publish(topics.cmd(collectorId), JSON.stringify(command), {
      qos: 1,
    });
  };

  const handle = (collectorId: string, msg: CollectorMessage) => {
    switch (msg.type) {
      case "hello": {
        store.upsertCollector(msg);
        sseBroadcast("collector", { collectorId });
        publishDesired(collectorId);
        break;
      }
      case "reported": {
        store.touchCollector(collectorId, "online");
        for (const device of msg.devices) {
          store.recordReportedDevice(
            device.mac,
            collectorId,
            device.state,
            device.lastReadAt,
            device.values,
          );
        }
        sseBroadcast("reported", { collectorId });
        break;
      }
      case "telemetry": {
        store.touchCollector(collectorId, "online");
        store.insertTelemetry(collectorId, msg.samples);
        sseBroadcast("telemetry", { collectorId, count: msg.samples.length });
        break;
      }
      case "sighting": {
        store.addSighting({
          sessionId: msg.sessionId,
          mac: msg.mac,
          collectorId,
          rssi: msg.rssi,
          seenAt: msg.seenAt,
        });
        sseBroadcast("sighting", { sessionId: msg.sessionId, mac: msg.mac });
        break;
      }
      case "job": {
        const job = store.updateJob(msg.jobId, msg.state, msg.error, msg.result);
        if (job && msg.state === "succeeded" && msg.result) {
          store.setDevicePayload(job.device_mac, msg.result.payload, msg.result.xor);
          publishDesired(job.collector_id);
        }
        sseBroadcast("job", { jobId: msg.jobId, state: msg.state });
        break;
      }
      case "event": {
        store.insertEvent({
          collectorId,
          kind: msg.kind,
          severity: msg.severity,
          message: msg.message,
          data: msg.data,
        });
        if (msg.kind === "offline") {
          store.touchCollector(collectorId, "offline");
        }
        sseBroadcast("event", { collectorId, kind: msg.kind });
        break;
      }
    }
  };

  client.on("connect", () => {
    client.subscribe([topics.collectorWildcard, topics.collectorJobsWildcard], {
      qos: 1,
    });
  });

  client.on("message", (topic, payload) => {
    const parts = topic.split("/");
    // yunmu/v1/collectors/{id}/{kind}[...]
    if (parts[0] !== "yunmu" || parts[1] !== "v1" || parts[2] !== "collectors") {
      return;
    }
    const collectorId = parts[3];
    if (!collectorId) return;
    const msg = decodeCollectorMessage(payload);
    if (!msg) return;
    handle(collectorId, msg);
  });

  return {
    publishDesired,
    publishCommand,
    close: async () => {
      const ended = Promise.withResolvers<void>();
      client.end(false, {}, () => ended.resolve());
      await ended.promise;
      if (broker) await broker.close();
    },
  };
}
