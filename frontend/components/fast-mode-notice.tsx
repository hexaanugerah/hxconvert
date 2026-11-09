import { AlertTriangle } from "lucide-react";

/**
 * Explains what the fast PDF -> Word mode really produces. Rendered only while
 * fast mode is selected so the trade-off is visible before converting.
 */
export function FastModeNotice({
  visible,
  dpi = 72,
}: {
  visible: boolean;
  dpi?: string | number | boolean;
}) {
  if (!visible) return null;
  const resolution = String(dpi ?? "").trim() || "72";

  return (
    <div
      role="status"
      className="mt-4 flex items-start gap-2.5 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-xs"
    >
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600" />
      <div className="space-y-1">
        <p className="font-medium">Mode cepat menghasilkan gambar, bukan teks</p>
        <p className="text-muted-foreground">
          Setiap halaman disimpan sebagai gambar JPEG {resolution} DPI, jadi teks di dalam DOCX
          tidak dapat diedit dan ketajamannya setara pratinjau layar. Naikkan{" "}
          <span className="font-medium">fast_dpi</span> bila hasil perlu dibaca detail atau dicetak;
          mode ini tetap paling cepat untuk batch besar.
        </p>
      </div>
    </div>
  );
}
