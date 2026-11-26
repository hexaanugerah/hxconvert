import { API_URL, apiFetch, getToken } from "@/lib/api";

export interface OcrTextRun {
  text: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface OcrPageLayout {
  page: number;
  page_count: number;
  width_pt: number;
  height_pt: number;
  runs: OcrTextRun[];
}

export interface PdfDrawing {
  id: string;
  kind: "line" | "rect" | "path";
  path: string;
  x: number;
  y: number;
  w: number;
  h: number;
  stroke: string;
  fill: string;
  stroke_width: number;
  stroke_opacity: number;
  fill_opacity: number;
  dashes: number[];
  seqno: number;
}

export interface PdfDrawingsPage {
  page: number;
  drawings: PdfDrawing[];
  truncated: boolean;
}

export interface PdfDrawingsLayout {
  pages: PdfDrawingsPage[];
  page_count: number;
}

export function getOcrPageLayout(
  file: File,
  pageNumber: number,
  language: string,
): Promise<OcrPageLayout> {
  const form = new FormData();
  form.append("files", file);
  form.append("page_number", String(pageNumber));
  form.append("language", language);
  return apiFetch<OcrPageLayout>("/api/v1/pdf/ocr-layout", { method: "POST", body: form });
}

export function getPdfDrawings(file: File): Promise<PdfDrawingsLayout> {
  const form = new FormData();
  form.append("files", file);
  return apiFetch<PdfDrawingsLayout>("/api/v1/pdf/drawings", { method: "POST", body: form });
}

export async function getPdfPageWithoutShapes(file: File, pageNumber: number): Promise<Blob> {
  const form = new FormData();
  form.append("files", file);
  form.append("page_number", String(pageNumber));
  const headers = new Headers();
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${API_URL}/api/v1/pdf/background-without-shapes`, {
    method: "POST",
    headers,
    body: form,
    credentials: "include",
  });
  if (!response.ok) {
    let message = `Gagal memuat halaman PDF (HTTP ${response.status}).`;
    try {
      const body = await response.json();
      message = body?.error?.message || message;
    } catch {
      // Keep the generic error when the proxy returns a non-JSON response.
    }
    throw new Error(message);
  }
  return response.blob();
}
