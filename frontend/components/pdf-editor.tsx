"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import type { PDFDocumentProxy } from "pdfjs-dist";
import {
  ArrowLeft,
  CheckCircle2,
  Circle,
  Download,
  FileText,
  ImagePlus,
  Loader2,
  Minus,
  MousePointer2,
  Redo2,
  RotateCcw,
  Save,
  ScanText,
  Sparkles,
  Square,
  Trash2,
  Type,
  Undo2,
  XCircle,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { UploadDropzone } from "@/components/upload-dropzone";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { API_URL, downloadJobFile, getToken } from "@/lib/api";
import { cn, formatBytes } from "@/lib/utils";
import { createConversion, getJob } from "@/services/conversions";
import { getFormats } from "@/services/formats";
import { getOcrPageLayout } from "@/services/pdf";

const MAX_EDITOR_PAGES = 20;
const MAX_ASSETS = 4;
const MIN_SHAPE_SIZE = 4;
const HISTORY_LIMIT = 60;
const MAX_TEXT_RUNS_PER_PAGE = 700;
const DRAFT_KEY = "docuconvert.pdf-editor.draft.v2";
const LEGACY_DRAFT_KEY = "docuconvert.pdf-editor.draft.v1";

type Mode = "select" | "text" | "image" | "rect" | "ellipse" | "line";
type ShapeType = "rect" | "ellipse";

interface PdfTextRun {
  id: string;
  page: number;
  text: string;
  x: number;
  y: number;
  w: number;
  h: number;
  size: number;
  font: TextItem["font"];
  bold: boolean;
  italic: boolean;
  color: string;
  // OCR line boxes have no PDF baseline; approximate it inside the box.
  baseline?: number;
  source?: "pdf" | "ocr";
}

interface EditorPage {
  number: number;
  widthPt: number;
  heightPt: number;
  displayWidth: number;
  displayHeight: number;
  url: string;
  texts: PdfTextRun[];
  textsTruncated?: boolean;
}

interface Asset {
  id: string;
  file: File;
  url: string;
  width: number;
  height: number;
}

interface BaseItem {
  id: string;
  page: number;
  x: number;
  y: number;
}

interface TextItem extends BaseItem {
  type: "text";
  text: string;
  size: number;
  color: string;
  font: "helv" | "times" | "courier";
  bold: boolean;
  italic: boolean;
}

interface ImageItem extends BaseItem {
  type: "image";
  w: number;
  h: number;
  asset: number;
  // Filename of the uploaded image, used to re-link the picture when a saved
  // draft is restored and the user uploads the same image again.
  assetName?: string;
}

interface ShapeItem extends BaseItem {
  type: ShapeType;
  w: number;
  h: number;
  stroke: string;
  fill: string;
  hasFill: boolean;
  strokeWidth: number;
}

interface LineItem extends BaseItem {
  type: "line";
  x2: number;
  y2: number;
  stroke: string;
  strokeWidth: number;
}

interface TextEditItem extends BaseItem {
  type: "text-edit";
  w: number;
  h: number;
  text: string;
  size: number;
  color: string;
  font: TextItem["font"];
  bold: boolean;
  italic: boolean;
  // True until the user changes size/color/font manually: the backend then
  // copies the original span style instead of the values sent by the editor.
  autoStyle: boolean;
  // Baseline of the original text in PDF points. The box is padded a little
  // to cover ascenders/descenders, so the replacement text is aligned here.
  baseline?: number;
}

type EditItem = TextItem | ImageItem | ShapeItem | LineItem | TextEditItem;

interface Draft {
  page: number;
  mode: "rect" | "ellipse" | "line";
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

interface EditorSnapshot {
  items: EditItem[];
}

interface SavedDraft {
  version: 2;
  savedAt: string;
  file: { name: string; size: number };
  items: EditItem[];
}

const FONT_STACK: Record<TextItem["font"], string> = {
  helv: "Helvetica, Arial, sans-serif",
  times: "'Times New Roman', Times, serif",
  courier: "'Courier New', Courier, monospace",
};

const TOOL_BUTTONS: { mode: Mode; label: string; icon: typeof Type }[] = [
  { mode: "select", label: "Pilih", icon: MousePointer2 },
  { mode: "text", label: "Teks", icon: Type },
  { mode: "image", label: "Gambar", icon: ImagePlus },
  { mode: "rect", label: "Kotak", icon: Square },
  { mode: "ellipse", label: "Elips", icon: Circle },
  { mode: "line", label: "Garis", icon: Minus },
];

function terminal(status: string | undefined): boolean {
  return status === "completed" || status === "failed" || status === "cancelled" || status === "expired";
}

function newId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `item-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, value));
}

function round(value: number): number {
  return Math.round(value * 10) / 10;
}

function finiteNumber(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function savedFont(value: unknown): TextItem["font"] {
  return value === "times" || value === "courier" ? value : "helv";
}

function savedColor(value: unknown, fallback: string): string {
  return typeof value === "string" && /^#[0-9a-f]{6}$/i.test(value) ? value : fallback;
}

function formatSavedAt(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString("id-ID", { dateStyle: "medium", timeStyle: "short" });
}

function inferRunFont(
  fontName: string,
  family: string,
): { font: TextItem["font"]; bold: boolean; italic: boolean } {
  const name = fontName.toLowerCase();
  const familyName = family.toLowerCase();
  const bold = /bold|black|heavy|semibold|demi/.test(`${name} ${familyName}`);
  const italic = /italic|oblique/.test(`${name} ${familyName}`);
  const monospace = /courier|mono/.test(`${name} ${familyName}`);
  const serif =
    /times|georgia|garamond|serif/.test(`${name} ${familyName}`) &&
    !familyName.includes("sans-serif");
  return { font: monospace ? "courier" : serif ? "times" : "helv", bold, italic };
}

function splitTextIntoWordBoxes(
  text: string,
  x: number,
  width: number,
  size: number,
  family: string,
  bold: boolean,
  italic: boolean,
  context: CanvasRenderingContext2D,
): { text: string; x: number; w: number }[] {
  let parts = text.match(/\s+|[^\s]+/gu) ?? [];
  if (
    parts.length === 1 &&
    !/\s/u.test(text) &&
    /[\u2e80-\u9fff\uf900-\ufaff\uac00-\ud7af]/u.test(text)
  ) {
    parts = Array.from(text);
  }
  const previousFont = context.font;
  context.font = `${italic ? "italic " : ""}${bold ? "700 " : "400 "}${Math.max(1, size)}px ${family || "sans-serif"}`;
  const measured = parts.map((part) => context.measureText(part).width);
  context.font = previousFont;

  const total = measured.reduce((sum, partWidth) => sum + partWidth, 0);
  const fallbackTotal = parts.reduce((sum, part) => sum + Array.from(part).length, 0) || 1;
  const scale = width / (total > 0 ? total : fallbackTotal);
  let offset = 0;
  const boxes: { text: string; x: number; w: number }[] = [];
  parts.forEach((part, index) => {
    const partWidth = (total > 0 ? measured[index] : Array.from(part).length) * scale;
    if (!/^\s+$/u.test(part)) {
      boxes.push({ text: part, x: x + offset, w: partWidth });
    }
    offset += partWidth;
  });
  return boxes;
}

function runIsCovered(run: PdfTextRun, items: EditItem[]): boolean {
  return items.some(
    (item) =>
      item.type === "text-edit" &&
      item.page === run.page &&
      item.x < run.x + run.w &&
      item.x + item.w > run.x &&
      item.y < run.y + run.h &&
      item.y + item.h > run.y,
  );
}

function detectInkColor(
  pixels: Uint8ClampedArray,
  canvasWidth: number,
  canvasHeight: number,
  run: Pick<PdfTextRun, "x" | "y" | "w" | "h">,
  scale: number,
): string {
  const x0 = clamp(Math.floor(run.x * scale), 0, canvasWidth - 1);
  const y0 = clamp(Math.floor(run.y * scale), 0, canvasHeight - 1);
  const x1 = clamp(Math.ceil((run.x + run.w) * scale), x0 + 1, canvasWidth);
  const y1 = clamp(Math.ceil((run.y + run.h) * scale), y0 + 1, canvasHeight);
  const width = x1 - x0;
  const height = y1 - y0;
  if (width <= 0 || height <= 0) return "#111111";

  // Sample a sparse grid: enough to find the glyph core without reading every
  // pixel of a large page for every text run.
  const step = Math.max(1, Math.ceil(Math.sqrt((width * height) / 1200)));
  const buckets = new Map<number, number>();
  const samples: number[] = [];
  for (let y = y0; y < y1; y += step) {
    for (let x = x0; x < x1; x += step) {
      const index = (y * canvasWidth + x) * 4;
      const red = pixels[index] ?? 0;
      const green = pixels[index + 1] ?? 0;
      const blue = pixels[index + 2] ?? 0;
      const packed = (red << 16) | (green << 8) | blue;
      samples.push(packed);
      const key = ((red >> 3) << 10) | ((green >> 3) << 5) | (blue >> 3);
      buckets.set(key, (buckets.get(key) ?? 0) + 1);
    }
  }
  if (!samples.length) return "#111111";

  // The most common colour in the run box is the background; the pixel that
  // differs from it most is the ink, which also works for light text on dark
  // backgrounds.
  let backgroundKey = 0;
  let backgroundCount = -1;
  buckets.forEach((count, key) => {
    if (count > backgroundCount) {
      backgroundCount = count;
      backgroundKey = key;
    }
  });
  const backgroundRed = (((backgroundKey >> 10) & 31) << 3) + 4;
  const backgroundGreen = (((backgroundKey >> 5) & 31) << 3) + 4;
  const backgroundBlue = ((backgroundKey & 31) << 3) + 4;

  let bestColor = 0;
  let bestDistance = -1;
  for (const packed of samples) {
    const red = (packed >> 16) & 255;
    const green = (packed >> 8) & 255;
    const blue = packed & 255;
    const distance =
      (red - backgroundRed) ** 2 +
      (green - backgroundGreen) ** 2 +
      (blue - backgroundBlue) ** 2;
    if (distance > bestDistance) {
      bestDistance = distance;
      bestColor = packed;
    }
  }
  if (bestDistance < 900) return "#111111";
  return `#${bestColor.toString(16).padStart(6, "0")}`;
}

async function detectRunColorsFromPageImage(
  url: string,
  pageWidthPt: number,
  runs: PdfTextRun[],
): Promise<void> {
  const image = new Image();
  await new Promise<void>((resolve, reject) => {
    image.onload = () => resolve();
    image.onerror = () => reject(new Error("Gambar halaman tidak dapat dibaca."));
    image.src = url;
  });
  const canvas = document.createElement("canvas");
  canvas.width = image.naturalWidth;
  canvas.height = image.naturalHeight;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) return;
  context.drawImage(image, 0, 0);
  const imageData = context.getImageData(0, 0, canvas.width, canvas.height);
  const scale = canvas.width / pageWidthPt;
  for (const run of runs) {
    run.color = detectInkColor(imageData.data, canvas.width, canvas.height, run, scale);
  }
}

