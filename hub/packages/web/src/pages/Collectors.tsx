import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StateBadge } from "@/components/StateBadge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { Collector, Device } from "@/lib/api";
import { formatTime } from "@/lib/format";

export function Collectors({
  collectors,
  devices,
}: {
  collectors: Collector[];
  devices: Device[];
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>采集器</CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>名称</TableHead>
              <TableHead>ID</TableHead>
              <TableHead>状态</TableHead>
              <TableHead>平台</TableHead>
              <TableHead>版本</TableHead>
              <TableHead>能力</TableHead>
              <TableHead>设备</TableHead>
              <TableHead>最后心跳</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {collectors.map((collector) => {
              const bound = devices.filter(
                (device) => device.collectorId === collector.id,
              ).length;
              return (
                <TableRow key={collector.id}>
                  <TableCell>{collector.name}</TableCell>
                  <TableCell className="font-mono text-xs">
                    {collector.id}
                  </TableCell>
                  <TableCell>
                    <StateBadge state={collector.status} />
                  </TableCell>
                  <TableCell>{collector.platform || "—"}</TableCell>
                  <TableCell className="text-xs">{collector.version}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {collector.capabilities
                      ? `${collector.capabilities.maxLinks} 链接 / ${collector.capabilities.maxReadsPerSecond} 读/秒`
                      : "—"}
                  </TableCell>
                  <TableCell>{bound}</TableCell>
                  <TableCell>{formatTime(collector.lastSeen)}</TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
