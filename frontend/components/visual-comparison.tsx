"use client";

import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Columns2,
  Download,
  Highlighter,
  Loader2,
  ScanEye,
  XCircle,
} from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { API_URL } from "@/lib/api";
import { cn, formatBytes } from "@/lib/utils";
import { getComparison, getJob } from "@/services/conversions";
import type { ComparisonPage } from "@/types";

function imageUrl(path: string): string {
  return `${API_URL}${path}`;
}

function toneFor(page: ComparisonPage): string {
  if (page.ratio < 0.001) return "bg-emerald-500";
  if (page.ratio < 0.01) return "bg-amber-500";
  return "bg-red-500";
}

function badgeVariant(status: string): "default" | "secondary" | "destructive" | "outline" {
  if (status === "Sama") return "secondary";
  if (status === "Mirip") return "outline";
  return "destructive";
}

function percent(value: number): string {
  return `${(value * 100).toFixed(3)}%`;
}

function Panes({
  page,
  mode,
}: {
  page: ComparisonPage;
  mode: "side" | "diff";
}) {
  const figure = (src: string, label: string, hint: string) => (
    <figure className="min-w-0 space-y-2">
      <figcaption className="flex items-center justify-between gap-2 text-xs font-medium text-muted-foreground">
        <span>{label}</span>
        <span className="font-normal">{hint}</span>
      </figcaption>
      <a
        href={imageUrl(src)}
        target="_blank"
        rel="noreferrer"
        className="block overflow-hidden rounded-lg border bg-white shadow-sm transition hover:shadow-md"
        title="Buka gambar ukuran penuh"
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={imageUrl(src)}
          alt={`${label} halaman ${page.page}`}
          loading="lazy"
          className="h-auto w-full"
        />
      </a>
    </figure>
  );

  if (mode === "diff") {
    return (
      <div className="grid gap-4 lg:grid-cols-2">
        {figure(page.original_url, "Asli", "piksel berbeda ditandai merah")}
        {figure(page.difference_url, "Sorotan selisih", percent(page.ratio))}
      </div>
    );
  }

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {figure(page.original_url, "Berkas asli", "halaman ini")}
      {figure(page.result_url, "Hasil konversi", percent(page.ratio))}
    </div>
  );
}

