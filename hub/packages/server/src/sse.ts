import type { ServerResponse } from "node:http";

const clients = new Set<ServerResponse>();

export function sseAdd(res: ServerResponse): void {
  clients.add(res);
  res.write("retry: 3000\n\n");
  res.on("close", () => clients.delete(res));
}

export function sseRemove(res: ServerResponse): void {
  clients.delete(res);
}

export function sseBroadcast(event: string, data: unknown): void {
  const frame = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const res of clients) {
    try {
      res.write(frame);
    } catch {
      clients.delete(res);
    }
  }
}

export function sseHeartbeat(): void {
  for (const res of clients) {
    try {
      res.write(": ping\n\n");
    } catch {
      clients.delete(res);
    }
  }
}
