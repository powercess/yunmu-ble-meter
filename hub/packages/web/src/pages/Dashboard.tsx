import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { StateBadge } from "@/components/StateBadge";
import type { Collector, Device, HubEvent } from "@/lib/api";
import { formatTime } from "@/lib/format";

export function Dashboard({
  collectors,
  devices,
  events,
}: {
  collectors: Collector[];
  devices: Device[];
  events: HubEvent[];
}) {
  const onlineCollectors = collectors.filter((c) => c.status === "online").length;
  const activeDevices = devices.filter((d) => d.enabled);
  const onlineDevices = activeDevices.filter((d) => d.state === "online").length;
  const staleDevices = activeDevices.filter((d) => d.stale);

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">采集器</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-semibold">{onlineCollectors}</div>
            <div className="text-xs text-muted-foreground">
              在线 / {collectors.length} 台
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">设备</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-semibold">{devices.length}</div>
            <div className="text-xs text-muted-foreground">
              {onlineDevices} 在线 / {activeDevices.length} 启用
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">异常</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-semibold text-destructive">
              {staleDevices.length}
            </div>
            <div className="text-xs text-muted-foreground">超过 30 秒无数据</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">事件</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-semibold">{events.length}</div>
            <div className="text-xs text-muted-foreground">最近记录</div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>异常设备</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>名称</TableHead>
                <TableHead>MAC</TableHead>
                <TableHead>采集器</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>最后数据</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {staleDevices.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={5} className="text-muted-foreground">
                    没有异常设备
                  </TableCell>
                </TableRow>
              ) : (
                staleDevices.map((device) => (
                  <TableRow key={device.id}>
                    <TableCell>{device.name}</TableCell>
                    <TableCell className="font-mono text-xs">{device.mac}</TableCell>
                    <TableCell>{device.collectorId ?? "—"}</TableCell>
                    <TableCell>
                      <StateBadge state={device.state} />
                    </TableCell>
                    <TableCell>{formatTime(device.lastSeen)}</TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>最近事件</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>时间</TableHead>
                <TableHead>采集器</TableHead>
                <TableHead>类型</TableHead>
                <TableHead>消息</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {events.slice(0, 10).map((event) => (
                <TableRow key={event.id}>
                  <TableCell>{formatTime(event.created_at)}</TableCell>
                  <TableCell>{event.collector_id}</TableCell>
                  <TableCell>{event.kind}</TableCell>
                  <TableCell className="text-muted-foreground">
                    {event.message}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
