import { useState } from "react";
import { Plus } from "lucide-react";
import { toast } from "sonner";

import { AddDeviceDialog } from "@/components/AddDeviceDialog";
import { StateBadge } from "@/components/StateBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, type Collector, type Device } from "@/lib/api";
import { formatTime } from "@/lib/format";

export function Devices({
  devices,
  collectors,
  refresh,
}: {
  devices: Device[];
  collectors: Collector[];
  refresh: () => void;
}) {
  const [addOpen, setAddOpen] = useState(false);

  const bind = async (device: Device, collectorId: string) => {
    try {
      if (collectorId === "none") await api.unbind(device.id);
      else await api.bind(device.id, collectorId);
      toast.success("绑定已更新");
      refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    }
  };

  const learn = async (device: Device) => {
    try {
      await api.learn(device.id, device.collectorId ?? undefined);
      toast.success("学习任务已下发");
      refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    }
  };

  const toggle = async (device: Device, enabled: boolean) => {
    try {
      await api.updateDevice(device.id, { enabled });
      refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">设备</h2>
        <Button onClick={() => setAddOpen(true)}>
          <Plus className="h-4 w-4" /> 录入设备
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm text-muted-foreground">
            共 {devices.length} 台，名称只在中控维护，采集器只认 MAC
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>名称</TableHead>
                <TableHead>MAC</TableHead>
                <TableHead>SN</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>采集器</TableHead>
                <TableHead>读数</TableHead>
                <TableHead>密钥</TableHead>
                <TableHead>启用</TableHead>
                <TableHead>操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {devices.map((device) => (
                <TableRow key={device.id}>
                  <TableCell>{device.name}</TableCell>
                  <TableCell className="font-mono text-xs">{device.mac}</TableCell>
                  <TableCell className="font-mono text-xs">
                    {device.sn ?? "—"}
                  </TableCell>
                  <TableCell>
                    <StateBadge state={device.state} />
                  </TableCell>
                  <TableCell>
                    <Select
                      value={device.collectorId ?? "none"}
                      onValueChange={(value) => void bind(device, value)}
                    >
                      <SelectTrigger className="w-32">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="none">未绑定</SelectItem>
                        {collectors.map((collector) => (
                          <SelectItem key={collector.id} value={collector.id}>
                            {collector.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </TableCell>
                  <TableCell className="text-xs">
                    {device.values
                      ? `${device.values.voltage.toFixed(1)} V / ${device.values.power.toFixed(1)} W`
                      : "—"}
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {device.payload ? `${device.payload} / 0x${(device.xor ?? 0).toString(16).toUpperCase()}` : "未学习"}
                  </TableCell>
                  <TableCell>
                    <Switch
                      checked={device.enabled}
                      onCheckedChange={(checked) => void toggle(device, checked)}
                    />
                  </TableCell>
                  <TableCell>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={!device.collectorId}
                      onClick={() => void learn(device)}
                    >
                      学习
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <AddDeviceDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        onCreated={refresh}
      />
    </div>
  );
}
