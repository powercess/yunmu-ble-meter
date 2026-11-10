export type Capabilities = {
  maxLinks: number;
  maxReadsPerSecond: number;
  canScanWhileConnected: boolean;
  platform: string;
  firmware: string;
};

export type Collector = {
  id: string;
  name: string;
  version: string;
  platform: string;
  capabilities: Capabilities | null;
  epoch: number;
  status: "online" | "offline";
  lastSeen: number | null;
};

export type ReadingValues = {
  voltage: number;
  current: number;
  power: number;
  powerFactor: number;
  counter: number;
  threshold: number;
};

export type Device = {
  id: string;
  mac: string;
  sn: string | null;
  name: string;
  model: string;
  payload: string | null;
  xor: number | null;
  verified: boolean;
  enabled: boolean;
  collectorId: string | null;
  state: string;
  lastSeen: number | null;
  values: ReadingValues | null;
  stale: boolean;
};

export type Sighting = {
  mac: string;
  collectorId: string;
  rssi: number | null;
  seenAt: number;
  deviceId: string | null;
};

export type ScanSession = {
  id: string;
  state: string;
  createdAt: number;
  endsAt: number;
  sightings: Sighting[];
};

export type Job = {
  id: string;
  type: string;
  device_mac: string;
  collector_id: string;
  state: string;
  error: string;
  result_json: string | null;
  created_at: number;
};

export type HubEvent = {
  id: number;
  collector_id: string;
  kind: string;
  severity: string;
  message: string;
  created_at: number;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { error?: string };
    throw new Error(body.error ?? `${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

export const api = {
  collectors: () => request<Collector[]>("/api/collectors"),
  devices: () => request<Device[]>("/api/devices"),
  jobs: () => request<Job[]>("/api/jobs"),
  events: () => request<HubEvent[]>("/api/events"),
  createDevice: (body: {
    mac: string;
    name?: string;
    sn?: string;
    payload?: string;
    xor?: number;
  }) =>
    request<Device>("/api/devices", { method: "POST", body: JSON.stringify(body) }),
  updateDevice: (
    id: string,
    body: { name?: string; sn?: string; enabled?: boolean },
  ) =>
    request<Device>(`/api/devices/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  bind: (id: string, collectorId: string) =>
    request<Device>(`/api/devices/${id}/bind`, {
      method: "POST",
      body: JSON.stringify({ collectorId }),
    }),
  unbind: (id: string) =>
    request<Device>(`/api/devices/${id}/bind`, { method: "DELETE" }),
  learn: (id: string, collectorId?: string) =>
    request<{ jobId: string }>(`/api/devices/${id}/learn`, {
      method: "POST",
      body: JSON.stringify(collectorId ? { collectorId } : {}),
    }),
  startScan: (body: { durationSec?: number; collectorIds?: string[] }) =>
    request<{ id: string; endsAt: number; collectors: string[] }>(
      "/api/scan-sessions",
      { method: "POST", body: JSON.stringify(body) },
    ),
  scanSession: (id: string) =>
    request<ScanSession>(`/api/scan-sessions/${id}`),
};

export function subscribeHub(onEvent: (topic: string, data: unknown) => void) {
  const source = new EventSource("/api/stream");
  const types = ["collector", "reported", "telemetry", "sighting", "job", "event"];
  for (const type of types) {
    source.addEventListener(type, (event) => {
      const payload = JSON.parse((event as MessageEvent).data) as unknown;
      onEvent(type, payload);
    });
  }
  return () => source.close();
}