function restoreItem(raw: unknown): EditItem | null {
  if (!raw || typeof raw !== "object") return null;
  const value = raw as Record<string, unknown>;
  const type = String(value.type);
  const page = Math.trunc(finiteNumber(value.page, 0));
  if (page < 1) return null;
  const base = {
    id: typeof value.id === "string" && value.id ? value.id : newId(),
    page,
    x: finiteNumber(value.x, 0),
    y: finiteNumber(value.y, 0),
  };
  if (type === "text") {
    return {
      ...base,
      type: "text",
      text: typeof value.text === "string" ? value.text : "",
      size: finiteNumber(value.size, 14),
      color: savedColor(value.color, "#111111"),
      font: savedFont(value.font),
      bold: Boolean(value.bold),
      italic: Boolean(value.italic),
    };
  }
  if (type === "image") {
    return {
      ...base,
      type: "image",
      w: finiteNumber(value.w, 120),
      h: finiteNumber(value.h, 80),
      asset: Math.max(0, Math.trunc(finiteNumber(value.asset, 0))),
      assetName: typeof value.assetName === "string" ? value.assetName : undefined,
    };
  }
  if (type === "text-edit") {
    return {
      ...base,
      type: "text-edit",
      w: finiteNumber(value.w, 120),
      h: finiteNumber(value.h, 20),
      text: typeof value.text === "string" ? value.text : "",
      size: finiteNumber(value.size, 14),
      color: savedColor(value.color, "#111111"),
      font: savedFont(value.font),
      bold: Boolean(value.bold),
      italic: Boolean(value.italic),
      autoStyle: value.autoStyle !== false,
      baseline: finiteNumber(
        value.baseline,
        finiteNumber(value.y, 0) + finiteNumber(value.h, 20),
      ),
    };
  }
  if (type === "line") {
    return {
      ...base,
      type: "line",
      x2: finiteNumber(value.x2, base.x),
      y2: finiteNumber(value.y2, base.y),
      stroke: savedColor(value.stroke, "#111111"),
      strokeWidth: finiteNumber(value.strokeWidth, 2),
    };
  }
  if (type === "rect" || type === "ellipse") {
    return {
      ...base,
      type,
      w: finiteNumber(value.w, 120),
      h: finiteNumber(value.h, 60),
      stroke: savedColor(value.stroke, "#e11d48"),
      fill: savedColor(value.fill, "#ffffff"),
      hasFill: Boolean(value.hasFill),
      strokeWidth: finiteNumber(value.strokeWidth, 2),
    };
  }
  return null;
}

function parseSavedDraft(raw: string): SavedDraft | null {
  try {
    const value = JSON.parse(raw) as Record<string, unknown>;
    // v1 drafts included free-text search replacements in a separate panel;
    // they are ignored now that text is edited directly on the page.
    if (!value || (value.version !== 1 && value.version !== 2)) return null;
    const file = value.file as Record<string, unknown> | undefined;
    if (!file || typeof file.name !== "string" || !Number.isFinite(Number(file.size))) {
      return null;
    }
    const items = Array.isArray(value.items)
      ? value.items.map(restoreItem).filter((item): item is EditItem => item !== null)
      : [];
    return {
      version: 2,
      savedAt:
        typeof value.savedAt === "string" ? value.savedAt : new Date().toISOString(),
      file: { name: file.name, size: Number(file.size) },
      items,
    };
  } catch {
    return null;
  }
}

