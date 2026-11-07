import type { Metadata, Viewport } from "next";

import { SiteFooter } from "@/components/site-footer";
import { SiteHeader } from "@/components/site-header";

import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: {
    default: "HXconvertX — Konversi dokumen yang cepat dan aman",
    template: "%s | HXconvertX",
  },
  description:
    "Konversi PDF ke Word, Word ke PDF, Excel, PowerPoint, gambar, dan OCR dalam satu aplikasi. Berkas Anda diproses di server sendiri dan dihapus otomatis.",
  keywords: ["konversi pdf", "pdf ke word", "word ke pdf", "ocr", "excel ke csv"],
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f8fafc" },
    { media: "(prefers-color-scheme: dark)", color: "#0b1220" },
  ],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="id" suppressHydrationWarning>
      <body className="flex min-h-screen flex-col">
        <Providers>
          <SiteHeader />
          <main className="flex-1">{children}</main>
          <SiteFooter />
        </Providers>
      </body>
    </html>
  );
}
