import { apiFetch } from "@/lib/api";
import type {
  ConversionJob,
  ConversionList,
  ConversionStats,
  JobStatus,
  VisualComparison,
} from "@/types";

export interface CreateConversionInput {
  tool: string;
  files: File[];
  options: Record<string, unknown>;
}

export function createConversion({ tool, files, options }: CreateConversionInput): Promise<ConversionJob> {
  const form = new FormData();
  form.append("tool", tool);
  form.append("options", JSON.stringify(options));
  files.forEach((file) => form.append("files", file));
  return apiFetch<ConversionJob>("/api/v1/conversions", { method: "POST", body: form });
}

export function getJob(jobId: string): Promise<ConversionJob> {
  return apiFetch<ConversionJob>(`/api/v1/conversions/${jobId}`);
}

export interface HistoryParams {
  page?: number;
  pageSize?: number;
  status?: JobStatus | "all";
  tool?: string;
  q?: string;
}

export function listJobs(params: HistoryParams = {}): Promise<ConversionList> {
  const search = new URLSearchParams();
  search.set("page", String(params.page ?? 1));
  search.set("page_size", String(params.pageSize ?? 20));
  if (params.status && params.status !== "all") search.set("status", params.status);
  if (params.tool) search.set("tool", params.tool);
  if (params.q) search.set("q", params.q);
  return apiFetch<ConversionList>(`/api/v1/conversions?${search.toString()}`);
}

export function getStats(): Promise<ConversionStats> {
  return apiFetch<ConversionStats>("/api/v1/conversions/stats");
}

export function getComparison(jobId: string): Promise<VisualComparison> {
  return apiFetch<VisualComparison>(`/api/v1/conversions/${jobId}/compare`);
}

export function deleteJob(jobId: string): Promise<void> {
  return apiFetch<void>(`/api/v1/conversions/${jobId}`, { method: "DELETE" });
}

export function clearHistory(): Promise<{ deleted: number }> {
  return apiFetch<{ deleted: number }>("/api/v1/conversions", { method: "DELETE" });
}
