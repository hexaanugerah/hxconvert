import Link from "next/link";

import { Logo } from "@/components/logo";

const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

export function SiteFooter() {
  return (
    <footer className="mt-20 border-t bg-card/40">
      <div className="container grid gap-10 py-12 md:grid-cols-4">
        <div className="md:col-span-2">
          <Logo />
          <p className="mt-4 max-w-sm text-sm leading-relaxed text-muted-foreground">
            HXconvertX mengubah PDF, Word, Excel, PowerPoint, dan gambar langsung di server Anda sendiri.
            Dokumen hasil konversi dihapus otomatis sesuai masa penyimpanan.
          </p>
        </div>
        <div>
          <h3 className="text-sm font-semibold">Produk</h3>
          <ul className="mt-3 space-y-2 text-sm text-muted-foreground">
            <li><Link href="/tools" className="hover:text-foreground">Alat konversi</Link></li>
            <li><Link href="/dashboard" className="hover:text-foreground">Dashboard</Link></li>
            <li><Link href="/history" className="hover:text-foreground">Riwayat</Link></li>
          </ul>
        </div>
        <div>
          <h3 className="text-sm font-semibold">Informasi</h3>
          <ul className="mt-3 space-y-2 text-sm text-muted-foreground">
            <li><Link href="/privacy" className="hover:text-foreground">Kebijakan privasi</Link></li>
            <li><a href={`${API_URL}/api/v1/health`} className="hover:text-foreground" target="_blank" rel="noreferrer">Status API</a></li>
            <li><Link href="/#faq" className="hover:text-foreground">FAQ</Link></li>
          </ul>
        </div>
      </div>
      <div className="border-t">
        <div className="container flex flex-col items-center justify-between gap-2 py-5 text-xs text-muted-foreground sm:flex-row">
          <p>© {new Date().getFullYear()} HXconvertX. Semua dokumen tetap milik Anda.</p>
          <p>Dibuat dengan Next.js, FastAPI, dan pustaka konversi sumber terbuka.</p>
        </div>
      </div>
    </footer>
  );
}