export function PdfEditor() {
  const formatsQuery = useQuery({ queryKey: ["formats"], queryFn: getFormats, staleTime: 60_000 });
  const maxUploadMb = formatsQuery.data?.capabilities.limits.max_upload_mb ?? 200;
  const ocrAvailable = formatsQuery.data?.capabilities.ocr.available ?? false;

  const [pdfFile, setPdfFile] = useState<File | null>(null);
  const [pages, setPages] = useState<EditorPage[]>([]);
  const [pdfPageCount, setPdfPageCount] = useState<number | null>(null);
  const [loadingPdf, setLoadingPdf] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [activeAsset, setActiveAsset] = useState(0);
  const [items, setItems] = useState<EditItem[]>([]);
  const [editingRun, setEditingRun] = useState<PdfTextRun | null>(null);
  const [editingItem, setEditingItem] = useState<TextEditItem | null>(null);
  const [editValue, setEditValue] = useState("");
  const [ocrLanguage, setOcrLanguage] = useState("eng+ind");
  const [ocrPending, setOcrPending] = useState<number | null>(null);
  const ocrLanguageRef = useRef(ocrLanguage);
  const ocrQueueRef = useRef<{ file: File; page: EditorPage }[]>([]);
  const ocrRequestedRef = useRef<Set<string>>(new Set());
  const ocrBusyRef = useRef(false);
  const [mode, setMode] = useState<Mode>("select");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [activePage, setActivePage] = useState(1);
  const [draft, setDraft] = useState<Draft | null>(null);
  const draftRef = useRef<Draft | null>(null);
  const dragRef = useRef<{
    id: string;
    startX: number;
    startY: number;
    origin: EditItem;
    page: EditorPage;
    rect: DOMRect;
  } | null>(null);

  const [formError, setFormError] = useState<string | null>(null);

  const [jobId, setJobId] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [wordJobId, setWordJobId] = useState<string | null>(null);

  const [savedDraft, setSavedDraft] = useState<SavedDraft | null>(null);
  const restoredRef = useRef(false);
  const historyRef = useRef<{ past: EditorSnapshot[]; future: EditorSnapshot[] }>({
    past: [],
    future: [],
  });
  const lastRecordRef = useRef<{ key: string; time: number } | null>(null);
  const autoSaveTimerRef = useRef<number | null>(null);
  const pdfOpenedAtRef = useRef(0);
  const pdfFileRef = useRef<File | null>(null);

  useEffect(() => {
    ocrLanguageRef.current = ocrLanguage;
  }, [ocrLanguage]);
  const cancelEditRef = useRef(false);

  const selected = useMemo(
    () => items.find((item) => item.id === selectedId) ?? null,
    [items, selectedId],
  );

  // Drafts live in this browser only: the PDF itself is not stored, so the same
  // file is re-selected later and its element layout is restored by name+size.
  useEffect(() => {
    try {
      const raw =
        window.localStorage.getItem(DRAFT_KEY) ??
        window.localStorage.getItem(LEGACY_DRAFT_KEY);
      const parsed = raw ? parseSavedDraft(raw) : null;
      if (parsed) setSavedDraft(parsed);
    } catch {
      // Private mode or disabled storage: the editor still works without drafts.
    }
  }, []);

  // Render the PDF locally so the editor needs no server round trip before
  // the user starts placing elements.
  useEffect(() => {
    pdfFileRef.current = pdfFile;
    if (!pdfFile) {
      setPages([]);
      setPdfPageCount(null);
      return;
    }
    let cancelled = false;
    let documentProxy: PDFDocumentProxy | null = null;
    let loadingTask: { destroy: () => Promise<void> } | null = null;
    const createdUrls: string[] = [];

    setPages([]);
    setPdfPageCount(null);
    setLoadingPdf(true);
    setLoadError(null);
    setItems([]);
    setEditingRun(null);
    setEditingItem(null);
    setOcrPending(null);
    ocrQueueRef.current = [];
    ocrRequestedRef.current.clear();
    setSelectedId(null);
    setActivePage(1);
    setJobId(null);
    setWordJobId(null);
    restoredRef.current = false;
    pdfOpenedAtRef.current = Date.now();
    historyRef.current = { past: [], future: [] };
    lastRecordRef.current = null;

    (async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        pdfjs.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs";
        const data = new Uint8Array(await pdfFile.arrayBuffer());
        const task = pdfjs.getDocument({ data, isEvalSupported: false });
        loadingTask = task as unknown as { destroy: () => Promise<void> };
        documentProxy = await task.promise;
        if (cancelled) return;
        setPdfPageCount(documentProxy.numPages);

        const total = Math.min(documentProxy.numPages, MAX_EDITOR_PAGES);
        const rendered: EditorPage[] = [];
        for (let number = 1; number <= total; number += 1) {
          const page = await documentProxy.getPage(number);
          const base = page.getViewport({ scale: 1 });
          const scale = Math.min(1.5, 740 / base.width);
          const viewport = page.getViewport({ scale });
          const ratio = Math.min(2, window.devicePixelRatio || 1);
          const canvas = document.createElement("canvas");
          canvas.width = Math.max(1, Math.floor(viewport.width * ratio));
          canvas.height = Math.max(1, Math.floor(viewport.height * ratio));
          const context = canvas.getContext("2d");
          if (!context) throw new Error("Canvas 2D tidak tersedia di peramban ini.");
          await page.render({
            canvasContext: context,
            viewport,
            transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : undefined,
          }).promise;

          // Collect the real text layer so every visible text run can be
          // clicked and edited in place. Rotated pages are skipped because the
          // run boxes would need the rotated viewport transform.
          const texts: PdfTextRun[] = [];
          let textsTruncated = false;
          if (!base.rotation) {
            try {
              const content = await page.getTextContent();
              const styles = content.styles as Record<string, { fontFamily?: string }>;
              textItems: for (const raw of content.items) {
                if (!("str" in raw)) continue;
                if (!raw.str.trim()) continue;
                const width = raw.width;
                const fontSize = Math.max(
                  4,
                  Math.hypot(raw.transform[2], raw.transform[3]) || raw.height || 10,
                );
                const x = raw.transform[4];
                const top = base.height - raw.transform[5] - fontSize;
                if (!Number.isFinite(x) || !Number.isFinite(top) || width <= 0) continue;
                if (x < -2 || top < -2 || x > base.width || top > base.height) continue;
                const family = styles?.[raw.fontName]?.fontFamily ?? "";
                const { font, bold, italic } = inferRunFont(raw.fontName, family);
                const words = splitTextIntoWordBoxes(
                  raw.str,
                  x,
                  width,
                  fontSize,
                  family,
                  bold,
                  italic,
                  context,
                );
                for (const word of words) {
                  if (texts.length >= MAX_TEXT_RUNS_PER_PAGE) {
                    textsTruncated = true;
                    break textItems;
                  }
                  texts.push({
                    id: newId(),
                    page: number,
                    text: word.text,
                    x: word.x,
                    y: top,
                    w: Math.max(0.5, word.w),
                    h: fontSize,
                    size: fontSize,
                    font,
                    bold,
                    italic,
                    color: "#111111",
                    source: "pdf",
                  });
                }
              }
            } catch {
              // A PDF without a usable text layer is still editable with new
              // text boxes; direct text editing is unavailable until OCR runs.
            }
          }
          if (texts.length) {
            try {
              const imageData = context.getImageData(0, 0, canvas.width, canvas.height);
              const pixelsPerPoint = canvas.width / base.width;
              for (const run of texts) {
                run.color = detectInkColor(
                  imageData.data,
                  canvas.width,
                  canvas.height,
                  run,
                  pixelsPerPoint,
                );
              }
            } catch {
              // Colour detection is a preview nicety; keep the default ink.
            }
          }
          page.cleanup();
          const blob = await new Promise<Blob | null>((resolve) =>
            canvas.toBlob(resolve, "image/png"),
          );
          canvas.width = 0;
          canvas.height = 0;
          if (!blob) throw new Error("Halaman PDF gagal dirender.");
          const url = URL.createObjectURL(blob);
          createdUrls.push(url);
          rendered.push({
            number,
            widthPt: base.width,
            heightPt: base.height,
            displayWidth: viewport.width,
            displayHeight: viewport.height,
            url,
            texts,
            textsTruncated,
          });
          if (cancelled) return;
          setPages([...rendered]);
        }
      } catch (error) {
        if (cancelled) return;
        setLoadError(
          error instanceof Error ? error.message : "Dokumen PDF tidak dapat dibaca.",
        );
      } finally {
        if (!cancelled) setLoadingPdf(false);
      }
    })();

    return () => {
      cancelled = true;
      createdUrls.forEach((url) => URL.revokeObjectURL(url));
      void documentProxy?.destroy?.();
      void loadingTask?.destroy?.();
    };
  }, [pdfFile]);

  useEffect(() => {
    if (!pdfFile || !savedDraft || restoredRef.current) return;
    const sameFile =
      savedDraft.file.name === pdfFile.name &&
      Math.abs(savedDraft.file.size - pdfFile.size) < 1;
    if (!sameFile) return;
    // Only a draft that existed before this file was opened is a resume
    // candidate. Otherwise the automatic save made moments after an edit would
    // re-apply the layout and undo the newest change.
    const savedAt = Date.parse(savedDraft.savedAt);
    if (Number.isFinite(savedAt) && savedAt > pdfOpenedAtRef.current) return;
    restoredRef.current = true;
    const restoredItems = savedDraft.items.map((item) => ({ ...item, id: newId() }));
    setItems(restoredItems);
    if (restoredItems.length) {
      toast.success("Susunan elemen dipulihkan", {
        description: `${restoredItems.length} elemen dari draft tersimpan.`,
      });
    }
  }, [pdfFile, savedDraft]);

  // Scanned pages have no text layer to click. OCR them automatically when a
  // page becomes visible, one page at a time, so opening the editor does not
  // block on OCRing every page at once.
  useEffect(() => {
    if (!pdfFile || !ocrAvailable || loadingPdf || !pages.length) return;
    const elements = Array.from(
      document.querySelectorAll<HTMLElement>("[data-pdf-page]"),
    );
    if (!elements.length) return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const number = Number((entry.target as HTMLElement).dataset.pdfPage);
          const page = pages.find((item) => item.number === number);
          if (page && page.texts.length === 0) requestPageOcr(page);
        }
      },
      { rootMargin: "300px 0px" },
    );
    elements.forEach((element) => observer.observe(element));
    return () => observer.disconnect();
  }, [pages, pdfFile, ocrAvailable, loadingPdf]);

  const editQuery = useQuery({
    queryKey: ["conversion", jobId],
    queryFn: () => getJob(jobId as string),
    enabled: Boolean(jobId),
    refetchInterval: (query) => (terminal(query.state.data?.status) ? false : 600),
  });
  const job = editQuery.data;

  const wordQuery = useQuery({
    queryKey: ["conversion", wordJobId],
    queryFn: () => getJob(wordJobId as string),
    enabled: Boolean(wordJobId),
    refetchInterval: (query) => (terminal(query.state.data?.status) ? false : 700),
  });
  const wordJob = wordQuery.data;

  useEffect(() => {
    if (job?.status === "completed") {
      toast.success("Edit PDF selesai", { description: job.output_filename ?? undefined });
    }
    if (job?.status === "failed") {
      toast.error("Edit PDF gagal", { description: job.error_message ?? undefined });
    }
  }, [job?.id, job?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (wordJob?.status === "completed") {
      toast.success("Versi Word siap diunduh");
    }
    if (wordJob?.status === "failed") {
      toast.error("Konversi ke Word gagal", { description: wordJob.error_message ?? undefined });
    }
  }, [wordJob?.id, wordJob?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  // Delete the selected element with the keyboard, but never while typing in
  // the property fields.
  useEffect(() => {
    if (!selectedId) return;
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.tagName === "SELECT" ||
          target.isContentEditable)
      ) {
        return;
      }
      if (event.key === "Delete" || event.key === "Backspace") {
        event.preventDefault();
        recordHistory();
        setItems((current) => current.filter((item) => item.id !== selectedId));
        setSelectedId(null);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [selectedId, items]); // eslint-disable-line react-hooks/exhaustive-deps

  const busy =
    editQuery.data?.status === "queued" ||
    editQuery.data?.status === "processing" ||
    wordQuery.data?.status === "queued" ||
    wordQuery.data?.status === "processing";

  const createMutation = useMutation({
    mutationFn: () => {
      if (!pdfFile) throw new Error("Pilih berkas PDF terlebih dahulu.");
      if (!items.length) {
        throw new Error("Tambahkan atau ubah minimal satu elemen terlebih dahulu.");
      }
      return createConversion({
        tool: "edit-pdf",
        files: [pdfFile, ...assets.map((asset) => asset.file)],
        options: { edits: JSON.stringify({ items: serializeItems(items) }) },
      });
    },
    onSuccess: (created) => {
      setFormError(null);
      setJobId(created.id);
      setWordJobId(null);
    },
    onError: (error: Error) => {
      setFormError(error.message);
      toast.error("Tidak dapat menerapkan edit", { description: error.message });
    },
  });

  const wordMutation = useMutation({
    mutationFn: async () => {
      if (!job?.download_url) throw new Error("PDF hasil edit belum tersedia.");
      const headers = new Headers();
      const token = getToken();
      if (token) headers.set("Authorization", `Bearer ${token}`);
      const response = await fetch(`${API_URL}${job.download_url}`, {
        headers,
        credentials: "include",
      });
      if (!response.ok) throw new Error("Gagal mengambil PDF hasil edit dari server.");
      const blob = await response.blob();
      const file = new File([blob], "edited.pdf", { type: "application/pdf" });
      return createConversion({ tool: "pdf-to-word", files: [file], options: {} });
    },
    onSuccess: (created) => {
      setWordJobId(created.id);
    },
    onError: (error: Error) => {
      toast.error("Tidak dapat membuat versi Word", { description: error.message });
    },
  });

  function pointFromEvent(
    clientX: number,
    clientY: number,
    page: EditorPage,
    rect: DOMRect,
  ): { x: number; y: number } {
    return {
      x: clamp(((clientX - rect.left) / rect.width) * page.widthPt, 0, page.widthPt),
      y: clamp(((clientY - rect.top) / rect.height) * page.heightPt, 0, page.heightPt),
    };
  }

  function beginRunEdit(run: PdfTextRun) {
    setSelectedId(null);
    setEditingItem(null);
    setMode("select");
    setActivePage(run.page);
    cancelEditRef.current = false;
    setEditingRun(run);
    setEditValue(run.text);
  }

  function beginItemEdit(item: TextEditItem) {
    if (mode !== "select") return;
    setSelectedId(item.id);
    setEditingRun(null);
    setMode("select");
    setActivePage(item.page);
    cancelEditRef.current = false;
    setEditingItem(item);
    setEditValue(item.text);
  }

  function commitItemEdit() {
    if (cancelEditRef.current) {
      cancelEditRef.current = false;
      setEditingItem(null);
      return;
    }
    const item = editingItem;
    if (!item) return;
    setEditingItem(null);
    const value = editValue.trim();
    if (!value || value === item.text.trim()) return;
    recordHistory();
    setItems((current) =>
      current.map((entry) =>
        entry.id === item.id && entry.type === "text-edit"
          ? { ...entry, text: value }
          : entry,
      ),
    );
  }

  function escapeItemEdit(item: TextEditItem) {
    cancelEditRef.current = true;
    setEditValue(item.text);
    setEditingItem(null);
  }

  function ocrRequestKey(file: File, pageNumber: number): string {
    return `${file.name}:${file.size}:${file.lastModified}:${pageNumber}`;
  }

  function requestPageOcr(page: EditorPage) {
    const file = pdfFileRef.current;
    if (!file || !ocrAvailable) return;
    const key = ocrRequestKey(file, page.number);
    if (ocrRequestedRef.current.has(key)) return;
    ocrRequestedRef.current.add(key);
    ocrQueueRef.current.push({ file, page });
    void drainOcrQueue();
  }

  async function drainOcrQueue() {
    if (ocrBusyRef.current) return;
    ocrBusyRef.current = true;
    try {
      while (ocrQueueRef.current.length) {
        const job = ocrQueueRef.current.shift();
        if (!job || pdfFileRef.current !== job.file) continue;
        await runPageOcr(job.page, job.file);
      }
    } finally {
      ocrBusyRef.current = false;
    }
  }

  async function runPageOcr(page: EditorPage, file: File) {
    setOcrPending(page.number);
    try {
      const layout = await getOcrPageLayout(file, page.number, ocrLanguageRef.current);
      if (pdfFileRef.current !== file) return;
      const wordContext = document.createElement("canvas").getContext("2d");
      const runs: PdfTextRun[] = layout.runs
        .filter((run) => run.text.trim() && run.w >= 2 && run.h >= 2)
        .flatMap((run) => {
          const size = Math.max(6, Math.min(72, round(run.h * 0.72)));
          const boxes = wordContext
            ? splitTextIntoWordBoxes(
                run.text,
                run.x,
                run.w,
                size,
                FONT_STACK.helv,
                false,
                false,
                wordContext,
              )
            : [{ text: run.text, x: run.x, w: run.w }];
          return boxes.map((word) => ({
            id: newId(),
            page: page.number,
            text: word.text,
            x: word.x,
            y: run.y,
            w: Math.max(0.5, word.w),
            h: run.h,
            size,
            font: "helv" as const,
            bold: false,
            italic: false,
            color: "#111111",
            baseline: run.y + run.h * 0.82,
            source: "ocr" as const,
          }));
        });
      if (!runs.length) {
        ocrRequestedRef.current.delete(ocrRequestKey(file, page.number));
        toast.message("OCR tidak menemukan teks di halaman ini.");
        return;
      }
      try {
        await detectRunColorsFromPageImage(page.url, page.widthPt, runs);
      } catch {
        // Keep black ink if the preview image cannot be sampled.
      }
      setPages((current) =>
        current.map((entry) =>
          entry.number === page.number
            ? { ...entry, texts: runs, textsTruncated: false }
            : entry,
        ),
      );
      toast.success(`${runs.length} kata teks terdeteksi`, {
        description: "Klik satu kata pada halaman untuk mengeditnya.",
      });
    } catch (error) {
      ocrRequestedRef.current.delete(ocrRequestKey(file, page.number));
      toast.error("OCR halaman gagal", {
        description: error instanceof Error ? error.message : "Terjadi kesalahan saat OCR.",
      });
    } finally {
      setOcrPending(null);
    }
  }

  function commitRunEdit() {
    if (cancelEditRef.current) {
      cancelEditRef.current = false;
      setEditingRun(null);
      return;
    }
    const run = editingRun;
    if (!run) return;
    setEditingRun(null);
    const value = editValue.trim();
    if (!value || value === run.text.trim()) return;
    recordHistory();
    // Pad the redaction box mostly at the bottom so descenders of the old text
    // are removed without clipping the line above; the new text keeps the
    // original baseline.
    const padTop = run.h * 0.08;
    const padBottom = run.h * 0.35;
    const item: TextEditItem = {
      id: newId(),
      type: "text-edit",
      page: run.page,
      x: Math.max(0, run.x - 1),
      y: Math.max(0, run.y - padTop),
      w: Math.max(run.w + 2, 8),
      h: Math.max(run.h + padTop + padBottom, 8),
      text: value,
      size: Math.max(4, run.size),
      color: run.color,
      font: run.font,
      bold: run.bold,
      italic: run.italic,
      autoStyle: true,
      baseline: run.baseline ?? run.y + run.h,
    };
    setItems((current) => [...current, item]);
    setSelectedId(item.id);
  }

  function escapeRunEdit(run: PdfTextRun) {
    cancelEditRef.current = true;
    setEditValue(run.text);
    setEditingRun(null);
  }

  function reconcileAssets(next: EditItem[]): EditItem[] {
    return next.map((item) => {
      if (item.type !== "image" || !item.assetName) return item;
      const index = assets.findIndex((asset) => asset.file.name === item.assetName);
      return index >= 0 ? { ...item, asset: index } : item;
    });
  }

  function recordHistory(key?: string) {
    const now = Date.now();
    const last = lastRecordRef.current;
    if (key && last?.key === key && now - last.time < 800) {
      lastRecordRef.current = { key, time: now };
      return;
    }
    lastRecordRef.current = key ? { key, time: now } : null;
    const history = historyRef.current;
    history.past.push({ items });
    if (history.past.length > HISTORY_LIMIT) history.past.shift();
    history.future = [];
  }

  function undo() {
    const history = historyRef.current;
    const previous = history.past.pop();
    if (!previous) return;
    history.future.push({ items });
    lastRecordRef.current = null;
    setEditingRun(null);
    setEditingItem(null);
    setItems(reconcileAssets(previous.items));
    setSelectedId(null);
  }

  function redo() {
    const history = historyRef.current;
    const next = history.future.pop();
    if (!next) return;
    history.past.push({ items });
    lastRecordRef.current = null;
    setEditingRun(null);
    setEditingItem(null);
    setItems(reconcileAssets(next.items));
    setSelectedId(null);
  }

  function buildDraft(): SavedDraft | null {
    if (!pdfFile) return null;
    return {
      version: 2,
      savedAt: new Date().toISOString(),
      file: { name: pdfFile.name, size: pdfFile.size },
      items: items.map((item) =>
        item.type === "image"
          ? { ...item, assetName: item.assetName ?? assets[item.asset]?.file.name }
          : item,
      ),
    };
  }

  function saveDraft(notify = false) {
    const payload = buildDraft();
    if (!payload) return;
    if (!payload.items.length) {
      if (notify) toast.error("Belum ada elemen untuk disimpan.");
      return;
    }
    try {
      window.localStorage.setItem(DRAFT_KEY, JSON.stringify(payload));
      window.localStorage.removeItem(LEGACY_DRAFT_KEY);
      setSavedDraft(payload);
      if (notify) {
        toast.success("Susunan elemen disimpan", {
          description: "Draft dipulihkan otomatis saat PDF yang sama dibuka lagi.",
        });
      }
    } catch {
      if (notify) {
        toast.error("Draft tidak dapat disimpan", {
          description: "Penyimpanan peramban penuh atau dinonaktifkan.",
        });
      }
    }
  }

  function clearDraft() {
    if (autoSaveTimerRef.current !== null) {
      window.clearTimeout(autoSaveTimerRef.current);
      autoSaveTimerRef.current = null;
    }
    try {
      window.localStorage.removeItem(DRAFT_KEY);
      window.localStorage.removeItem(LEGACY_DRAFT_KEY);
    } catch {
      // Storage can be unavailable; the in-memory draft is still cleared.
    }
    setSavedDraft(null);
    toast.success("Draft susunan dihapus");
  }

  // Persist the layout shortly after every change, so closing the tab does
  // not lose work. Writing is debounced while dragging or typing.
  useEffect(() => {
    if (!pdfFile || !items.length) return;
    const timer = window.setTimeout(() => {
      autoSaveTimerRef.current = null;
      saveDraft(false);
    }, 900);
    autoSaveTimerRef.current = timer;
    return () => {
      window.clearTimeout(timer);
      if (autoSaveTimerRef.current === timer) autoSaveTimerRef.current = null;
    };
  }, [pdfFile, items, assets]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey)) return;
      const target = event.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.tagName === "SELECT" ||
          target.isContentEditable)
      ) {
        return;
      }
      const key = event.key.toLowerCase();
      if (key === "z" && !event.shiftKey) {
        event.preventDefault();
        undo();
      } else if (key === "y" || (key === "z" && event.shiftKey)) {
        event.preventDefault();
        redo();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [items, assets]); // eslint-disable-line react-hooks/exhaustive-deps

  function addText(page: EditorPage, x: number, y: number) {
    const item: TextItem = {
      id: newId(),
      type: "text",
      page: page.number,
      x,
      y: clamp(y - 8, 0, page.heightPt),
      text: "Teks baru",
      size: 14,
      color: "#111111",
      font: "helv",
      bold: false,
      italic: false,
    };
    recordHistory();
    setItems((current) => [...current, item]);
    setSelectedId(item.id);
    setMode("select");
  }

  function addImage(page: EditorPage, x: number, y: number) {
    const asset = assets[activeAsset];
    if (!asset) {
      toast.error("Unggah gambar terlebih dahulu", {
        description: "Tambahkan gambar pada panel Gambar di toolbar.",
      });
      return;
    }
    const width = 120;
    const height = asset.width > 0 ? (width * asset.height) / asset.width : 80;
    const item: ImageItem = {
      id: newId(),
      type: "image",
      page: page.number,
      x,
      y,
      w: width,
      h: clamp(height, 12, page.heightPt),
      asset: activeAsset,
      assetName: asset.file.name,
    };
    recordHistory();
    setItems((current) => [...current, item]);
    setSelectedId(item.id);
    setMode("select");
  }

  function addDraftShape(finished: Draft) {
    const page = pages.find((entry) => entry.number === finished.page);
    if (!page) return;
    if (finished.mode === "line") {
      const dx = finished.x1 - finished.x0;
      const dy = finished.y1 - finished.y0;
      if (Math.hypot(dx, dy) < MIN_SHAPE_SIZE) return;
      const item: LineItem = {
        id: newId(),
        type: "line",
        page: page.number,
        x: finished.x0,
        y: finished.y0,
        x2: finished.x1,
        y2: finished.y1,
        stroke: "#111111",
        strokeWidth: 2,
      };
      recordHistory();
      setItems((current) => [...current, item]);
      setSelectedId(item.id);
      setMode("select");
      return;
    }
    const width = Math.abs(finished.x1 - finished.x0);
    const height = Math.abs(finished.y1 - finished.y0);
    if (width < MIN_SHAPE_SIZE && height < MIN_SHAPE_SIZE) return;
    const item: ShapeItem = {
      id: newId(),
      type: finished.mode,
      page: page.number,
      x: Math.min(finished.x0, finished.x1),
      y: Math.min(finished.y0, finished.y1),
      w: Math.max(width, MIN_SHAPE_SIZE),
      h: Math.max(height, MIN_SHAPE_SIZE),
      stroke: "#e11d48",
      fill: "#ffffff",
      hasFill: false,
      strokeWidth: 2,
    };
    recordHistory();
    setItems((current) => [...current, item]);
    setSelectedId(item.id);
    setMode("select");
  }

  function handleSurfacePointerDown(event: React.PointerEvent<HTMLDivElement>, page: EditorPage) {
    setActivePage(page.number);
    setSelectedId(null);
    if (mode === "select") return;
    event.preventDefault();
    const rect = event.currentTarget.getBoundingClientRect();
    const point = pointFromEvent(event.clientX, event.clientY, page, rect);

    if (mode === "text") {
      addText(page, point.x, point.y);
      return;
    }
    if (mode === "image") {
      addImage(page, point.x, point.y);
      return;
    }

    const next: Draft = {
      page: page.number,
      mode,
      x0: point.x,
      y0: point.y,
      x1: point.x,
      y1: point.y,
    };
    draftRef.current = next;
    setDraft(next);

    const onMove = (moveEvent: PointerEvent) => {
      const current = draftRef.current;
      if (!current) return;
      const moved = pointFromEvent(moveEvent.clientX, moveEvent.clientY, page, rect);
      const updated: Draft = { ...current, x1: moved.x, y1: moved.y };
      draftRef.current = updated;
      setDraft(updated);
    };
    const onUp = () => {
      const finished = draftRef.current;
      draftRef.current = null;
      setDraft(null);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      if (finished) addDraftShape(finished);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  function beginDrag(event: React.PointerEvent<HTMLDivElement>, item: EditItem) {
    event.stopPropagation();
    setSelectedId(item.id);
    setActivePage(item.page);
    // A direct text edit must stay on the text it replaces, otherwise the
    // original glyphs would be revealed beneath the new text.
    if (mode !== "select" || item.type === "text-edit") return;
    const surface = event.currentTarget.closest<HTMLElement>("[data-page-surface]");
    const page = pages.find((entry) => entry.number === item.page);
    if (!surface || !page) return;
    const rect = surface.getBoundingClientRect();
    dragRef.current = {
      id: item.id,
      startX: event.clientX,
      startY: event.clientY,
      origin: { ...item },
      page,
      rect,
    };
    let recorded = false;
    const onMove = (moveEvent: PointerEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      if (!recorded) {
        recordHistory();
        recorded = true;
      }
      const dx = ((moveEvent.clientX - drag.startX) / drag.rect.width) * drag.page.widthPt;
      const dy = ((moveEvent.clientY - drag.startY) / drag.rect.height) * drag.page.heightPt;
      setItems((current) =>
        current.map((entry) => {
          if (entry.id !== drag.id) return entry;
          if (entry.type === "line" && drag.origin.type === "line") {
            const lengthX = drag.origin.x2 - drag.origin.x;
            const lengthY = drag.origin.y2 - drag.origin.y;
            const x = clamp(drag.origin.x + dx, 0, drag.page.widthPt);
            const y = clamp(drag.origin.y + dy, 0, drag.page.heightPt);
            return {
              ...entry,
              x,
              y,
              x2: clamp(x + lengthX, 0, drag.page.widthPt),
              y2: clamp(y + lengthY, 0, drag.page.heightPt),
            };
          }
          return {
            ...entry,
            x: clamp(drag.origin.x + dx, 0, drag.page.widthPt),
            y: clamp(drag.origin.y + dy, 0, drag.page.heightPt),
          };
        }),
      );
    };
    const onUp = () => {
      dragRef.current = null;
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  function beginResize(
    event: React.PointerEvent<HTMLSpanElement>,
    item: ImageItem | ShapeItem,
  ) {
    event.stopPropagation();
    event.preventDefault();
    const surface = event.currentTarget.closest<HTMLElement>("[data-page-surface]");
    const page = pages.find((entry) => entry.number === item.page);
    if (!surface || !page) return;
    const rect = surface.getBoundingClientRect();
    const startX = event.clientX;
    const startY = event.clientY;
    let recorded = false;
    const onMove = (moveEvent: PointerEvent) => {
      if (!recorded) {
        recordHistory();
        recorded = true;
      }
      const dw = ((moveEvent.clientX - startX) / rect.width) * page.widthPt;
      const dh = ((moveEvent.clientY - startY) / rect.height) * page.heightPt;
      setItems((current) =>
        current.map((entry) =>
          entry.id === item.id && (entry.type === "image" || entry.type === "rect" || entry.type === "ellipse")
            ? {
                ...entry,
                w: clamp(item.w + dw, 8, page.widthPt * 2),
                h: clamp(item.h + dh, 8, page.heightPt * 2),
              }
            : entry,
        ),
      );
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  function beginResizeLine(
    event: React.PointerEvent<HTMLSpanElement>,
    item: LineItem,
    endpoint: "start" | "end",
  ) {
    event.stopPropagation();
    event.preventDefault();
    const surface = event.currentTarget.closest<HTMLElement>("[data-page-surface]");
    const page = pages.find((entry) => entry.number === item.page);
    if (!surface || !page) return;
    setSelectedId(item.id);
    const rect = surface.getBoundingClientRect();
    let recorded = false;
    const onMove = (moveEvent: PointerEvent) => {
      const point = pointFromEvent(moveEvent.clientX, moveEvent.clientY, page, rect);
      if (!recorded) {
        recordHistory();
        recorded = true;
      }
      setItems((current) =>
        current.map((entry) => {
          if (entry.id !== item.id || entry.type !== "line") return entry;
          return endpoint === "start"
            ? { ...entry, x: point.x, y: point.y }
            : { ...entry, x2: point.x, y2: point.y };
        }),
      );
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  function updateSelected(patch: Record<string, unknown>) {
    if (!selectedId) return;
    recordHistory(`element-${selectedId}`);
    setItems((current) =>
      current.map((item) => (item.id === selectedId ? ({ ...item, ...patch } as EditItem) : item)),
    );
  }

  function updateTextStyle(patch: Record<string, unknown>, manualStyle = true) {
    if (selected?.type === "text-edit" && manualStyle) {
      updateSelected({ ...patch, autoStyle: false });
      return;
    }
    updateSelected(patch);
  }

  function removeSelected() {
    if (!selectedId) return;
    recordHistory();
    setItems((current) => current.filter((item) => item.id !== selectedId));
    setSelectedId(null);
  }


  async function addAssets(fileList: FileList | null) {
    if (!fileList?.length) return;
    const room = MAX_ASSETS - assets.length;
    if (room <= 0) {
      toast.error(`Maksimal ${MAX_ASSETS} gambar tambahan.`);
      return;
    }
    const accepted = Array.from(fileList)
      .filter((file) => /\.(png|jpe?g|webp)$/i.test(file.name))
      .slice(0, room);
    if (!accepted.length) {
      toast.error("Format gambar harus PNG, JPG, atau WebP.");
      return;
    }
    const loaded = await Promise.all(
      accepted.map(async (file) => {
        const url = URL.createObjectURL(file);
        const size = await new Promise<{ width: number; height: number }>((resolve) => {
          const image = new Image();
          image.onload = () => resolve({ width: image.naturalWidth, height: image.naturalHeight });
          image.onerror = () => resolve({ width: 1, height: 1 });
          image.src = url;
        });
        return { id: newId(), file, url, width: size.width, height: size.height };
      }),
    );
    const offset = assets.length;
    setAssets((current) => [...current, ...loaded]);
    // Restored drafts keep the original image filename; re-link those items as
    // soon as the matching picture is uploaded again.
    setItems((current) =>
      current.map((item) => {
        if (item.type !== "image" || !item.assetName) return item;
        const found = loaded.findIndex((asset) => asset.file.name === item.assetName);
        return found >= 0 ? { ...item, asset: offset + found } : item;
      }),
    );
  }

  function removeAsset(index: number) {
    recordHistory();
    const asset = assets[index];
    if (asset) URL.revokeObjectURL(asset.url);
    setAssets((current) => current.filter((_, position) => position !== index));
    setItems((current) =>
      current
        .filter((item) => item.type !== "image" || item.asset !== index)
        .map((item) =>
          item.type === "image" && item.asset > index ? { ...item, asset: item.asset - 1 } : item,
        ),
    );
    setActiveAsset((current) => clamp(current >= index ? current - 1 : current, 0, MAX_ASSETS - 1));
  }

  function resetEditor() {
    // Flush a pending debounce before the workspace is cleared.
    if (pdfFile && items.length) saveDraft(false);
    setPdfFile(null);
    setAssets((current) => {
      current.forEach((asset) => URL.revokeObjectURL(asset.url));
      return [];
    });
    setItems([]);
    setEditingRun(null);
    setEditingItem(null);
    setOcrPending(null);
    setSelectedId(null);
    setJobId(null);
    setWordJobId(null);
    setFormError(null);
    setMode("select");
    historyRef.current = { past: [], future: [] };
    lastRecordRef.current = null;
    restoredRef.current = false;
  }

  async function handleDownload(url: string | null | undefined, filename: string) {
    if (!url) return;
    setDownloading(true);
    try {
      await downloadJobFile(url, filename);
    } catch (error) {
      toast.error("Unduhan gagal", { description: (error as Error).message });
    } finally {
      setDownloading(false);
    }
  }

  const canUndo = historyRef.current.past.length > 0;
  const canRedo = historyRef.current.future.length > 0;
  const missingAssetCount = items.filter(
    (item) => item.type === "image" && !assets[item.asset],
  ).length;
  const draftMatchesCurrent = Boolean(
    savedDraft &&
      pdfFile &&
      savedDraft.file.name === pdfFile.name &&
      Math.abs(savedDraft.file.size - pdfFile.size) < 1,
  );

  if (!pdfFile) {
    return (
      <div className="container max-w-4xl pt-8 pb-16 sm:pt-10">
        <Link
          href="/tools"
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" /> Semua alat
        </Link>
        <div className="mt-5 flex items-start gap-4">
          <span className="grid h-12 w-12 place-items-center rounded-xl bg-primary/10 text-primary">
            <FileText className="h-6 w-6" aria-hidden />
          </span>
          <div>
            <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">Edit PDF</h1>
            <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
              Klik teks lama langsung di halaman untuk mengubahnya, atau tambah teks, gambar,
              kotak, elips, dan garis tanpa panel terpisah. Setelah selesai, unduh PDF hasil edit
              atau versi Word untuk pengeditan lanjutan di luar aplikasi.
            </p>
          </div>
        </div>

        <Card className="mt-8">
          <CardHeader>
            <CardTitle className="text-base">Pilih berkas PDF</CardTitle>
            <CardDescription>
              Hanya PDF. Teks lama bisa diklik dan diganti bila PDF memiliki lapisan teks. PDF
              hasil pindai tanpa lapisan teks dideteksi otomatis dengan OCR saat halaman
              terlihat, lalu teksnya bisa diklik dan diedit.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <UploadDropzone
              files={[]}
              onFilesChange={(files) => setPdfFile(files[0] ?? null)}
              extensions={[".pdf"]}
              maxFiles={1}
              maxSizeMb={maxUploadMb}
            />
          </CardContent>
        </Card>

        {savedDraft && (
          <Card className="mt-4 border-primary/30 bg-primary/5">
            <CardContent className="flex flex-wrap items-start justify-between gap-3 p-4">
              <div className="flex min-w-0 items-start gap-2.5">
                <Save className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                <div className="min-w-0 text-xs">
                  <p className="truncate text-sm font-medium">
                    Draft tersimpan: {savedDraft.file.name}
                  </p>
                  <p className="text-muted-foreground">
                    {savedDraft.items.length} elemen · {formatSavedAt(savedDraft.savedAt)}
                  </p>
                  <p className="mt-1">
                    Pilih berkas PDF yang sama untuk melanjutkan susunan elemen secara otomatis.
                  </p>
                </div>
              </div>
              <Button type="button" variant="ghost" size="sm" onClick={clearDraft}>
                <Trash2 className="h-3.5 w-3.5" /> Hapus draft
              </Button>
            </CardContent>
          </Card>
        )}

        <div className="mt-4 grid gap-3 text-xs text-muted-foreground sm:grid-cols-3">
          <p className="rounded-lg border bg-card/60 p-3">
            <span className="font-medium text-foreground">Langsung</span> — klik teks lama di
            halaman untuk mengubahnya, lalu terapkan menjadi PDF baru.
          </p>
          <p className="rounded-lg border bg-card/60 p-3">
            <span className="font-medium text-foreground">Eksternal</span> — buat versi Word dari
            hasil edit agar bisa diubah di Microsoft Word atau LibreOffice.
          </p>
          <p className="rounded-lg border bg-card/60 p-3">
            <span className="font-medium text-foreground">Aman</span> — berkas diproses di server
            ini dan dihapus otomatis setelah masa berlaku.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="container max-w-7xl pt-8 pb-24 sm:pt-10">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Link
          href="/tools"
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" /> Semua alat
        </Link>
        <Button type="button" variant="ghost" size="sm" onClick={resetEditor} disabled={busy}>
          <RotateCcw className="h-3.5 w-3.5" /> Ganti PDF
        </Button>
      </div>

      <div className="mt-4 flex flex-wrap items-start gap-4">
        <span className="grid h-12 w-12 place-items-center rounded-xl bg-primary/10 text-primary">
          <FileText className="h-6 w-6" aria-hidden />
        </span>
        <div className="min-w-0 flex-1">
          <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">Edit PDF</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {pdfFile.name} · {formatBytes(pdfFile.size)}
            {pdfPageCount ? ` · ${pdfPageCount} halaman` : ""}
            {pdfPageCount && pdfPageCount > MAX_EDITOR_PAGES
              ? ` (editor menampilkan ${MAX_EDITOR_PAGES} halaman pertama)`
              : ""}
          </p>
        </div>
      </div>

      {loadingPdf && (
        <div className="mt-8 flex items-center gap-2 rounded-lg border bg-card px-4 py-3 text-sm">
          <Loader2 className="h-4 w-4 animate-spin text-primary" />
          Merender halaman PDF…
        </div>
      )}

      {loadError && (
        <div className="mt-8 flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
          <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <div>
            <p className="font-medium">PDF tidak dapat dibuka di editor</p>
            <p className="text-xs">{loadError}</p>
            <Button variant="outline" size="sm" className="mt-2" onClick={resetEditor}>
              Pilih berkas lain
            </Button>
          </div>
        </div>
      )}

      {!loadingPdf && !loadError && pages.length > 0 && (
        <div className="mt-6 grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
          <div className="min-w-0 space-y-4">
            <Card>
              <CardContent className="space-y-3 p-3">
                <div className="flex flex-wrap items-center gap-1.5">
                  {TOOL_BUTTONS.map((tool) => {
                    const Icon = tool.icon;
                    return (
                      <Button
                        key={tool.mode}
                        type="button"
                        size="sm"
                        variant={mode === tool.mode ? "default" : "outline"}
                        onClick={() => setMode(tool.mode)}
                        disabled={busy}
                        title={tool.label}
                      >
                        <Icon className="h-4 w-4" />
                        <span className="hidden sm:inline">{tool.label}</span>
                      </Button>
                    );
                  })}
                  <div className="ml-auto flex items-center gap-1.5">
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={undo}
                      disabled={!canUndo || busy}
                      title="Urungkan (Ctrl+Z)"
                      aria-label="Urungkan"
                    >
                      <Undo2 className="h-4 w-4" />
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={redo}
                      disabled={!canRedo || busy}
                      title="Ulangi (Ctrl+Shift+Z)"
                      aria-label="Ulangi"
                    >
                      <Redo2 className="h-4 w-4" />
                    </Button>
                    <span className="text-xs text-muted-foreground">
                      Halaman aktif {activePage}
                    </span>
                  </div>
                </div>

                <div className="flex items-center gap-2">
                  <label className="inline-flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-sm">
                    <ImagePlus className="h-4 w-4 text-primary" />
                    Tambah gambar
                    <input
                      type="file"
                      accept=".png,.jpg,.jpeg,.webp"
                      multiple
                      className="sr-only"
                      disabled={busy || assets.length >= MAX_ASSETS}
                      onChange={(event) => {
                        void addAssets(event.target.files);
                        event.target.value = "";
                      }}
                    />
                  </label>
                  {assets.length === 0 && (
                    <span className="text-xs text-muted-foreground">
                      Unggah gambar, lalu pilih “Gambar” dan klik halaman.
                    </span>
                  )}
                  {assets.map((asset, index) => (
                    <div
                      key={asset.id}
                      className={cn(
                        "group relative h-10 w-10 overflow-hidden rounded-md border",
                        activeAsset === index && "ring-2 ring-primary",
                      )}
                    >
                      <button
                        type="button"
                        className="h-full w-full"
                        onClick={() => setActiveAsset(index)}
                        title={asset.file.name}
                      >
                        {/* eslint-disable-next-line @next/next/no-img-element */}
                        <img
                          src={asset.url}
                          alt={asset.file.name}
                          className="h-full w-full object-cover"
                        />
                      </button>
                      <button
                        type="button"
                        className="absolute right-0 top-0 grid h-4 w-4 place-items-center rounded-bl bg-background/90 text-destructive"
                        onClick={() => removeAsset(index)}
                        aria-label={`Hapus ${asset.file.name}`}
                      >
                        <XCircle className="h-3 w-3" />
                      </button>
                    </div>
                  ))}
                </div>

                <p className="text-[11px] text-muted-foreground">
                  {mode === "select" &&
                    "Klik teks lama pada halaman untuk mengeditnya langsung. Klik elemen untuk memilih, seret untuk memindahkan, atau tekan Delete."}
                  {mode === "text" && "Klik halaman untuk menempatkan kotak teks baru."}
                  {mode === "image" && "Pilih salah satu gambar di atas, lalu klik halaman."}
                  {(mode === "rect" || mode === "ellipse" || mode === "line") &&
                    "Seret di halaman untuk menggambar bentuk."}
                </p>
                {pages.some((page) => page.texts.length === 0) && ocrAvailable && (
                  <p className="text-[11px] text-muted-foreground">
                    Halaman pindai dideteksi otomatis dengan OCR saat mulai terlihat.
                  </p>
                )}
                {pages.some((page) => page.texts.length === 0) && (
                  <div className="space-y-1.5">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-[11px] text-muted-foreground">
                        Bahasa OCR otomatis
                      </span>
                      <Select
                        value={ocrLanguage}
                        onChange={(event) => setOcrLanguage(event.target.value)}
                        className="h-7 w-auto text-xs"
                        aria-label="Bahasa OCR"
                      >
                        <option value="eng+ind">Indonesia + Inggris</option>
                        <option value="eng">Inggris</option>
                        <option value="ind">Indonesia</option>
                      </Select>
                    </div>
                    {!ocrAvailable && (
                      <p className="text-[11px] text-amber-600">
                        OCR belum tersedia di server, jadi teks pada halaman pindai belum bisa
                        diklik.
                      </p>
                    )}
                  </div>
                )}
                {missingAssetCount > 0 && (
                  <p className="text-[11px] text-amber-600">
                    {missingAssetCount} gambar dari draft belum diunggah ulang. Unggah gambar
                    dengan nama berkas yang sama agar tampil kembali.
                  </p>
                )}
              </CardContent>
            </Card>

            <div className="space-y-5 overflow-x-auto pb-2">
              {pages.map((page) => (
                <div
                  key={page.number}
                  data-pdf-page={page.number}
                  className={cn(
                    "rounded-lg border bg-muted/20 p-2",
                    activePage === page.number && "border-primary/50",
                  )}
                  onClick={() => setActivePage(page.number)}
                >
                  <div className="mb-1 flex min-h-6 items-center justify-between gap-2 px-1">
                    <p className="text-[11px] font-medium text-muted-foreground">
                      Halaman {page.number}
                      {page.texts.length === 0 ? " · teks pindai" : ""}
                      {page.textsTruncated ? " · sebagian teks tidak dapat diklik" : ""}
                    </p>
                    {page.texts.length === 0 && (
                      <span
                        className="flex items-center gap-1 text-[11px] text-muted-foreground"
                        title={
                          ocrAvailable
                            ? "OCR berjalan otomatis saat halaman terlihat"
                            : "OCR belum tersedia di server"
                        }
                      >
                        {ocrAvailable ? (
                          ocrPending === page.number ? (
                            <>
                              <Loader2 className="h-3 w-3 animate-spin" />
                              OCR otomatis...
                            </>
                          ) : (
                            <>
                              <ScanText className="h-3 w-3" />
                              Menunggu OCR
                            </>
                          )
                        ) : (
                          "OCR tidak tersedia"
                        )}
                      </span>
                    )}
                  </div>
                  <div
                    data-page-surface
                    className={cn(
                      "relative mx-auto select-none bg-white",
                      mode === "select" ? "cursor-default" : "cursor-crosshair",
                    )}
                    style={{ width: page.displayWidth, height: page.displayHeight }}
                    onPointerDown={(event) => handleSurfacePointerDown(event, page)}
                  >
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={page.url}
                      alt={`Halaman ${page.number}`}
                      draggable={false}
                      className="pointer-events-none absolute inset-0 h-full w-full"
                    />
                    <div className="pointer-events-none absolute inset-0">
                      {items
                        .filter((item) => item.page === page.number)
                        .map((item) => (
                          <EditorElement
                            key={item.id}
                            item={item}
                            assets={assets}
                            selected={selectedId === item.id}
                            scaleX={page.displayWidth / page.widthPt}
                            scaleY={page.displayHeight / page.heightPt}
                            onPointerDown={beginDrag}
                            onResize={beginResize}
                            onResizeLine={beginResizeLine}
                            onOpenTextEdit={beginItemEdit}
                          />
                        ))}
                      {draft && draft.page === page.number && (
                        <DraftShape draft={draft} page={page} />
                      )}
                    </div>

                    {mode === "select" && (
                      <div className="pointer-events-none absolute inset-0">
                        {page.texts.map((run) => {
                        if (runIsCovered(run, items)) return null;
                        const scaleX = page.displayWidth / page.widthPt;
                        const scaleY = page.displayHeight / page.heightPt;
                        const left = run.x * scaleX;
                        const top = run.y * scaleY;
                        const width = Math.max(3, run.w * scaleX);
                        const height = Math.max(7, run.h * scaleY);
                        if (editingRun?.id === run.id) {
                          return (
                            <div
                              key={run.id}
                              className="pointer-events-auto absolute z-20"
                              style={{
                                left,
                                top,
                                width: Math.max(width, 110),
                                height: Math.max(height, 26),
                              }}
                              onPointerDown={(event) => event.stopPropagation()}
                            >
                              <input
                                autoFocus
                                value={editValue}
                                onChange={(event) => setEditValue(event.target.value)}
                                onFocus={(event) => event.currentTarget.select()}
                                onBlur={commitRunEdit}
                                onKeyDown={(event) => {
                                  if (event.key === "Enter") {
                                    event.preventDefault();
                                    event.currentTarget.blur();
                                  } else if (event.key === "Escape") {
                                    event.preventDefault();
                                    escapeRunEdit(run);
                                  }
                                }}
                                className="h-full w-full rounded-sm border-2 border-primary bg-white px-0.5 text-foreground shadow-lg outline-none"
                                style={{ fontSize: Math.max(11, run.size * scaleY) }}
                                aria-label="Edit teks"
                              />
                            </div>
                          );
                        }
                        return (
                          <button
                            key={run.id}
                            type="button"
                            className="pointer-events-auto absolute rounded-[2px] border border-transparent transition-colors hover:border-primary/60 hover:bg-primary/10 focus-visible:border-primary focus-visible:bg-primary/10"
                            style={{ left, top, width, height }}
                            onClick={() => beginRunEdit(run)}
                            onPointerDown={(event) => event.stopPropagation()}
                            title={`Klik untuk mengedit kata: ${run.text.trim().slice(0, 80)}`}
                            aria-label={`Edit kata: ${run.text.trim().slice(0, 60)}`}
                          />
                        );
                        })}
                      </div>
                    )}

                    {editingItem && editingItem.page === page.number && (
                      <div
                        className="pointer-events-auto absolute z-30"
                        style={{
                          left: editingItem.x * (page.displayWidth / page.widthPt),
                          top: editingItem.y * (page.displayHeight / page.heightPt),
                          width: Math.max(
                            editingItem.w * (page.displayWidth / page.widthPt),
                            110,
                          ),
                          height: Math.max(
                            editingItem.h * (page.displayHeight / page.heightPt),
                            26,
                          ),
                        }}
                        onPointerDown={(event) => event.stopPropagation()}
                      >
                        <input
                          autoFocus
                          value={editValue}
                          onChange={(event) => setEditValue(event.target.value)}
                          onFocus={(event) => event.currentTarget.select()}
                          onBlur={commitItemEdit}
                          onKeyDown={(event) => {
                            if (event.key === "Enter") {
                              event.preventDefault();
                              event.currentTarget.blur();
                            } else if (event.key === "Escape") {
                              event.preventDefault();
                              escapeItemEdit(editingItem);
                            }
                          }}
                          className="h-full w-full rounded-sm border-2 border-primary bg-white px-0.5 text-foreground shadow-lg outline-none"
                          style={{
                            fontSize: Math.max(
                              11,
                              editingItem.size * (page.displayHeight / page.heightPt),
                            ),
                          }}
                          aria-label="Edit teks hasil edit"
                        />
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <aside className="space-y-4 lg:sticky lg:top-24">
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">
                  {selected ? "Properti elemen" : "Properti"}
                </CardTitle>
                <CardDescription>
                  {selected
                    ? "Ubah elemen yang dipilih."
                    : "Pilih elemen di halaman untuk mengubah propertinya."}
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-3">
                {!selected && (
                  <p className="text-xs text-muted-foreground">
                    Elemen baru otomatis terpilih setelah ditambahkan.
                  </p>
                )}

                {(selected?.type === "text" || selected?.type === "text-edit") && (
                  <>
                    <div className="space-y-1.5">
                      <Label htmlFor="edit-text">Teks</Label>
                      <textarea
                        id="edit-text"
                        value={selected.text}
                        onChange={(event) => updateSelected({ text: event.target.value })}
                        rows={3}
                        className="w-full rounded-lg border border-input bg-card px-3 py-2 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      />
                    </div>
                    {selected.type === "text-edit" && selected.autoStyle && (
                      <p className="text-[11px] text-muted-foreground">
                        Warna dan font mengikuti teks asli. Mengubah salah satunya akan memakai
                        pilihan Anda.
                      </p>
                    )}
                    <div className="grid grid-cols-2 gap-2">
                      <div className="space-y-1.5">
                        <Label htmlFor="edit-size">Ukuran</Label>
                        <Input
                          id="edit-size"
                          type="number"
                          min={4}
                          max={200}
                          value={selected.size}
                          onChange={(event) =>
                            updateTextStyle(
                              { size: clamp(Number(event.target.value) || 14, 4, 200) },
                              false,
                            )
                          }
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label htmlFor="edit-color">Warna</Label>
                        <input
                          id="edit-color"
                          type="color"
                          value={selected.color}
                          onChange={(event) => updateTextStyle({ color: event.target.value })}
                          className="h-10 w-full rounded-lg border border-input bg-card p-1"
                        />
                      </div>
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="edit-font">Font</Label>
                      <Select
                        id="edit-font"
                        value={selected.font}
                        onChange={(event) =>
                          updateTextStyle({ font: event.target.value as TextItem["font"] })
                        }
                      >
                        <option value="helv">Helvetica / Arial</option>
                        <option value="times">Times New Roman</option>
                        <option value="courier">Courier New</option>
                      </Select>
                    </div>
                    <div className="flex gap-4 text-sm">
                      <label className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          checked={selected.bold}
                          onChange={(event) => updateTextStyle({ bold: event.target.checked })}
                          className="h-4 w-4 accent-primary"
                        />
                        Tebal
                      </label>
                      <label className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          checked={selected.italic}
                          onChange={(event) => updateTextStyle({ italic: event.target.checked })}
                          className="h-4 w-4 accent-primary"
                        />
                        Miring
                      </label>
                    </div>
                  </>
                )}

                {selected?.type === "image" && (
                  <div className="grid grid-cols-2 gap-2">
                    <div className="space-y-1.5">
                      <Label htmlFor="edit-width">Lebar (pt)</Label>
                      <Input
                        id="edit-width"
                        type="number"
                        min={8}
                        value={Math.round(selected.w)}
                        onChange={(event) =>
                          updateSelected({ w: clamp(Number(event.target.value) || 8, 8, 2000) })
                        }
                      />
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="edit-height">Tinggi (pt)</Label>
                      <Input
                        id="edit-height"
                        type="number"
                        min={8}
                        value={Math.round(selected.h)}
                        onChange={(event) =>
                          updateSelected({ h: clamp(Number(event.target.value) || 8, 8, 2000) })
                        }
                      />
                    </div>
                  </div>
                )}

                {(selected?.type === "rect" || selected?.type === "ellipse") && (
                  <>
                    <div className="grid grid-cols-2 gap-2">
                      <div className="space-y-1.5">
                        <Label htmlFor="shape-stroke">Garis</Label>
                        <input
                          id="shape-stroke"
                          type="color"
                          value={selected.stroke}
                          onChange={(event) => updateSelected({ stroke: event.target.value })}
                          className="h-10 w-full rounded-lg border border-input bg-card p-1"
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label htmlFor="shape-width">Ketebalan</Label>
                        <Input
                          id="shape-width"
                          type="number"
                          min={0.5}
                          max={30}
                          step={0.5}
                          value={selected.strokeWidth}
                          onChange={(event) =>
                            updateSelected({
                              strokeWidth: clamp(Number(event.target.value) || 1, 0.5, 30),
                            })
                          }
                        />
                      </div>
                    </div>
                    <label className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={selected.hasFill}
                        onChange={(event) => updateSelected({ hasFill: event.target.checked })}
                        className="h-4 w-4 accent-primary"
                      />
                      Isi warna
                    </label>
                    {selected.hasFill && (
                      <input
                        type="color"
                        value={selected.fill}
                        onChange={(event) => updateSelected({ fill: event.target.value })}
                        className="h-10 w-full rounded-lg border border-input bg-card p-1"
                        aria-label="Warna isi"
                      />
                    )}
                  </>
                )}

                {selected?.type === "line" && (
                  <div className="grid grid-cols-2 gap-2">
                    <div className="space-y-1.5">
                      <Label htmlFor="line-stroke">Warna</Label>
                      <input
                        id="line-stroke"
                        type="color"
                        value={selected.stroke}
                        onChange={(event) => updateSelected({ stroke: event.target.value })}
                        className="h-10 w-full rounded-lg border border-input bg-card p-1"
                      />
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="line-width">Ketebalan</Label>
                      <Input
                        id="line-width"
                        type="number"
                        min={0.5}
                        max={30}
                        step={0.5}
                        value={selected.strokeWidth}
                        onChange={(event) =>
                          updateSelected({
                            strokeWidth: clamp(Number(event.target.value) || 1, 0.5, 30),
                          })
                        }
                      />
                    </div>
                  </div>
                )}

                {selected && (
                  <Button variant="outline" className="w-full" onClick={removeSelected} disabled={busy}>
                    <Trash2 className="h-4 w-4" /> Hapus elemen
                  </Button>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">Terapkan &amp; ekspor</CardTitle>
                <CardDescription>{items.length} elemen susunan</CardDescription>
              </CardHeader>
              <CardContent className="space-y-3">
                {formError && (
                  <div
                    role="alert"
                    className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-2.5 text-xs text-destructive"
                  >
                    <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    <span>{formError}</span>
                  </div>
                )}

                <Button
                  className="w-full"
                  onClick={() => createMutation.mutate()}
                  disabled={busy || createMutation.isPending || !items.length}
                >
                  {createMutation.isPending || job?.status === "queued" || job?.status === "processing" ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Sparkles className="h-4 w-4" />
                  )}
                  {busy ? "Memproses…" : "Terapkan edit"}
                </Button>

                {job?.status === "processing" && (
                  <p className="text-xs text-muted-foreground">Menerapkan elemen ke PDF…</p>
                )}
                {job?.status === "failed" && (
                  <p className="text-xs text-destructive">{job.error_message ?? "Edit gagal."}</p>
                )}

                {job?.status === "completed" && (
                  <div className="space-y-2">
                    <div className="flex items-start gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-2.5">
                      <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
                      <div className="min-w-0 text-xs">
                        <p className="truncate font-medium">{job.output_filename}</p>
                        <p className="text-muted-foreground">
                          {formatBytes(job.output_size)} · PDF
                        </p>
                      </div>
                    </div>
                    <Button
                      className="w-full"
                      onClick={() => void handleDownload(job.download_url, job.output_filename || "edited.pdf")}
                      disabled={downloading}
                    >
                      {downloading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
                      Unduh PDF hasil edit
                    </Button>
                    <Button
                      variant="outline"
                      className="w-full"
                      onClick={() => wordMutation.mutate()}
                      disabled={wordMutation.isPending || wordQuery.data?.status === "queued" || wordQuery.data?.status === "processing"}
                    >
                      {wordMutation.isPending ||
                      wordQuery.data?.status === "queued" ||
                      wordQuery.data?.status === "processing" ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <FileText className="h-4 w-4" />
                      )}
                      Buat versi Word (edit eksternal)
                    </Button>
                    {wordJob?.status === "completed" && wordJob.download_url && (
                      <Button
                        className="w-full"
                        variant="secondary"
                        onClick={() =>
                          void handleDownload(wordJob.download_url, wordJob.output_filename || "edited.docx")
                        }
                        disabled={downloading}
                      >
                        <Download className="h-4 w-4" /> Unduh Word (.docx)
                      </Button>
                    )}
                    {wordJob?.status === "failed" && (
                      <p className="text-xs text-destructive">
                        Versi Word gagal dibuat: {wordJob.error_message}
                      </p>
                    )}
                  </div>
                )}

                <div className="space-y-2 rounded-lg border bg-muted/30 p-2.5 text-xs">
                  <div className="flex items-center justify-between gap-2">
                    <span className="flex items-center gap-1.5 font-medium">
                      <Save className="h-3.5 w-3.5" /> Draft susunan
                    </span>
                    <span className="text-[11px] text-muted-foreground">
                      {draftMatchesCurrent
                        ? "Tersimpan otomatis"
                        : savedDraft
                          ? "Draft PDF lain"
                          : "Belum disimpan"}
                    </span>
                  </div>
                  {draftMatchesCurrent && savedDraft && (
                    <p className="text-[11px] text-muted-foreground">
                      Terakhir disimpan {formatSavedAt(savedDraft.savedAt)}
                    </p>
                  )}
                  <div className="flex flex-wrap gap-1.5">
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => saveDraft(true)}
                      disabled={busy || !items.length}
                    >
                      <Save className="h-3.5 w-3.5" /> Simpan susunan
                    </Button>
                    {savedDraft && (
                      <Button type="button" size="sm" variant="ghost" onClick={clearDraft} disabled={busy}>
                        <Trash2 className="h-3.5 w-3.5" /> Hapus draft
                      </Button>
                    )}
                  </div>
                  <p className="text-[11px] text-muted-foreground">
                    Draft disimpan di peramban ini dan dipulihkan saat PDF yang sama dibuka
                    kembali. Gambar perlu diunggah ulang.
                  </p>
                </div>

                <p className="text-[11px] text-muted-foreground">
                  Versi Word mengikuti tata letak PDF dan bisa diedit di Word/LibreOffice.
                </p>
              </CardContent>
            </Card>
          </aside>
        </div>
      )}
    </div>
  );
}

function EditorElement({
  item,
  assets,
  selected,
  scaleX,
  scaleY,
  onPointerDown,
  onResize,
  onResizeLine,
  onOpenTextEdit,
}: {
  item: EditItem;
  assets: Asset[];
  selected: boolean;
  scaleX: number;
  scaleY: number;
  onPointerDown: (event: React.PointerEvent<HTMLDivElement>, item: EditItem) => void;
  onResize: (event: React.PointerEvent<HTMLSpanElement>, item: ImageItem | ShapeItem) => void;
  onResizeLine: (
    event: React.PointerEvent<HTMLSpanElement>,
    item: LineItem,
    endpoint: "start" | "end",
  ) => void;
  onOpenTextEdit: (item: TextEditItem) => void;
}) {
  const common = {
    left: item.x * scaleX,
    top: item.y * scaleY,
  };
  const outline = selected ? "outline outline-2 outline-primary" : "";

  if (item.type === "text") {
    return (
      <div
        className={cn("pointer-events-auto absolute cursor-move px-0.5", outline)}
        style={{
          ...common,
          color: item.color,
          fontSize: Math.max(6, item.size * scaleY),
          fontFamily: FONT_STACK[item.font],
          fontWeight: item.bold ? 700 : 400,
          fontStyle: item.italic ? "italic" : "normal",
          lineHeight: 1.25,
          whiteSpace: "pre-wrap",
        }}
        onPointerDown={(event) => onPointerDown(event, item)}
      >
        {item.text || " "}
      </div>
    );
  }

  if (item.type === "image") {
    const asset = assets[item.asset];
    return (
      <div
        className={cn("pointer-events-auto absolute cursor-move", outline)}
        style={{ ...common, width: item.w * scaleX, height: item.h * scaleY }}
        onPointerDown={(event) => onPointerDown(event, item)}
      >
        {asset ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={asset.url}
            alt={asset.file.name}
            draggable={false}
            className="h-full w-full object-contain"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center bg-muted text-[10px] text-muted-foreground">
            Gambar
          </div>
        )}
        {selected && <ResizeHandle onPointerDown={(event) => onResize(event, item)} />}
      </div>
    );
  }

  if (item.type === "text-edit") {
    const baseline = item.baseline ?? item.y + item.h;
    const textOffset = Math.max(0, (baseline - item.size - item.y) * scaleY);
    return (
      <div
        className={cn("pointer-events-auto absolute cursor-text overflow-visible px-0.5", outline)}
        style={{
          ...common,
          width: item.w * scaleX,
          height: item.h * scaleY,
          paddingTop: textOffset,
          background: "#ffffff",
          color: item.color,
          fontSize: Math.max(6, item.size * scaleY),
          fontFamily: FONT_STACK[item.font],
          fontWeight: item.bold ? 700 : 400,
          fontStyle: item.italic ? "italic" : "normal",
          lineHeight: 1,
          whiteSpace: "pre",
        }}
        onPointerDown={(event) => onPointerDown(event, item)}
        onDoubleClick={(event) => {
          event.stopPropagation();
          onOpenTextEdit(item);
        }}
        title="Klik dua kali untuk mengedit teks"
      >
        {item.text}
      </div>
    );
  }

  if (item.type === "line") {
    const dx = (item.x2 - item.x) * scaleX;
    const dy = (item.y2 - item.y) * scaleY;
    const length = Math.hypot(dx, dy);
    const angle = Math.atan2(dy, dx);
    return (
      <div
        className="pointer-events-auto absolute cursor-move"
        style={{
          left: item.x * scaleX,
          top: item.y * scaleY,
          width: length,
          height: 10,
          transform: `translateY(-5px) rotate(${angle}rad)`,
          transformOrigin: "0 5px",
        }}
        onPointerDown={(event) => onPointerDown(event, item)}
      >
        <div
          className={cn("h-full w-full", selected && "bg-primary/10")}
          style={{
            borderTop: `${Math.max(0.75, item.strokeWidth * scaleY)}px solid ${item.stroke}`,
            marginTop: 5,
          }}
        />
        {selected && (
          <>
            <span
              role="presentation"
              onPointerDown={(event) => onResizeLine(event, item, "start")}
              className="absolute -left-1.5 top-[-1px] z-10 h-3 w-3 rounded-full border border-white bg-primary shadow"
            />
            <span
              role="presentation"
              onPointerDown={(event) => onResizeLine(event, item, "end")}
              className="absolute -right-1.5 top-[-1px] z-10 h-3 w-3 rounded-full border border-white bg-primary shadow"
            />
          </>
        )}
      </div>
    );
  }

  const shapeStyle: React.CSSProperties = {
    ...common,
    width: item.w * scaleX,
    height: item.h * scaleY,
    border: `${Math.max(1, item.strokeWidth * scaleY)}px solid ${item.stroke}`,
    background: item.hasFill ? item.fill : "transparent",
    borderRadius: item.type === "ellipse" ? "50%" : 2,
  };
  return (
    <div
      className={cn("pointer-events-auto absolute cursor-move", outline)}
      style={shapeStyle}
      onPointerDown={(event) => onPointerDown(event, item)}
    >
      {selected && <ResizeHandle onPointerDown={(event) => onResize(event, item)} />}
    </div>
  );
}

function ResizeHandle({
  onPointerDown,
}: {
  onPointerDown: (event: React.PointerEvent<HTMLSpanElement>) => void;
}) {
  return (
    <span
      role="presentation"
      onPointerDown={onPointerDown}
      className="absolute -bottom-1.5 -right-1.5 h-3 w-3 cursor-nwse-resize rounded-sm border border-white bg-primary shadow"
    />
  );
}

function DraftShape({ draft, page }: { draft: Draft; page: EditorPage }) {
  const scaleX = page.displayWidth / page.widthPt;
  const scaleY = page.displayHeight / page.heightPt;
  if (draft.mode === "line") {
    const dx = (draft.x1 - draft.x0) * scaleX;
    const dy = (draft.y1 - draft.y0) * scaleY;
    return (
      <div
        className="absolute"
        style={{
          left: draft.x0 * scaleX,
          top: draft.y0 * scaleY,
          width: Math.hypot(dx, dy),
          height: 2,
          transform: `translateY(-1px) rotate(${Math.atan2(dy, dx)}rad)`,
          transformOrigin: "0 1px",
          borderTop: "2px dashed #2563eb",
        }}
      />
    );
  }
  return (
    <div
      className="absolute border-2 border-dashed border-primary bg-primary/10"
      style={{
        left: Math.min(draft.x0, draft.x1) * scaleX,
        top: Math.min(draft.y0, draft.y1) * scaleY,
        width: Math.abs(draft.x1 - draft.x0) * scaleX,
        height: Math.abs(draft.y1 - draft.y0) * scaleY,
        borderRadius: draft.mode === "ellipse" ? "50%" : 2,
      }}
    />
  );
}

function serializeItems(items: EditItem[]): Record<string, unknown>[] {
  return items.map((item) => {
    if (item.type === "text") {
      return {
        type: "text",
        page: item.page,
        x: round(item.x),
        y: round(item.y),
        text: item.text,
        size: round(item.size),
        color: item.color,
        font: item.font,
        bold: item.bold,
        italic: item.italic,
      };
    }
    if (item.type === "image") {
      return {
        type: "image",
        page: item.page,
        x: round(item.x),
        y: round(item.y),
        w: round(item.w),
        h: round(item.h),
        asset: item.asset,
      };
    }
    if (item.type === "text-edit") {
      return {
        type: "text-edit",
        page: item.page,
        x: round(item.x),
        y: round(item.y),
        w: round(item.w),
        h: round(item.h),
        text: item.text,
        size: round(item.size),
        color: item.color,
        font: item.font,
        bold: item.bold,
        italic: item.italic,
        auto_style: item.autoStyle,
        baseline: round(item.baseline ?? item.y + item.h),
      };
    }
    if (item.type === "line") {
      return {
        type: "line",
        page: item.page,
        x: round(item.x),
        y: round(item.y),
        x2: round(item.x2),
        y2: round(item.y2),
        stroke: item.stroke,
        stroke_width: round(item.strokeWidth),
      };
    }
    return {
      type: item.type,
      page: item.page,
      x: round(item.x),
      y: round(item.y),
      w: round(item.w),
      h: round(item.h),
      stroke: item.stroke,
      fill: item.hasFill ? item.fill : null,
      stroke_width: round(item.strokeWidth),
    };
  });
}
