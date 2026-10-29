import { CheckCircle2, Clock, Loader2, XCircle } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import type { JobStatus } from "@/types";

const STATUS: Record<
  JobStatus,
  { label: string; variant: "default" | "success" | "destructive" | "warning" | "muted"; icon: typeof CheckCircle2 }
> = {
  queued: { label: "Dalam antrean", variant: "muted", icon: Clock },
  processing: { label: "Mengonversi", variant: "default", icon: Loader2 },
  completed: { label: "Selesai", variant: "success", icon: CheckCircle2 },
  failed: { label: "Gagal", variant: "destructive", icon: XCircle },
  expired: { label: "Kedaluwarsa", variant: "warning", icon: Clock },
};

export function JobStatusBadge({ status }: { status: JobStatus }) {
  const meta = STATUS[status] ?? STATUS.queued;
  const Icon = meta.icon;
  return (
    <Badge variant={meta.variant}>
      <Icon className={status === "processing" ? "h-3 w-3 animate-spin" : "h-3 w-3"} aria-hidden />
      {meta.label}
    </Badge>
  );
}
