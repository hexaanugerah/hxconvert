import { FileStack } from "lucide-react";
import Link from "next/link";

import { cn } from "@/lib/utils";

export function Logo({ className, showText = true }: { className?: string; showText?: boolean }) {
  return (
    <Link href="/" className={cn("flex items-center gap-2.5", className)} aria-label="HXconvertX - Beranda">
      <span className="grid h-9 w-9 place-items-center rounded-xl bg-primary text-primary-foreground shadow-sm">
        <FileStack className="h-5 w-5" aria-hidden />
      </span>
      {showText && (
        <span className="text-lg font-semibold tracking-tight">
          HX<span className="text-primary">convertX</span>
        </span>
      )}
    </Link>
  );
}
