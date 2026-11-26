import type { Metadata } from "next";
import Link from "next/link";
import { FileText, PenTool, ArrowRight } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export const metadata: Metadata = {
  title: "Edit Dokumen",
  description: "Pilih editor PDF atau Word untuk mulai mengubah dokumen.",
};

const EDITORS = [
  {
    href: "/convert/edit-pdf",
    title: "Edit PDF",
    description: "Tambahkan teks, gambar, kotak, elips, dan garis. Atur ulang elemen lalu unduh PDF.",
    icon: PenTool,
  },
  {
    href: "/edit/word",
    title: "Edit Word",
    description: "Ubah paragraf dan teks di tabel DOCX, tambahkan paragraf, lalu unduh hasilnya.",
    icon: FileText,
  },
];

export default function EditDocumentsPage() {
  return (
    <div className="container py-10">
      <div className="max-w-2xl">
        <p className="text-sm font-medium text-primary">Ruang kerja dokumen</p>
        <h1 className="mt-1 text-3xl font-semibold tracking-tight">Edit dokumen</h1>
        <p className="mt-2 text-muted-foreground">
          Pilih jenis dokumen untuk membuka editor di browser.
        </p>
      </div>
      <div className="mt-7 grid gap-4 md:grid-cols-2">
        {EDITORS.map(({ href, title, description, icon: Icon }) => (
          <Card key={href} className="flex flex-col">
            <CardHeader>
              <div className="mb-2 grid h-11 w-11 place-items-center rounded-xl bg-primary/10 text-primary">
                <Icon className="h-5 w-5" />
              </div>
              <CardTitle>{title}</CardTitle>
              <CardDescription>{description}</CardDescription>
            </CardHeader>
            <CardContent className="mt-auto">
              <Button asChild>
                <Link href={href}>Buka editor <ArrowRight className="h-4 w-4" /></Link>
              </Button>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
