import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: "Kebijakan Privasi",
  description: "Bagaimana XConvert menyimpan, memproses, dan menghapus dokumen Anda.",
};

export default function PrivacyPage() {
  return (
    <div className="container max-w-3xl py-12">
      <h1 className="text-3xl font-semibold tracking-tight">Kebijakan privasi</h1>
      <p className="mt-2 text-sm text-muted-foreground">Terakhir diperbarui: 22 Oktober 2026</p>

      <div className="prose-doc mt-8">
        <h2>1. Prinsip utama</h2>
        <p>
          XConvert memproses dokumen di server yang menjalankan aplikasi ini. Dokumen tidak dikirim ke layanan
          pihak ketiga, tidak dianalisis untuk iklan, dan tidak dibaca oleh manusia. Isi dokumen tidak ditulis ke
          log aplikasi.
        </p>

        <h2>2. Data yang disimpan</h2>
        <ul>
          <li>Metadata pekerjaan: nama berkas asli, format, ukuran, alat, status, dan waktu.</li>
          <li>Berkas masukan sementara di penyimpanan server, dihapus setelah pekerjaan selesai.</li>
          <li>Berkas hasil konversi, disimpan sampai masa berlaku berakhir lalu dihapus otomatis.</li>
          <li>Untuk akun: nama, email, hash kata sandi, dan waktu pembuatan akun.</li>
          <li>Untuk pengguna anonim: pengenal sesi acak dalam cookie, bukan alamat IP atau identitas lain.</li>
        </ul>

        <h2>3. Masa penyimpanan</h2>
        <p>
          Berkas hasil dapat diunduh selama masa berlaku yang ditampilkan di halaman konversi (bawaan 24 jam).
          Pembersih terjadwal menghapus berkas dan menandai pekerjaan sebagai kedaluwarsa. Anda juga dapat
          menghapus pekerjaan atau seluruh riwayat kapan saja dari halaman Riwayat dan Pengaturan.
        </p>

        <h2>4. Keamanan</h2>
        <ul>
          <li>Validasi ekstensi dan isi berkas (magic bytes) dilakukan sebelum konversi.</li>
          <li>Berkas disimpan dengan nama acak dan tidak dapat diakses tanpa kepemilikan sesi atau akun.</li>
          <li>Kata sandi disimpan sebagai hash bcrypt; token akses ditandatangani dan memiliki masa berlaku.</li>
          <li>Makro dan konten aktif dalam dokumen tidak dijalankan.</li>
          <li>Batas ukuran, jumlah berkas, waktu proses, dan laju permintaan diterapkan.</li>
        </ul>
        <p>
          Tidak ada sistem yang sepenuhnya aman. Jika server dikelola pihak lain, administrator server secara
          teknis dapat mengakses berkas yang tersimpan selama masa berlaku. Untuk dokumen sangat sensitif,
          gunakan instalasi lokal dan hapus riwayat segera setelah selesai.
        </p>

        <h2>5. Cookie dan autentikasi</h2>
        <p>
          Cookie <code>dc_access</code> menyimpan sesi login, dan <code>dc_guest</code> mengikat riwayat tamu ke
          peramban Anda. Cookie bersifat HttpOnly dan SameSite=Lax. Token akses juga disimpan di localStorage
          pada peramban Anda untuk permintaan API; Anda dapat keluar kapan saja untuk menghapusnya.
        </p>

        <h2>6. Hak Anda</h2>
        <ul>
          <li>Menghapus satu pekerjaan beserta berkasnya melalui Riwayat.</li>
          <li>Menghapus seluruh riwayat melalui Riwayat atau Pengaturan.</li>
          <li>Memperbarui nama dan email akun melalui Pengaturan.</li>
          <li>Mengganti kata sandi kapan saja.</li>
        </ul>

        <h2>7. Layanan eksternal</h2>
        <p>
          Backend menggunakan pustaka sumber terbuka (PyMuPDF, pdfplumber, python-docx, openpyxl, python-pptx,
          Pillow) dan dapat memakai LibreOffice/Tesseract bila dipasang oleh administrator server. Layanan ini
          berjalan di infrastruktur yang sama dan tidak mengirim dokumen ke internet.
        </p>

        <h2>8. Kontak</h2>
        <p>
          Karena XConvert adalah aplikasi yang dijalankan sendiri, pertanyaan privasi ditujukan kepada
          pengelola server tempat Anda menggunakan aplikasi ini.
        </p>
      </div>

      <div className="mt-10 rounded-xl border bg-card p-5 text-sm text-muted-foreground">
        Ingin menghapus data sekarang? Buka <Link href="/history" className="text-primary hover:underline">Riwayat konversi</Link>{" "}
        atau <Link href="/settings" className="text-primary hover:underline">Pengaturan</Link>.
      </div>
    </div>
  );
}
