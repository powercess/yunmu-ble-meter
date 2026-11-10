import { Badge } from "@/components/ui/badge";

const MAP: Record<string, { label: string; variant: "success" | "destructive" | "secondary" | "warning" }> = {
  online: { label: "在线", variant: "success" },
  offline: { label: "离线", variant: "destructive" },
  error: { label: "错误", variant: "destructive" },
  disabled: { label: "已禁用", variant: "secondary" },
  learning: { label: "学习中", variant: "warning" },
  unknown: { label: "未知", variant: "secondary" },
};

export function StateBadge({ state }: { state: string }) {
  const entry = MAP[state] ?? { label: state, variant: "secondary" as const };
  return <Badge variant={entry.variant}>{entry.label}</Badge>;
}
