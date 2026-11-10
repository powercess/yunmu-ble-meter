import { useState } from "react";
import {
  Cpu,
  LayoutDashboard,
  Radar,
  RadioTower,
  ScrollText,
} from "lucide-react";

import { useHubData } from "@/hooks/useHubData";
import { cn } from "@/lib/utils";
import { Collectors } from "@/pages/Collectors";
import { Dashboard } from "@/pages/Dashboard";
import { Devices } from "@/pages/Devices";
import { Events } from "@/pages/Events";
import { Scan } from "@/pages/Scan";

type Tab = "dashboard" | "devices" | "collectors" | "scan" | "events";

const NAV: { id: Tab; label: string; icon: typeof LayoutDashboard }[] = [
  { id: "dashboard", label: "概览", icon: LayoutDashboard },
  { id: "devices", label: "设备", icon: Cpu },
  { id: "collectors", label: "采集器", icon: RadioTower },
  { id: "scan", label: "扫描", icon: Radar },
  { id: "events", label: "事件", icon: ScrollText },
];

export default function App() {
  const [tab, setTab] = useState<Tab>("dashboard");
  const { collectors, devices, events, error, refresh } = useHubData();

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-4">
          <div className="flex items-center gap-3">
            <div className="flex h-8 w-8 items-center justify-center rounded-md bg-primary text-primary-foreground">
              Y
            </div>
            <div>
              <h1 className="text-lg font-semibold">YunMu Hub</h1>
              <p className="text-xs text-muted-foreground">
                采集器与蓝牙功率计统一管理
              </p>
            </div>
          </div>
          <div className="text-xs text-muted-foreground">
            {collectors.filter((c) => c.status === "online").length}/
            {collectors.length} 采集器在线
          </div>
        </div>
      </header>

      <div className="mx-auto flex max-w-7xl gap-6 px-6 py-6">
        <nav className="w-40 shrink-0 space-y-1">
          {NAV.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                onClick={() => setTab(item.id)}
                className={cn(
                  "flex w-full items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors",
                  tab === item.id
                    ? "bg-secondary text-secondary-foreground"
                    : "text-muted-foreground hover:bg-secondary/60 hover:text-foreground",
                )}
              >
                <Icon className="h-4 w-4" />
                {item.label}
              </button>
            );
          })}
        </nav>

        <main className="min-w-0 flex-1">
          {error ? (
            <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          ) : null}
          {tab === "dashboard" ? (
            <Dashboard collectors={collectors} devices={devices} events={events} />
          ) : null}
          {tab === "devices" ? (
            <Devices devices={devices} collectors={collectors} refresh={refresh} />
          ) : null}
          {tab === "collectors" ? (
            <Collectors collectors={collectors} devices={devices} />
          ) : null}
          {tab === "scan" ? (
            <Scan collectors={collectors} refresh={refresh} />
          ) : null}
          {tab === "events" ? <Events events={events} /> : null}
        </main>
      </div>
    </div>
  );
}
