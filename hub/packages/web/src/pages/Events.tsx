import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { HubEvent } from "@/lib/api";
import { formatTime } from "@/lib/format";

export function Events({ events }: { events: HubEvent[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>事件</CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>时间</TableHead>
              <TableHead>采集器</TableHead>
              <TableHead>类型</TableHead>
              <TableHead>级别</TableHead>
              <TableHead>消息</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {events.map((event) => (
              <TableRow key={event.id}>
                <TableCell>{formatTime(event.created_at)}</TableCell>
                <TableCell>{event.collector_id}</TableCell>
                <TableCell>{event.kind}</TableCell>
                <TableCell>{event.severity}</TableCell>
                <TableCell className="text-muted-foreground">
                  {event.message}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