export function VisualComparison({ jobId }: { jobId: string }) {
  const [pageIndex, setPageIndex] = useState(0);
  const [mode, setMode] = useState<"side" | "diff">("side");

  const comparisonQuery = useQuery({
    queryKey: ["comparison", jobId],
    queryFn: () => getComparison(jobId),
    retry: false,
    staleTime: 5 * 60_000,
  });
  const jobQuery = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => getJob(jobId),
    retry: false,
  });

  const comparison = comparisonQuery.data;
  const pages = useMemo(() => comparison?.pages ?? [], [comparison]);
  const current = pages[Math.min(pageIndex, Math.max(0, pages.length - 1))];
  const job = jobQuery.data;

  return (
    <div className="container py-10">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="flex items-center gap-2 text-3xl font-semibold tracking-tight">
            <ScanEye className="h-7 w-7 text-primary" /> Banding visual
          </h1>
          <p className="mt-1 max-w-2xl text-muted-foreground">
            Halaman asli dibandingkan dengan hasil konversi pada resolusi yang sama.
            Selisih kecil dari perenderan font dianggap mirip; sorotan merah menandai
            piksel yang benar-benar berbeda.
          </p>
          {job && (
            <p className="mt-2 truncate text-sm text-muted-foreground">
              {job.original_filename}
              {job.output_filename ? ` → ${job.output_filename}` : ""}
              {job.output_size ? ` · ${formatBytes(job.output_size)}` : ""}
            </p>
          )}
        </div>
        <div className="flex gap-2">
          <Button variant="outline" asChild>
            <Link href="/history">
              <ArrowLeft className="h-4 w-4" /> Riwayat
            </Link>
          </Button>
          {job?.download_url && (
            <Button asChild>
              <a href={imageUrl(job.download_url)} download>
                <Download className="h-4 w-4" /> Unduh hasil
              </a>
            </Button>
          )}
        </div>
      </div>

      {comparisonQuery.isLoading && (
        <div className="mt-6 space-y-4">
          <Skeleton className="h-24" />
          <div className="grid gap-4 lg:grid-cols-2">
            <Skeleton className="h-[60vh]" />
            <Skeleton className="h-[60vh]" />
          </div>
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Merender kedua berkas dan
            menghitung selisih halaman. Perbandingan pertama bisa memakan beberapa detik.
          </p>
        </div>
      )}

      {comparisonQuery.isError && (
        <div className="mt-6 flex items-start gap-3 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm">
          <XCircle className="mt-0.5 h-4 w-4 text-destructive" />
          <div>
            <p className="font-medium">Perbandingan tidak dapat dimuat</p>
            <p className="text-muted-foreground">{(comparisonQuery.error as Error).message}</p>
            <Button
              variant="outline"
              size="sm"
              className="mt-3"
              onClick={() => void comparisonQuery.refetch()}
              disabled={comparisonQuery.isFetching}
            >
              Coba lagi
            </Button>
          </div>
        </div>
      )}

      {comparison && !comparison.available && (
        <Card className="mt-6">
          <CardContent className="flex items-start gap-3 p-6 text-sm">
            <AlertTriangle className="mt-0.5 h-5 w-5 text-amber-500" />
            <div>
              <p className="font-medium">Banding visual tidak tersedia</p>
              <p className="mt-1 text-muted-foreground">{comparison.reason}</p>
              <Button variant="outline" size="sm" className="mt-4" asChild>
                <Link href="/history">Kembali ke riwayat</Link>
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {comparison?.available && current && (
        <>
          <div className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Card>
              <CardContent className="p-4">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">
                  Halaman dibandingkan
                </p>
                <p className="mt-1 text-2xl font-semibold">
                  {comparison.page_count}
                  <span className="ml-1 text-sm font-normal text-muted-foreground">
                    asli {comparison.original_page_count} · hasil {comparison.result_page_count}
                  </span>
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">
                  Rata-rata selisih piksel
                </p>
                <p className="mt-1 text-2xl font-semibold">{percent(comparison.average_ratio)}</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">
                  Halaman identik
                </p>
                <p className="mt-1 text-2xl font-semibold">
                  {comparison.identical_pages}
                  <span className="ml-1 text-sm font-normal text-muted-foreground">
                    dari {comparison.page_count}
                  </span>
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">DPI</p>
                <p className="mt-1 text-2xl font-semibold">{comparison.dpi}</p>
              </CardContent>
            </Card>
          </div>

          {(comparison.truncated ||
            comparison.only_original.length > 0 ||
            comparison.only_result.length > 0) && (
            <div className="mt-4 rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 text-sm">
              {comparison.truncated && (
                <p>
                  Hanya {comparison.max_pages} halaman pertama yang dibandingkan untuk
                  membatasi waktu pemrosesan.
                </p>
              )}
              {comparison.only_original.length > 0 && (
                <p>
                  Halaman hanya di berkas asli: {comparison.only_original.join(", ")}.
                </p>
              )}
              {comparison.only_result.length > 0 && (
                <p>Halaman hanya di hasil konversi: {comparison.only_result.join(", ")}.</p>
              )}
            </div>
          )}

          <Card className="mt-6">
            <CardContent className="space-y-4 p-4 sm:p-5">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={pageIndex <= 0}
                    onClick={() => setPageIndex((value) => Math.max(0, value - 1))}
                  >
                    <ArrowLeft className="h-4 w-4" /> Sebelumnya
                  </Button>
                  <span className="text-sm text-muted-foreground">
                    Halaman {current.page} dari {pages.length}
                  </span>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={pageIndex >= pages.length - 1}
                    onClick={() =>
                      setPageIndex((value) => Math.min(pages.length - 1, value + 1))
                    }
                  >
                    Berikutnya <ArrowRight className="h-4 w-4" />
                  </Button>
                </div>
                <div className="flex items-center gap-2">
                  <Badge variant={badgeVariant(current.status)}>{current.status}</Badge>
                  <span className="text-xs text-muted-foreground">
                    {percent(current.ratio)} piksel berbeda
                    {current.text_similarity !== null &&
                      ` · teks ${(current.text_similarity * 100).toFixed(1)}%`}
                  </span>
                  <div className="flex rounded-lg border p-0.5">
                    <Button
                      variant={mode === "side" ? "secondary" : "ghost"}
                      size="sm"
                      onClick={() => setMode("side")}
                    >
                      <Columns2 className="h-4 w-4" /> Berdampingan
                    </Button>
                    <Button
                      variant={mode === "diff" ? "secondary" : "ghost"}
                      size="sm"
                      onClick={() => setMode("diff")}
                    >
                      <Highlighter className="h-4 w-4" /> Sorotan selisih
                    </Button>
                  </div>
                </div>
              </div>

              <Panes page={current} mode={mode} />

              <div className="flex flex-wrap gap-1 border-t pt-3">
                {pages.map((page, index) => (
                  <button
                    key={page.page}
                    type="button"
                    onClick={() => setPageIndex(index)}
                    aria-label={`Halaman ${page.page}: ${page.status}`}
                    aria-current={index === pageIndex ? "page" : undefined}
                    className={cn(
                      "relative h-8 min-w-8 rounded-md border px-2 text-xs tabular-nums transition",
                      index === pageIndex
                        ? "border-primary font-semibold text-primary"
                        : "text-muted-foreground hover:bg-muted",
                    )}
                  >
                    {page.page}
                    <span
                      className={cn(
                        "absolute right-1 top-1 h-1.5 w-1.5 rounded-full",
                        toneFor(page),
                      )}
                    />
                  </button>
                ))}
              </div>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
