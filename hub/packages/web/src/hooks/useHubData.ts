import { useCallback, useEffect, useRef, useState } from "react";

import {
  api,
  subscribeHub,
  type Collector,
  type Device,
  type HubEvent,
  type Job,
} from "@/lib/api";

export function useHubData() {
  const [collectors, setCollectors] = useState<Collector[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [events, setEvents] = useState<HubEvent[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [nextCollectors, nextDevices, nextEvents, nextJobs] =
        await Promise.all([
          api.collectors(),
          api.devices(),
          api.events(),
          api.jobs(),
        ]);
      setCollectors(nextCollectors);
      setDevices(nextDevices);
      setEvents(nextEvents);
      setJobs(nextJobs);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  const debounce = useRef<number | null>(null);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 5000);
    const unsubscribe = subscribeHub(() => {
      if (debounce.current !== null) window.clearTimeout(debounce.current);
      debounce.current = window.setTimeout(() => void refresh(), 300);
    });
    return () => {
      window.clearInterval(interval);
      unsubscribe();
      if (debounce.current !== null) window.clearTimeout(debounce.current);
    };
  }, [refresh]);

  return { collectors, devices, events, jobs, error, refresh };
}
