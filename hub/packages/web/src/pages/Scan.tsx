import { useEffect, useState } from "react";
import { Radar } from "lucide-react";
import { toast } from "sonner";

import { AddDeviceDialog } from "@/components/AddDeviceDialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, type Collector, type ScanSession } from "@/lib/api";
import { formatTime } from "@/lib/format";

export function Scan({
  collectors,
  refresh,
}: {
  collectors: Collector[];
  refresh: () => void;
}) {
  const [session, setSession] = useState<ScanSession | null>(null);
  const [busy, setBusy] = useState(false);
  const [addMac, setAddMac] = useState<string | undefined>();
  const [addOpen, setAddOpen] = useState(false);

  const start = async () => {
    setBusy(true);
    try {
      const created = await api.startScan({ durationSec: 120 });
      const next = await api.scanSession(created.id);
      setSession(next);
      toast.success(`扫描已启动，${created.collectors.length} 台采集器参与`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (!session || session.state !== "active") return;
    const timer = window.setInterval(() => {
      void api
        .scanSession(session.id)
        .then(setSession)
        .catch(() => undefined);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [session?.id, session?.state]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">扫描新设备</h2>
          <p className="text-sm text-muted-foreground">
            开始扫描后，请给新设备断电重启，让它在广播窗口内被发现。
          </p>
        </div>
        <Button disabled={busy || collectors.length === 0} onClick={() => void start()}>
          <Radar className="h-4 w-4" />
          {busy ? "启动中…" : "开始扫描 (120s)"}
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm text-muted-foreground">
            {session
              ? `会话 ${session.id.slice(0, 8)} · ${session.state} · 截止 ${formatTime(session.endsAt)}`
              : "尚未启动扫描"}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>MAC</TableHead>
                <TableHead>采集器</TableHead>
                <TableHead>RSSI</TableHead>
                <TableHead>发现时间</TableHead>
                <TableHead>操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(session?.sightings ?? []).map((sighting) => (
                <TableRow key={`${sighting.mac}-${sighting.collectorId}`}>
                  <TableCell className="font-mono text-xs">
                    {sighting.mac}
                  </TableCell>
                  <TableCell>{sighting.collectorId}</TableCell>
                  <TableCell>{sighting.rssi ?? "—"}</TableCell>
                  <TableCell>{formatTime(sighting.seenAt)}</TableCell>
                  <TableCell>
                    {sighting.deviceId ? (
                      <span className="text-xs text-muted-foreground">已录入</span>
                    ) : (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => {
                          setAddMac(sighting.mac);
                          setAddOpen(true);
                        }}
                      >
                        录入
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
              {session && session.sightings.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={5} className="text-muted-foreground">
                    等待采集器上报…
                  </TableCell>
                </TableRow>
              ) : null}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <AddDeviceDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        initialMac={addMac}
        onCreated={() => {
          refresh();
          if (session) {
            void api.scanSession(session.id).then(setSession).catch(() => undefined);
          }
        }}
      />
    </div>
  );
}
