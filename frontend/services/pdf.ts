import { apiFetch } from "@/lib/api";

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
