"use client";

import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  CheckCircle2,
  FileLock2,
  Gauge,
  Layers,
  ScanText,
  ShieldCheck,
  Sparkles,
  TimerReset,
  Zap,
} from "lucide-react";
import Link from "next/link";

import { QuickStart } from "@/components/quick-start";
import { ToolCard } from "@/components/tool-card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { POPULAR_TOOL_IDS } from "@/lib/tools";
import { getFormats } from "@/services/formats";

const FEATURES = [
  {
    icon: Sparkles,
    title: "PDF ke Word yang benar-benar dapat diedit",
    description:
      "Teks, judul, daftar, tabel, dan gambar diekstrak menjadi DOCX. Bukan halaman hasil tangkapan layar.",
  },
  {
    icon: ScanText,
    title: "OCR untuk hasil pindai",
    description:
      "Halaman tanpa lapisan teks diproses dengan OCR, termasuk dukungan Bahasa Indonesia dan Inggris sesuai mesin yang tersedia.",
  },
  {
    icon: Layers,
    title: "Gabungkan, pisah, dan atur halaman",
    description:
      "Urutkan ulang, putar, hapus halaman, tambahkan nomor halaman dan tanda air, lalu kompres PDF.",
  },
  {
    icon: Zap,
    title: "Status pekerjaan nyata",
    description:
      "Frontend memantau status dari backend. Tidak ada indikator persentase palsu, hanya status yang dapat diverifikasi.",
  },
  {
    icon: TimerReset,
    title: "Penyimpanan yang jelas",
    description:
      "Hasil tersedia untuk periode terbatas dan dihapus otomatis oleh pembersih terjadwal di server.",
  },
  {
    icon: FileLock2,
    title: "Akses berbasis pemilik",
    description:
      "Setiap pekerjaan hanya dapat diakses oleh akun atau sesi anonim yang membuatnya. Unduhan diperiksa kembali di server.",
  },
];

const STEPS = [
  {
    title: "Unggah berkas",
    description: "Pilih alat atau jatuhkan berkas di area unggah. Format dan isi berkas divalidasi di server.",
  },
  {
    title: "Tentukan opsi",
    description: "Atur halaman, kualitas, bahasa OCR, atau opsi lain yang memang didukung alat tersebut.",
  },
  {
    title: "Unduh hasil",
    description: "Pantau status pekerjaan, lalu unduh hasil konversi sebelum masa penyimpanan berakhir.",
  },
];

const FAQ = [
  {
    question: "Apakah hasil konversi PDF ke Word dapat diedit?",
    answer:
      "Ya. XConvert mengekstrak teks dan struktur PDF lalu membangun DOCX dengan paragraf, gaya judul, daftar, tabel, dan gambar. Tata letak yang sangat kompleks seperti beberapa kolom, rumus, atau tabel bergabung dapat berubah dan akan dilaporkan sebagai catatan hasil.",
  },
  {
    question: "Format apa yang benar-benar didukung?",
    answer:
      "Daftar alat di halaman Alat Konversi diambil langsung dari backend. Alat yang bergantung pada LibreOffice atau mesin OCR hanya ditampilkan sebagai tersedia jika dependensi tersebut terpasang di server.",
  },
  {
    question: "Berapa lama berkas saya disimpan?",
    answer:
      "Berkas masukan dihapus setelah pekerjaan selesai. Berkas hasil disimpan sesuai masa berlaku yang tertera pada halaman konversi dan dihapus otomatis oleh proses pembersihan server.",
  },
  {
    question: "Apakah saya harus membuat akun?",
    answer:
      "Tidak. Konversi dasar dapat digunakan tanpa akun dan riwayat tamu terikat pada cookie peramban Anda. Membuat akun membuat riwayat dapat diakses dari perangkat lain dan memberi Anda pengaturan profil.",
  },
  {
    question: "Apakah dokumen saya dibaca atau dicatat?",
    answer:
      "Isi dokumen tidak ditulis ke log aplikasi. Log hanya mencatat peristiwa pekerjaan seperti waktu mulai, status, dan ukuran berkas. Dokumen tetap berada di penyimpanan server lokal hingga dihapus.",
  },
];

