"use client";

import type { PDFDocumentProxy } from "pdfjs-dist";
import { FileWarning, Loader2, RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

// Rendering more than a couple of pages at once can exhaust memory on large
// documents. Override at build time with NEXT_PUBLIC_PREVIEW_CONCURRENCY.
const PREVIEW_CONCURRENCY = Math.max(
  1,
  Math.min(4, Number(process.env.NEXT_PUBLIC_PREVIEW_CONCURRENCY ?? 2) || 2),
);
const PREVIEW_MAX_WIDTH = 900;
const PREVIEW_MAX_SCALE = 1.6;
const IDLE_TICK_MS = 80;

type PageStatus = "pending" | "rendering" | "ready" | "error";

interface PreviewPage {
  pageNumber: number;
  status: PageStatus;
  url?: string;
  width?: number;
  height?: number;
  error?: string;
}

export function PdfPreview({ file }: { file: File }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [pages, setPages] = useState<PreviewPage[]>([]);
  const [pageCount, setPageCount] = useState<number | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [fallbackUrl, setFallbackUrl] = useState<string | null>(null);
  const [retryKey, setRetryKey] = useState(0);
  const [zoomPage, setZoomPage] = useState<PreviewPage | null>(null);

  useEffect(() => {
    let cancelled = false;
    let documentProxy: PDFDocumentProxy | null = null;
    let loadingTask: { destroy: () => Promise<void> } | null = null;
    const createdUrls: string[] = [];
    let fallback: string | null = null;

    setPages([]);
    setPageCount(null);
    setFatal(null);
    setFallbackUrl(null);
    setZoomPage(null);

    (async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        pdfjs.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs";
        const data = new Uint8Array(await file.arrayBuffer());
        const task = pdfjs.getDocument({ data, isEvalSupported: false });
        loadingTask = task as unknown as { destroy: () => Promise<void> };
        documentProxy = await task.promise;
        if (cancelled) return;

        const total = documentProxy.numPages;
        setPageCount(total);
        setPages(
          Array.from({ length: total }, (_, index) => ({
            pageNumber: index + 1,
            status: "pending" as PageStatus,
          })),
        );

        const targetWidth = Math.max(
          280,
          Math.min(PREVIEW_MAX_WIDTH, (containerRef.current?.clientWidth ?? 720) - 8),
        );
        const pageStatuses = new Map<number, PageStatus>(
          Array.from({ length: total }, (_, index) => [index + 1, "pending" as PageStatus]),
        );

        // Lazy rendering: pages are queued only while their placeholder is on
        // screen; off-screen pages keep a lightweight placeholder.
        const visiblePages = new Set<number>();
        if (typeof IntersectionObserver === "undefined") {
          // No observer support: behave like the old eager renderer.
          for (const pageNumber of pageStatuses.keys()) visiblePages.add(pageNumber);
        }

        const renderPage = async (pageNumber: number) => {
          pageStatuses.set(pageNumber, "rendering");
          setPages((current) =>
            current.map((page) =>
              page.pageNumber === pageNumber ? { ...page, status: "rendering" } : page,
            ),
          );

          const canvas = document.createElement("canvas");
          try {
            const page = await documentProxy!.getPage(pageNumber);
            if (cancelled) {
              page.cleanup();
              return;
            }
            const base = page.getViewport({ scale: 1 });
            const scale = Math.min(PREVIEW_MAX_SCALE, targetWidth / base.width);
            const viewport = page.getViewport({ scale });
            const ratio = Math.min(2, window.devicePixelRatio || 1);
            canvas.width = Math.max(1, Math.floor(viewport.width * ratio));
            canvas.height = Math.max(1, Math.floor(viewport.height * ratio));
            const context = canvas.getContext("2d");
            if (!context) throw new Error("Canvas 2D tidak tersedia di peramban ini.");

            await page.render({
              canvasContext: context,
              viewport,
              transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : undefined,
            }).promise;
            page.cleanup();

            const blob = await new Promise<Blob | null>((resolve) =>
              canvas.toBlob(resolve, "image/png"),
            );
            if (!blob) throw new Error("Hasil render halaman kosong.");
            const url = URL.createObjectURL(blob);
            createdUrls.push(url);
            if (!cancelled) {
              setPages((current) =>
                current.map((page) =>
                  page.pageNumber === pageNumber
                    ? {
                        ...page,
                        status: "ready",
                        url,
                        width: Math.round(viewport.width),
                        height: Math.round(viewport.height),
                      }
                    : page,
                ),
              );
            }
          } catch (error) {
            if (!cancelled) {
              setPages((current) =>
                current.map((page) =>
                  page.pageNumber === pageNumber
                    ? {
                        ...page,
                        status: "error",
                        error:
                          error instanceof Error
                            ? error.message
                            : "Halaman ini gagal dirender.",
                      }
                    : page,
                ),
              );
            }
          } finally {
            canvas.width = 0;
            canvas.height = 0;
          }
        };

        const scheduler = async () => {
          const inFlight = () =>
            Array.from(pageStatuses.values()).filter((status) => status === "rendering").length;

          while (!cancelled) {
            let started = false;
            while (!cancelled && inFlight() < PREVIEW_CONCURRENCY) {
              const next = Array.from(pageStatuses.entries()).find(
                ([pageNumber, status]) =>
                  status === "pending" && (visiblePages.has(pageNumber) || visiblePages.size === 0),
              )?.[0];
              if (!next) break;
              visiblePages.delete(next);
              void renderPage(next);
              started = true;
            }
            if (!started && !cancelled) {
              await new Promise((resolve) => window.setTimeout(resolve, IDLE_TICK_MS));
            }
          }
        };

        if (typeof IntersectionObserver !== "undefined") {
          // The placeholders render right after setPages; observe them as soon
          // as they exist (retry a few ticks in case of slow paints).
          let attempts = 0;
          const observer = new IntersectionObserver(
            (entries) => {
              for (const entry of entries) {
                const pageNumber = Number((entry.target as HTMLElement).dataset.previewPage);
                if (!pageNumber) continue;
                if (entry.isIntersecting) {
                  visiblePages.add(pageNumber);
                } else {
                  visiblePages.delete(pageNumber);
                }
              }
            },
            { rootMargin: "240px" },
          );

          const attach = () => {
            if (cancelled || attempts > 20 || observerStarted) return;
            const elements = containerRef.current?.querySelectorAll<HTMLElement>(
              "[data-preview-page]",
            );
            if (elements && elements.length >= total) {
              elements.forEach((element) => observer.observe(element));
              observerStarted = true;
              return;
            }
            attempts += 1;
            window.setTimeout(attach, 50);
          };
          let observerStarted = false;
          attach();
        }

        void scheduler();
      } catch (error) {
        if (cancelled) return;
        const message =
          error instanceof Error ? error.message : "Dokumen PDF tidak dapat dibaca.";
        setFatal(message);
        try {
          fallback = URL.createObjectURL(file);
          setFallbackUrl(fallback);
        } catch {
          fallback = null;
        }
      }
    })();

    return () => {
      cancelled = true;
      createdUrls.forEach((url) => URL.revokeObjectURL(url));
      if (fallback) URL.revokeObjectURL(fallback);
      void documentProxy?.destroy?.();
      void loadingTask?.destroy?.();
    };
  }, [file, retryKey]);

  const readyCount = pages.filter((page) => page.status === "ready").length;
  const failedCount = pages.filter((page) => page.status === "error").length;
  const loading = pageCount !== null && readyCount + failedCount < pageCount;
  const showDialog = zoomPage !== null;

  return (
    <div ref={containerRef} className="rounded-lg border bg-muted/20 p-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          {pageCount === null
            ? "Memuat dokumen PDF…"
            : `${readyCount} dari ${pageCount} halaman siap${failedCount ? ` · ${failedCount} gagal` : ""}`}
        </p>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => setRetryKey((value) => value + 1)}
        >
          <RefreshCw className="h-3.5 w-3.5" /> Muat ulang
        </Button>
      </div>

      {fatal ? (
        <div className="space-y-2">
          <div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 p-2 text-xs">
            <FileWarning className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
            <span>
              Pratinjau PDF.js gagal: {fatal}. Viewer bawaan peramban dipakai sebagai gantinya.
            </span>
          </div>
          {fallbackUrl && (
            <iframe src={fallbackUrl} title="Pratinjau PDF" className="h-[420px] w-full" />
          )}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {pages.map((page) => (
            <figure key={page.pageNumber} className="overflow-hidden rounded-md border bg-white">
              <button
                type="button"
                data-preview-page={page.pageNumber}
                onClick={() => page.status === "ready" && setZoomPage(page)}
                className={cn(
                  "relative flex w-full items-center justify-center bg-muted/40",
                  page.status === "ready" ? "cursor-zoom-in" : "cursor-default",
                )}
                aria-label={`Halaman ${page.pageNumber}`}
              >
                {page.status === "ready" && page.url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={page.url}
                    alt={`Halaman ${page.pageNumber} dari ${pageCount ?? "?"}`}
                    width={page.width}
                    height={page.height}
                    className="block h-auto w-full"
                  />
                ) : (
                  <span
                    className={cn(
                      "flex w-full flex-col items-center justify-center gap-1 py-10 text-[11px] text-muted-foreground",
                      page.status === "error" && "text-destructive",
                    )}
                  >
                    {page.status === "error" ? (
                      <>
                        <FileWarning className="h-4 w-4" />
                        <span className="max-w-[90%] text-center">
                          {page.error ?? "Gagal dirender."}
                        </span>
                      </>
                    ) : (
                      <>
                        <Loader2 className="h-4 w-4 animate-spin" />
                        <span>{page.status === "rendering" ? "Merender…" : "Menunggu…"}</span>
                      </>
                    )}
                  </span>
                )}
              </button>
              <figcaption className="flex items-center justify-between px-2 py-1 text-[10px] text-muted-foreground">
                <span>Halaman {page.pageNumber}</span>
                <span>
                  {page.status === "ready"
                    ? "Siap"
                    : page.status === "error"
                      ? "Gagal"
                      : "Memuat"}
                </span>
              </figcaption>
            </figure>
          ))}
        </div>
      )}

      {loading && (
        <p className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          Merender halaman secara bertahap ({readyCount}/{pageCount})…
        </p>
      )}

      {failedCount > 0 && (
        <p className="mt-2 text-xs text-destructive">
          {failedCount} halaman gagal dirender. Coba muat ulang atau gunakan berkas lain.
        </p>
      )}

      <Dialog
        open={showDialog}
        onOpenChange={(open) => {
          if (!open) setZoomPage(null);
        }}
      >
        <DialogContent className="max-w-[min(96vw,1100px)] p-3">
          <DialogTitle className="text-sm">
            Halaman {zoomPage?.pageNumber} dari {pageCount ?? "?"}
          </DialogTitle>
          {zoomPage?.url && (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={zoomPage.url}
              alt={`Pratinjau besar halaman ${zoomPage.pageNumber}`}
              className="mx-auto max-h-[80vh] w-auto max-w-full object-contain"
            />
          )}
        </DialogContent>
      </Dialog>

    </div>
  );
}
