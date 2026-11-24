"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowRight,
  Download,
  FileClock,
  Loader2,
  RefreshCw,
  ScanEye,
  Search,
  Trash2,
  XCircle,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { JobStatusBadge } from "@/components/job-status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { downloadJobFile } from "@/lib/api";
import { formatBytes, formatDate, titleCase } from "@/lib/utils";
import { clearHistory, deleteJob, listJobs } from "@/services/conversions";
import { getFormats } from "@/services/formats";
import type { ConversionJob, JobStatus } from "@/types";

const PAGE_SIZE = 10;

export default function HistoryPage() {
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState<JobStatus | "all">("all");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [downloadingId, setDownloadingId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ConversionJob | null>(null);
  const [clearOpen, setClearOpen] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setQuery(search.trim());
      setPage(1);
    }, 300);
    return () => window.clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    setPage(1);
  }, [status]);

  const historyQuery = useQuery({
    queryKey: ["history", page, status, query],
    queryFn: () => listJobs({ page, pageSize: PAGE_SIZE, status, q: query }),
  });
  const formatsQuery = useQuery({ queryKey: ["formats"], queryFn: getFormats, staleTime: 60_000 });

  const toolLabels = useMemo(() => {
    const map = new Map<string, string>();
    formatsQuery.data?.tools.forEach((tool) => map.set(tool.id, tool.label));
    return map;
  }, [formatsQuery.data]);

  const deleteMutation = useMutation({
    mutationFn: (jobId: string) => deleteJob(jobId),
    onSuccess: () => {
      toast.success("Pekerjaan dan berkasnya telah dihapus.");
      setDeleteTarget(null);
      void queryClient.invalidateQueries({ queryKey: ["history"] });
      void queryClient.invalidateQueries({ queryKey: ["stats"] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (error: Error) => toast.error("Gagal menghapus", { description: error.message }),
  });

  const clearMutation = useMutation({
    mutationFn: () => clearHistory(),
    onSuccess: (result) => {
      toast.success(`${result.deleted} pekerjaan dihapus.`);
      setClearOpen(false);
      setPage(1);
      void queryClient.invalidateQueries({ queryKey: ["history"] });
      void queryClient.invalidateQueries({ queryKey: ["stats"] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (error: Error) => toast.error("Gagal menghapus riwayat", { description: error.message }),
  });

  async function handleDownload(job: ConversionJob) {
    if (!job.download_url) return;
    setDownloadingId(job.id);
    try {
      await downloadJobFile(job.download_url, job.output_filename || "hasil");
    } catch (error) {
      toast.error("Unduhan gagal", { description: (error as Error).message });
    } finally {
      setDownloadingId(null);
    }
  }

  const data = historyQuery.data;
  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  return (
    <div className="container py-10">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Riwayat konversi</h1>
          <p className="mt-1 text-muted-foreground">
            Semua pekerjaan milik akun atau sesi tamu Anda. Hapus riwayat untuk membersihkan berkas tersimpan.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => void historyQuery.refetch()} disabled={historyQuery.isFetching}>
            {historyQuery.isFetching ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            Muat ulang
          </Button>
          <Button variant="outline" className="text-destructive" onClick={() => setClearOpen(true)} disabled={!data?.total}>
            <Trash2 className="h-4 w-4" /> Hapus semua
          </Button>
          <Button asChild><Link href="/convert">Konversi baru</Link></Button>
        </div>
      </div>

      <Card className="mt-6">
        <CardContent className="p-4 sm:p-5">
          <div className="flex flex-col gap-3 sm:flex-row">
            <div className="relative flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Cari nama berkas atau alat…"
                className="pl-9"
                aria-label="Cari riwayat"
              />
            </div>
            <Select
              value={status}
              onChange={(event) => setStatus(event.target.value as JobStatus | "all")}
              className="sm:w-48"
              aria-label="Filter status"
            >
              <option value="all">Semua status</option>
              <option value="completed">Berhasil</option>
              <option value="processing">Diproses</option>
              <option value="queued">Menunggu</option>
              <option value="failed">Gagal</option>
              <option value="cancelled">Dibatalkan</option>
              <option value="expired">Kedaluwarsa</option>
            </Select>
          </div>
        </CardContent>
      </Card>

      {historyQuery.isError && (
        <div className="mt-4 flex items-start gap-3 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm">
          <XCircle className="mt-0.5 h-4 w-4 text-destructive" />
          <div>
            <p className="font-medium">Riwayat tidak dapat dimuat</p>
            <p className="text-muted-foreground">{(historyQuery.error as Error).message}</p>
          </div>
        </div>
      )}

      <div className="mt-4 overflow-hidden rounded-xl border bg-card">
        {historyQuery.isLoading && (
          <div className="space-y-3 p-4">
            {[0, 1, 2, 3].map((index) => <Skeleton key={index} className="h-16" />)}
          </div>
        )}

        {!historyQuery.isLoading && data?.items.length === 0 && (
          <div className="p-10 text-center">
            <FileClock className="mx-auto h-10 w-10 text-muted-foreground" />
            <p className="mt-3 font-medium">Tidak ada pekerjaan yang cocok</p>
            <p className="mt-1 text-sm text-muted-foreground">
              {query || status !== "all" ? "Coba ubah pencarian atau filter status." : "Mulai konversi untuk melihat riwayat di sini."}
            </p>
            <Button className="mt-4" asChild><Link href="/convert">Mulai konversi</Link></Button>
          </div>
        )}

        {data && data.items.length > 0 && (
          <>
            <div className="hidden overflow-x-auto md:block">
              <table className="w-full text-left text-sm">
                <thead className="border-b bg-muted/40 text-xs uppercase tracking-wide text-muted-foreground">
                  <tr>
                    <th className="px-4 py-3 font-medium">Berkas</th>
                    <th className="px-4 py-3 font-medium">Alat</th>
                    <th className="px-4 py-3 font-medium">Status</th>
                    <th className="px-4 py-3 font-medium">Ukuran</th>
                    <th className="px-4 py-3 font-medium">Dibuat</th>
                    <th className="px-4 py-3 text-right font-medium">Aksi</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {data.items.map((job) => (
                    <tr key={job.id} className="align-middle hover:bg-muted/20">
                      <td className="max-w-[16rem] px-4 py-3">
                        <p className="truncate font-medium">{job.original_filename}</p>
                        <p className="truncate text-xs text-muted-foreground">
                          {job.output_filename ? `→ ${job.output_filename}` : job.error_message ?? ""}
                        </p>
                      </td>
                      <td className="px-4 py-3 text-muted-foreground">
                        {toolLabels.get(job.tool) ?? titleCase(job.tool)}
                      </td>
                      <td className="px-4 py-3"><JobStatusBadge status={job.status} /></td>
                      <td className="px-4 py-3 text-muted-foreground">{formatBytes(job.file_size)}</td>
                      <td className="whitespace-nowrap px-4 py-3 text-muted-foreground">{formatDate(job.created_at)}</td>
                      <td className="px-4 py-3">
                        <div className="flex justify-end gap-1">
                          {job.compare_url && (
                            <Button variant="ghost" size="sm" asChild>
                              <Link href={`/compare/${job.id}`} aria-label={`Bandingkan ${job.original_filename}`}>
                                <ScanEye className="h-4 w-4" />
                              </Link>
                            </Button>
                          )}
                          {job.status === "completed" && job.download_url && (
                            <Button variant="ghost" size="sm" onClick={() => void handleDownload(job)} disabled={downloadingId === job.id}>
                              {downloadingId === job.id ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
                              <span className="sr-only">Unduh {job.original_filename}</span>
                            </Button>
                          )}
                          <Button variant="ghost" size="sm" asChild>
                            <Link href={`/convert/${job.tool}`} aria-label={`Konversi lagi dengan ${job.tool}`}>
                              <RefreshCw className="h-4 w-4" />
                            </Link>
                          </Button>
                          <Button variant="ghost" size="sm" onClick={() => setDeleteTarget(job)} aria-label={`Hapus ${job.original_filename}`}>
                            <Trash2 className="h-4 w-4 text-destructive" />
                          </Button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <ul className="divide-y md:hidden">
              {data.items.map((job) => (
                <li key={job.id} className="p-4">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="truncate font-medium">{job.original_filename}</p>
                      <p className="text-xs text-muted-foreground">
                        {toolLabels.get(job.tool) ?? titleCase(job.tool)} · {formatBytes(job.file_size)}
                      </p>
                      <p className="mt-0.5 text-xs text-muted-foreground">{formatDate(job.created_at)}</p>
                    </div>
                    <JobStatusBadge status={job.status} />
                  </div>
                  {job.error_message && <p className="mt-2 text-xs text-destructive">{job.error_message}</p>}
                  <div className="mt-3 flex flex-wrap gap-2">
                    {job.status === "completed" && job.download_url && (
                      <Button size="sm" onClick={() => void handleDownload(job)} disabled={downloadingId === job.id}>
                        {downloadingId === job.id ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
                        Unduh
                      </Button>
                    )}
                    {job.compare_url && (
                      <Button size="sm" variant="outline" asChild>
                        <Link href={`/compare/${job.id}`}>
                          <ScanEye className="h-4 w-4" /> Bandingkan
                        </Link>
                      </Button>
                    )}
                    <Button size="sm" variant="outline" asChild>
                      <Link href={`/convert/${job.tool}`}>Ulangi</Link>
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setDeleteTarget(job)}>
                      <Trash2 className="h-4 w-4" /> Hapus
                    </Button>
                  </div>
                </li>
              ))}
            </ul>

            <div className="flex items-center justify-between border-t px-4 py-3 text-sm">
              <p className="text-muted-foreground">
                Halaman {page} dari {totalPages} · {data.total} pekerjaan
              </p>
              <div className="flex gap-2">
                <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>
                  <ArrowLeft className="h-4 w-4" /> Sebelumnya
                </Button>
                <Button variant="outline" size="sm" disabled={page >= totalPages} onClick={() => setPage((value) => value + 1)}>
                  Berikutnya <ArrowRight className="h-4 w-4" />
                </Button>
              </div>
            </div>
          </>
        )}
      </div>

      <Dialog open={Boolean(deleteTarget)} onOpenChange={(open) => !open && setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Hapus pekerjaan ini?</DialogTitle>
            <DialogDescription>
              Berkas masukan dan hasil konversi <strong>{deleteTarget?.original_filename}</strong> akan dihapus permanen.
              Tindakan ini tidak dapat dibatalkan.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>Batal</Button>
            <Button
              variant="destructive"
              onClick={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
              disabled={deleteMutation.isPending}
            >
              {deleteMutation.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              Hapus permanen
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={clearOpen} onOpenChange={setClearOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Hapus seluruh riwayat?</DialogTitle>
            <DialogDescription>
              Semua pekerjaan milik identitas Anda akan dihapus beserta berkasnya. Akun Anda tetap aktif.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setClearOpen(false)}>Batal</Button>
            <Button variant="destructive" onClick={() => clearMutation.mutate()} disabled={clearMutation.isPending}>
              {clearMutation.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              Hapus semua riwayat
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
