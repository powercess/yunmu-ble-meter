import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api";

export function AddDeviceDialog({
  open,
  onOpenChange,
  onCreated,
  initialMac,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: () => void;
  initialMac?: string;
}) {
  const [mac, setMac] = useState(initialMac ?? "");
  const [name, setName] = useState("");
  const [sn, setSn] = useState("");
  const [payload, setPayload] = useState("");
  const [xor, setXor] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      const parsedXor = xor.trim() === "" ? undefined : Number(xor);
      await api.createDevice({
        mac,
        name: name.trim() || undefined,
        sn: sn.trim() || undefined,
        payload: payload.trim() || undefined,
        xor: Number.isNaN(parsedXor) ? undefined : parsedXor,
      });
      toast.success("设备已录入");
      onOpenChange(false);
      onCreated();
      setName("");
      setSn("");
      setPayload("");
      setXor("");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>录入设备</DialogTitle>
          <DialogDescription>
            MAC 是唯一键；payload/xor 可以稍后通过学习获取。
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-3">
          <div className="grid gap-1.5">
            <Label htmlFor="mac">MAC</Label>
            <Input
              id="mac"
              placeholder="26:04:18:01:12:f4"
              value={mac}
              onChange={(event) => setMac(event.target.value)}
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-1.5">
              <Label htmlFor="name">名称</Label>
              <Input
                id="name"
                placeholder="1号机"
                value={name}
                onChange={(event) => setName(event.target.value)}
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="sn">SN</Label>
              <Input
                id="sn"
                placeholder="KK01264I12F4"
                value={sn}
                onChange={(event) => setSn(event.target.value)}
              />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-1.5">
              <Label htmlFor="payload">read_payload</Label>
              <Input
                id="payload"
                placeholder="a4aca4"
                value={payload}
                onChange={(event) => setPayload(event.target.value)}
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="xor">payload_xor</Label>
              <Input
                id="xor"
                placeholder="0x2C"
                value={xor}
                onChange={(event) => setXor(event.target.value)}
              />
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button disabled={busy || !mac.trim()} onClick={() => void submit()}>
            {busy ? "提交中…" : "录入"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
