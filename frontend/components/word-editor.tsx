"use client";

import { useMemo, useRef, useState } from "react";
import { Download, FileText, Loader2, Plus, Save, Upload, X } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { API_URL, apiFetch, getToken } from "@/lib/api";

interface WordParagraph {
  index: number;
  text: string;
  style: string;
}

interface WordDocumentView {
  filename: string;
  paragraphs: WordParagraph[];
}

interface AddedParagraph {
  id: string;
  text: string;
}

function paragraphStyle(style: string): string {
  const normalized = style.toLowerCase();
  if (normalized.includes("title")) return "text-xl font-bold";
  if (normalized.includes("heading 1")) return "text-lg font-bold";
  if (normalized.includes("heading 2")) return "text-base font-semibold";
  if (normalized.includes("heading")) return "font-semibold";
  if (normalized.includes("quote")) return "border-l-2 border-primary/40 pl-3 italic";
  return "";
}

function newId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `paragraph-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function WordEditor() {
  const fileInput = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [documentView, setDocumentView] = useState<WordDocumentView | null>(null);
  const [changes, setChanges] = useState<Record<number, string>>({});
  const [additions, setAdditions] = useState<AddedParagraph[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const changedCount = useMemo(
    () => Object.keys(changes).filter((key) => {
      const paragraph = documentView?.paragraphs[Number(key)];
      return paragraph && changes[Number(key)] !== paragraph.text;
    }).length + additions.length,
    [additions.length, changes, documentView],
  );

  async function openFile(selected: File | undefined) {
    if (!selected) return;
    if (!selected.name.toLowerCase().endsWith(".docx")) {
      toast.error("Pilih dokumen Word berformat .docx.");
      return;
    }
    setBusy(true);
    setError(null);
    setFile(selected);
    setDocumentView(null);
    setChanges({});
    setAdditions([]);
    const form = new FormData();
    form.append("file", selected);
    try {
      const result = await apiFetch<WordDocumentView>("/api/v1/word/inspect", {
        method: "POST",
        body: form,
      });
      setDocumentView(result);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "Dokumen Word tidak dapat dibuka.";
      setError(message);
      setFile(null);
      toast.error("Gagal membuka dokumen", { description: message });
    } finally {
      setBusy(false);
    }
  }

  async function saveDocument() {
    if (!file || !documentView || busy) return;
    const edits = Object.entries(changes)
      .map(([index, text]) => ({ index: Number(index), text }))
      .filter(({ index, text }) => documentView.paragraphs[index]?.text !== text);
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("changes", JSON.stringify(edits));
    form.append("additions", JSON.stringify(additions.map((item) => item.text)));
    const headers = new Headers();
    const token = getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);

    setBusy(true);
    try {
      const response = await fetch(`${API_URL}/api/v1/word/save`, {
        method: "POST",
        headers,
        body: form,
        credentials: "include",
      });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new Error(body?.error?.message || `Gagal menyimpan dokumen (HTTP ${response.status}).`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${file.name.replace(/\.docx$/i, "")}-edited.docx`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      toast.success("Dokumen Word berhasil disimpan", { description: "Berkas hasil sudah diunduh." });
    } catch (cause) {
      toast.error("Gagal menyimpan dokumen", {
        description: cause instanceof Error ? cause.message : "Terjadi kesalahan saat menyimpan.",
      });
    } finally {
      setBusy(false);
    }
  }

  function addParagraph() {
    setAdditions((current) => [...current, { id: newId(), text: "" }]);
  }

  return (
    <div className="container py-8 sm:py-10">
      <div className="mx-auto max-w-5xl">
        <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
          <div>
            <p className="text-sm font-medium text-primary">Editor dokumen</p>
            <h1 className="mt-1 text-3xl font-semibold tracking-tight">Edit Word</h1>
            <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
              Ubah paragraf dan teks di tabel DOCX, tambahkan paragraf, lalu unduh dokumen hasilnya.
            </p>
          </div>
          {documentView && (
            <Button onClick={() => void saveDocument()} disabled={busy || changedCount === 0}>
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
              Simpan & unduh
            </Button>
          )}
        </div>

        {!documentView ? (
          <Card>
            <CardHeader>
              <CardTitle>Buka dokumen Word</CardTitle>
              <CardDescription>Unggah berkas DOCX. Isi dokumen akan ditampilkan untuk diedit di browser.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <Input
                ref={fileInput}
                type="file"
                accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                onChange={(event) => {
                  const selected = event.target.files?.[0];
                  event.target.value = "";
                  void openFile(selected);
                }}
                disabled={busy}
                aria-label="Pilih dokumen Word"
              />
              {busy && <p className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" /> Membuka dokumen…</p>}
              {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
              <p className="text-xs text-muted-foreground">
                Mendukung DOCX. Gaya paragraf dan format teks yang tidak diubah tetap dipertahankan.
              </p>
            </CardContent>
          </Card>
        ) : (
          <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_260px]">
            <section className="space-y-3" aria-label="Isi dokumen Word">
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border bg-card p-4">
                <div className="flex min-w-0 items-center gap-3">
                  <FileText className="h-5 w-5 shrink-0 text-primary" />
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">{documentView.filename}</p>
                    <p className="text-xs text-muted-foreground">
                      {documentView.paragraphs.length} paragraf · {changedCount} perubahan
                    </p>
                  </div>
                </div>
                <Button variant="outline" size="sm" onClick={() => fileInput.current?.click()} disabled={busy}>
                  <Upload className="h-4 w-4" /> Ganti berkas
                </Button>
                <input
                  ref={fileInput}
                  type="file"
                  accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                  className="sr-only"
                  onChange={(event) => {
                    const selected = event.target.files?.[0];
                    event.target.value = "";
                    void openFile(selected);
                  }}
                />
              </div>

              {documentView.paragraphs.map((paragraph) => (
                <div key={paragraph.index} className="rounded-lg border bg-card p-3">
                  <div className="mb-2 flex items-center justify-between gap-2">
                    <span className="text-[11px] text-muted-foreground">
                      Paragraf {paragraph.index + 1}
                    </span>
                    <span className="rounded bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">
                      {paragraph.style || "Normal"}
                    </span>
                  </div>
                  <textarea
                    value={changes[paragraph.index] ?? paragraph.text}
                    onChange={(event) => setChanges((current) => ({
                      ...current,
                      [paragraph.index]: event.target.value,
                    }))}
                    rows={Math.min(8, Math.max(2, (paragraph.text.match(/\n/g)?.length ?? 0) + 2))}
                    placeholder="Paragraf kosong"
                    aria-label={`Edit paragraf ${paragraph.index + 1}`}
                    className={`w-full resize-y rounded-md border border-input bg-background px-3 py-2 text-sm leading-relaxed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${paragraphStyle(paragraph.style)}`}
                  />
                </div>
              ))}

              {additions.map((item, index) => (
                <div key={item.id} className="rounded-lg border border-primary/40 bg-primary/5 p-3">
                  <div className="mb-2 flex items-center justify-between">
                    <span className="text-[11px] font-medium text-primary">Paragraf baru {index + 1}</span>
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={`Hapus paragraf baru ${index + 1}`}
                      onClick={() => setAdditions((current) => current.filter((entry) => entry.id !== item.id))}
                    >
                      <X className="h-4 w-4" />
                    </Button>
                  </div>
                  <textarea
                    value={item.text}
                    onChange={(event) => setAdditions((current) => current.map((entry) =>
                      entry.id === item.id ? { ...entry, text: event.target.value } : entry,
                    ))}
                    rows={3}
                    placeholder="Tulis paragraf baru…"
                    aria-label={`Isi paragraf baru ${index + 1}`}
                    className="w-full resize-y rounded-md border border-input bg-background px-3 py-2 text-sm leading-relaxed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  />
                </div>
              ))}
            </section>

            <aside className="space-y-3 lg:sticky lg:top-24 lg:self-start">
              <Card>
                <CardHeader className="pb-3">
                  <CardTitle className="text-base">Edit dokumen</CardTitle>
                  <CardDescription>Perubahan diterapkan ke salinan DOCX.</CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <Button variant="outline" className="w-full" onClick={addParagraph} disabled={busy}>
                    <Plus className="h-4 w-4" /> Tambah paragraf
                  </Button>
                  <Button className="w-full" onClick={() => void saveDocument()} disabled={busy || changedCount === 0}>
                    {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                    Simpan & unduh DOCX
                  </Button>
                  <p className="text-xs leading-relaxed text-muted-foreground">
                    Teks paragraf dan sel tabel bisa diubah. Gaya paragraf tetap dipertahankan; format per bagian teks dijaga sebisa mungkin.
                  </p>
                </CardContent>
              </Card>
            </aside>
          </div>
        )}
      </div>
    </div>
  );
}
