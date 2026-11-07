import type { Metadata } from "next";

import { ToolsExplorer } from "@/components/tools-explorer";

export const metadata: Metadata = {
  title: "Alat Konversi",
  description: "Semua alat konversi dokumen yang tersedia di server XConvert.",
};

export default function ToolsPage() {
  return (
    <div className="container py-10">
      <div className="max-w-2xl">
        <h1 className="text-3xl font-semibold tracking-tight">Alat konversi</h1>
        <p className="mt-2 text-muted-foreground">
          Daftar ini dimuat langsung dari API <code className="rounded bg-muted px-1.5 py-0.5 text-xs">/api/v1/formats</code>.
          Alat yang dependensinya tidak terpasang di server ditandai tidak tersedia, bukan disembunyikan.
        </p>
      </div>
      <div className="mt-8">
        <ToolsExplorer />
      </div>
    </div>
  );
}
