"use client";

import { FileWarning, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";

import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { cn, formatBytes } from "@/lib/utils";

interface ImageTile {
  file: File;
  url: string | null;
  width?: number;
  height?: number;
  error?: string;
}

function ImageTile({ item, pageNumber }: { item: ImageTile; pageNumber: number }) {
  const [status, setStatus] = useState<"loading" | "ready" | "error">(
    item.url ? "loading" : "error",
  );

  useEffect(() => {
    setStatus(item.url ? "loading" : "error");
  }, [item.url]);

  return (
    <div>
      <div className="relative flex min-h-24 items-center justify-center bg-muted/40">
        {item.url ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={item.url}
            alt={`Pratinjau halaman ${pageNumber}: ${item.file.name}`}
            loading="lazy"
            onLoad={() => setStatus("ready")}
            onError={() => setStatus("error")}
            className={cn(
              "block max-h-72 w-full object-contain",
              status === "loading" && "opacity-0",
            )}
          />
        ) : null}
        {status === "loading" && (
          <span className="absolute inset-0 flex flex-col items-center justify-center gap-1 text-[11px] text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Memuat gambar…
          </span>
        )}
        {status === "error" && (
          <span className="flex flex-col items-center justify-center gap-1 p-4 text-center text-[11px] text-destructive">
            <FileWarning className="h-4 w-4" />
            Gambar tidak dapat ditampilkan.
          </span>
        )}
      </div>
      <div className="flex items-center justify-between gap-2 px-2 py-1 text-[10px] text-muted-foreground">
        <span className="truncate">Halaman {pageNumber} · {item.file.name}</span>
        <span className="shrink-0">{formatBytes(item.file.size)}</span>
      </div>
    </div>
  );
}

export function ImagePreviewGrid({ files }: { files: File[] }) {
  const [items, setItems] = useState<ImageTile[]>([]);
  const [zoom, setZoom] = useState<ImageTile | null>(null);

  useEffect(() => {
    const urls: string[] = [];
    const next = files.map((file) => {
      try {
        const url = URL.createObjectURL(file);
        urls.push(url);
        return { file, url };
      } catch {
        return { file, url: null, error: "Object URL tidak dapat dibuat." };
      }
    });
    setItems(next);
    setZoom(null);

    // Object URLs are always released, including when the file list changes.
    return () => {
      urls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [files]);

  if (files.length === 0) return null;

  return (
    <div className="rounded-lg border bg-muted/20 p-3">
      <p className="mb-2 text-xs text-muted-foreground">
        {files.length} gambar dipilih · pratinjau seluruh halaman
      </p>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {items.map((item, index) => (
          <figure
            key={`${item.file.name}-${item.file.size}-${item.file.lastModified}-${index}`}
            className="overflow-hidden rounded-md border bg-white"
          >
            <button
              type="button"
              className="block w-full cursor-zoom-in text-left"
              onClick={() => item.url && setZoom(item)}
            >
              <ImageTile item={item} pageNumber={index + 1} />
            </button>
          </figure>
        ))}
      </div>

      <Dialog
        open={zoom !== null}
        onOpenChange={(open) => {
          if (!open) setZoom(null);
        }}
      >
        <DialogContent className="max-w-[min(96vw,1100px)] p-3">
          <DialogTitle className="text-sm">
            {zoom ? `Gambar halaman ${items.findIndex((item) => item === zoom) + 1}` : "Pratinjau"}
          </DialogTitle>
          {zoom?.url && (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={zoom.url}
              alt={`Pratinjau besar ${zoom.file.name}`}
              className="mx-auto max-h-[80vh] w-auto max-w-full object-contain"
            />
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