export default function LandingPage() {
  const formatsQuery = useQuery({ queryKey: ["formats"], queryFn: getFormats, staleTime: 60_000 });
  const formats = formatsQuery.data;
  const popularTools = POPULAR_TOOL_IDS.map((id) => formats?.tools.find((tool) => tool.id === id)).filter(
    (tool): tool is NonNullable<typeof tool> => Boolean(tool),
  );

  return (
    <>
      <section className="hero-glow relative overflow-hidden border-b">
        <div className="container grid items-center gap-10 py-14 sm:py-20 lg:grid-cols-[1.1fr_0.9fr] lg:gap-14">
          <div className="animate-fade-in">
            <span className="inline-flex items-center gap-2 rounded-full border bg-card px-3 py-1 text-xs font-medium text-muted-foreground">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
              Berjalan di server Anda sendiri
            </span>
            <h1 className="mt-5 text-3xl font-semibold leading-tight tracking-tight sm:text-4xl lg:text-5xl">
              Konversi dokumen dengan cepat,{" "}
              <span className="text-primary">akurat, dan aman.</span>
            </h1>
            <p className="mt-4 max-w-xl text-base leading-relaxed text-muted-foreground sm:text-lg">
              PDF ke Word yang dapat diedit, Word ke PDF, tabel Excel, presentasi, gambar, dan OCR —
              semuanya diproses oleh backend nyata dengan penyimpanan berkas yang terkendali.
            </p>
            <div className="mt-7 flex flex-wrap items-center gap-3">
              <Button size="lg" asChild>
                <Link href="/tools">Mulai konversi <ArrowRight className="h-4 w-4" /></Link>
              </Button>
              <Button size="lg" variant="outline" asChild>
                <Link href="/register">Buat akun gratis</Link>
              </Button>
            </div>
            <ul className="mt-7 flex flex-wrap gap-x-6 gap-y-2 text-sm text-muted-foreground">
              {["Tanpa perlu kartu kredit", "Unduhan langsung", "Berkas dihapus otomatis"].map((item) => (
                <li key={item} className="flex items-center gap-1.5">
                  <CheckCircle2 className="h-4 w-4 text-emerald-500" /> {item}
                </li>
              ))}
            </ul>
          </div>

          <div className="animate-fade-in lg:pl-4">
            <QuickStart formats={formats} />
          </div>
        </div>
      </section>

      <section className="border-b bg-card/40">
        <div className="container grid grid-cols-2 gap-6 py-6 text-center sm:grid-cols-4">
          {[
            { value: formats ? `${formats.tools.length}` : "—", label: "alat konversi terdaftar" },
            { value: formats ? `${formats.capabilities.limits.max_upload_mb} MB` : "—", label: "batas per berkas" },
            { value: formats ? `${formats.capabilities.limits.result_ttl_hours} jam` : "—", label: "masa simpan hasil" },
            { value: formats?.capabilities.ocr.available ? "Aktif" : "Nonaktif", label: "mesin OCR" },
          ].map((item) => (
            <div key={item.label}>
              <p className="text-lg font-semibold sm:text-xl">{item.value}</p>
              <p className="text-xs text-muted-foreground">{item.label}</p>
            </div>
          ))}
        </div>
      </section>

      <section className="container py-16" id="alat">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">Alat paling sering dipakai</h2>
            <p className="mt-2 max-w-2xl text-muted-foreground">
              Semua alat di bawah terhubung ke mesin konversi backend dan tersedia sesuai kapabilitas server.
            </p>
          </div>
          <Button variant="outline" asChild>
            <Link href="/tools">Lihat semua alat <ArrowRight className="h-4 w-4" /></Link>
          </Button>
        </div>

        <div className="mt-7 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {formatsQuery.isLoading &&
            Array.from({ length: 6 }).map((_, index) => <Skeleton key={index} className="h-48" />)}
          {popularTools.map((tool) => (
            <ToolCard key={tool.id} tool={tool} />
          ))}
          {formatsQuery.isError && (
            <div className="col-span-full rounded-xl border border-destructive/30 bg-destructive/5 p-5 text-sm">
              Backend belum dapat dihubungi. Jalankan server FastAPI lalu muat ulang halaman.
              <span className="mt-1 block text-muted-foreground">{(formatsQuery.error as Error).message}</span>
            </div>
          )}
        </div>

        {formats && (
          <div className="mt-10 rounded-xl border bg-card p-5">
            <div className="flex flex-wrap items-center gap-2">
              <span className="mr-1 text-sm font-medium">Format didukung:</span>
              {[...formats.formats.input, ...formats.formats.output]
                .filter((value, index, array) => array.indexOf(value) === index)
                .map((format) => (
                  <span key={format} className="rounded-md bg-muted px-2 py-1 text-xs font-medium uppercase text-muted-foreground">
                    {format}
                  </span>
                ))}
            </div>
          </div>
        )}
      </section>

      <section className="border-y bg-card/40 py-16" id="fitur">
        <div className="container">
          <div className="max-w-2xl">
            <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">Fitur yang benar-benar bekerja</h2>
            <p className="mt-2 text-muted-foreground">
              Kami tidak menampilkan tombol atau alat yang belum memiliki implementasi di backend.
            </p>
          </div>
          <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {FEATURES.map((feature) => (
              <div key={feature.title} className="rounded-xl border bg-card p-5">
                <span className="grid h-10 w-10 place-items-center rounded-lg bg-primary/10 text-primary">
                  <feature.icon className="h-5 w-5" aria-hidden />
                </span>
                <h3 className="mt-3 font-semibold">{feature.title}</h3>
                <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">{feature.description}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="container py-16">
        <div className="grid gap-10 lg:grid-cols-[0.9fr_1.1fr]">
          <div>
            <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">Alur konversi</h2>
            <p className="mt-2 text-muted-foreground">
              Tiga langkah, tanpa langkah tersembunyi. Semua status berasal dari pekerjaan backend yang sebenarnya.
            </p>
            <ol className="mt-7 space-y-5">
              {STEPS.map((step, index) => (
                <li key={step.title} className="flex gap-4">
                  <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full border bg-card text-sm font-semibold text-primary">
                    {index + 1}
                  </span>
                  <div>
                    <h3 className="font-medium">{step.title}</h3>
                    <p className="mt-1 text-sm text-muted-foreground">{step.description}</p>
                  </div>
                </li>
              ))}
            </ol>
          </div>

          <div className="rounded-2xl border bg-card p-6 sm:p-8" id="keamanan">
            <div className="flex items-center gap-2">
              <ShieldCheck className="h-5 w-5 text-emerald-500" />
              <h2 className="text-xl font-semibold">Keamanan & privasi dokumen</h2>
            </div>
            <p className="mt-3 text-sm leading-relaxed text-muted-foreground">
              XConvert dirancang untuk pemakaian lokal maupun server pribadi. Berkas tidak dikirim ke layanan
              pihak ketiga dan tidak dibaca oleh manusia.
            </p>
            <ul className="mt-5 space-y-3 text-sm">
              {[
                ["Validasi isi berkas", "Ekstensi dan isi berkas diperiksa, sehingga berkas dengan tipe palsu ditolak."],
                ["Nama penyimpanan acak", "Berkas disimpan dengan nama UUID, bukan nama asli pengguna, untuk mencegah path traversal."],
                ["Batas ukuran & jumlah", `Maksimal ${formats?.capabilities.limits.max_upload_mb ?? 25} MB per berkas dan ${formats?.capabilities.limits.max_files_per_job ?? 10} berkas per pekerjaan.`],
                ["Masa berlaku hasil", `Hasil tersedia ${formats?.capabilities.limits.result_ttl_hours ?? 24} jam, lalu dihapus oleh pembersih otomatis.`],
                ["Tanpa makro", "Konten aktif dan makro dari dokumen pengguna tidak pernah dijalankan."],
              ].map(([title, description]) => (
                <li key={title} className="flex gap-3">
                  <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />
                  <span>
                    <span className="font-medium">{title}</span>
                    <span className="block text-muted-foreground">{description}</span>
                  </span>
                </li>
              ))}
            </ul>
            <div className="mt-5 flex flex-wrap gap-4 border-t pt-4 text-xs text-muted-foreground">
              <span className="flex items-center gap-1.5"><Gauge className="h-3.5 w-3.5" /> Batas laju permintaan</span>
              <span className="flex items-center gap-1.5"><FileLock2 className="h-3.5 w-3.5" /> Akses per pemilik</span>
              <span className="flex items-center gap-1.5"><TimerReset className="h-3.5 w-3.5" /> Penghapusan tertunda</span>
            </div>
          </div>
        </div>
      </section>

      <section className="border-y bg-card/40 py-16" id="faq">
        <div className="container max-w-3xl">
          <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">Pertanyaan yang sering diajukan</h2>
          <div className="mt-6 divide-y rounded-xl border bg-card">
            {FAQ.map((item) => (
              <details key={item.question} className="group p-5">
                <summary className="cursor-pointer list-none font-medium marker:hidden">
                  <span className="flex items-start justify-between gap-4">
                    {item.question}
                    <ArrowRight className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
                  </span>
                </summary>
                <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{item.answer}</p>
              </details>
            ))}
          </div>
        </div>
      </section>

      <section className="container py-16">
        <div className="rounded-2xl border bg-gradient-to-br from-primary/10 via-card to-card p-8 text-center sm:p-12">
          <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">Siap mengonversi dokumen pertama Anda?</h2>
          <p className="mx-auto mt-2 max-w-xl text-muted-foreground">
            Tanpa pendaftaran untuk konversi dasar. Buat akun bila Anda ingin riwayat tersimpan lintas perangkat.
          </p>
          <div className="mt-6 flex flex-wrap justify-center gap-3">
            <Button size="lg" asChild><Link href="/tools">Pilih alat konversi</Link></Button>
            <Button size="lg" variant="outline" asChild><Link href="/history">Lihat riwayat</Link></Button>
          </div>
        </div>
      </section>
    </>
  );
}
