# DocuConvert

Aplikasi web full-stack untuk mengonversi dan mengolah dokumen: PDF, Word, Excel,
PowerPoint, gambar, teks, dan OCR. Frontend Next.js terhubung ke REST API FastAPI
dengan database SQLAlchemy, penyimpanan berkas lokal, autentikasi, dan mesin
konversi yang benar-benar berjalan di server.

Tidak ada endpoint palsu dan tidak ada data statis yang menyamarkan fitur yang
belum selesai. Daftar alat di UI diambil dari backend, dan alat yang dependensinya
tidak tersedia ditandai **tidak tersedia** beserta alasannya.

---

## Ringkasan status

| Area | Status |
|---|---|
| Frontend Next.js 15 (App Router) | Selesai: landing, alat, konverter, dashboard, riwayat, pengaturan, login/daftar, privasi |
| Backend FastAPI | Selesai: auth JWT + cookie, pekerjaan konversi, unduhan, pembersihan otomatis |
| Database | SQLite (default) / PostgreSQL, model + migrasi Alembic |
| PDF → Word | Selesai, dua mode: **prioritaskan tata letak** (`pdf2docx`, gambar raster/JPEG2000/vektor dipertahankan) dan **prioritaskan pengeditan** (paragraf, tabel, gambar sebaris, OCR) |
| Ukuran & orientasi halaman | Dipertahankan per halaman: PDF → Word memakai section terpisah untuk ukuran campuran; Word → PDF membaca page setup; gambar → PDF menjaga rasio |
| Pratinjau multi-halaman | Semua halaman PDF/gambar dirender; API menyediakan URL pratinjau nyata per halaman |
| PDF → Excel / PowerPoint / gambar | Selesai |
| Alat PDF lain | Merge, split, rotate, compress, ekstrak teks, ekstrak tabel, nomor halaman, watermark, hapus halaman, urutkan halaman |
| Word | DOCX → PDF/TXT/HTML/Markdown, TXT/Markdown → DOCX, merge, rapikan format |
| Word → PDF | **Microsoft Word Automation** sebagai renderer referensi di Windows (pagination, font, header/footer, tabel, gambar); LibreOffice lalu HTML sebagai fallback dengan peringatan |
| Excel | XLSX → PDF/CSV/HTML, CSV → XLSX |
| PowerPoint | PPTX → PDF/gambar **(memerlukan LibreOffice)** |
| Gambar & OCR | Konversi JPG/PNG/WebP, gambar → PDF, OCR gambar, PDF pindai → DOCX |
| Keamanan berkas | Validasi isi (magic bytes), nama acak, batas ukuran/jumlah, timeout, akses per pemilik, retensi |
| Pengujian | 78 tes backend lulus (termasuk fidelitas gambar/Word, multi-halaman, pratinjau, batch, alat PDF/gambar baru, konversi nyata, dan kontrol akses) |
| Build frontend | `tsc --noEmit` dan `next build` (standalone) lulus |
| Deployment | Docker Compose produksi (Postgres, nginx, certbot) + panduan di `DEPLOYMENT.md` |
| Verifikasi UI | Konversi PDF→Word diuji melalui browser headless terhadap backend asli |

### Keterbatasan lingkungan saat ini

- **Microsoft Word terpasang** di mesin pengembangan Windows, sehingga DOCX → PDF memakai Word Automation dan tata letaknya identik dengan Word. Di server tanpa Word (termasuk Docker Linux), backend otomatis memakai LibreOffice bila ada, lalu renderer HTML dengan peringatan.
- **LibreOffice tidak terpasang** di mesin pengembangan ini. Akibatnya:
  - PPTX → PDF dan PPTX → gambar ditandai tidak tersedia (dan tidak akan muncul sebagai alat yang bisa dipakai).
  - XLSX → PDF memakai renderer HTML fallback (rapi untuk teks/tabel, tetapi tidak identik dengan tata letak Excel).
  - Pasang LibreOffice lalu restart backend untuk alat PowerPoint dan hasil Excel yang lebih akurat.
- OCR memakai **RapidOCR (ONNX)** karena Tesseract tidak terpasang. RapidOCR menangani aksara Latin dengan baik; bila Tesseract beserta data bahasa Indonesia tersedia, backend otomatis memilih Tesseract.
- Antrean pekerjaan berjalan **in-process** (thread + status polling). Cocok untuk satu node. Untuk skala horizontal, ganti `app/workers/runner.py` dengan Celery/RQ tanpa mengubah kontrak API.
- Penyimpanan objek S3 **belum diimplementasikan**; penyimpanan lokal adalah satu-satunya backend penyimpanan.
- Belum ada verifikasi email, reset kata sandi, atau login pihak ketiga.

---

## Arsitektur

```text
convert/  (root proyek)
├── frontend/                  Next.js 15 + TypeScript + Tailwind + shadcn-style UI
│   ├── app/                   Rute App Router (landing, tools, convert, dashboard, history, settings, auth)
│   ├── components/            Komponen UI, dropzone, pratinjau PDF.js, konverter
│   ├── hooks/                 (disiapkan untuk hook bersama)
│   ├── lib/                   Klien API, util, metadata alat
│   ├── services/              Panggilan API per domain
│   ├── store/                 Zustand: sesi auth dan berkas tertunda
│   ├── types/                 Tipe bersama
│   └── public/                Aset statis + worker PDF.js (disalin saat dev/build)
├── backend/
│   ├── app/
│   │   ├── main.py            Aplikasi FastAPI, CORS, lifespan, health
│   │   ├── api/v1/            Router auth, conversions, formats, pdf, ocr
│   │   ├── core/              Config, database, security, storage, error, rate limit
│   │   ├── models/            User, ConversionJob, ConversionLog
│   │   ├── schemas/           Validasi/response Pydantic
│   │   ├── repositories/      Akses database
│   │   ├── services/          Mesin konversi (pdf_*, docx_ops, word_com, excel_ops, pptx_ops, image_ops, ocr)
│   │   └── workers/           Eksekusi pekerjaan + pembersih kedaluwarsa
│   ├── alembic/               Migrasi database
│   ├── tests/                 78 tes integrasi + unit melalui HTTP
│   ├── requirements.txt
│   └── .env.example
├── storage/                   inputs/, outputs/, tmp/ (berkas dokumen — tidak di-commit)
├── scripts/start-dev.ps1      Bootstrap + jalankan backend & frontend di Windows
├── docs/API.md                Dokumentasi API
└── README.md
```

### Alur pekerjaan

1. Frontend mengirim `multipart/form-data` ke `POST /api/v1/conversions`.
2. Backend memvalidasi alat, jumlah berkas, ekstensi, isi berkas (magic bytes), dan ukuran.
3. Berkas disimpan dengan nama UUID di `storage/inputs/{job_id}/`, metadata di database.
4. Pekerjaan berjalan di background thread dengan timeout; status `queued → processing → completed|failed`.
5. Frontend mem-polling status setiap 1,5 detik (tidak ada persentase palsu).
6. Hasil dipindahkan ke `storage/outputs/{job_id}/`, tersedia lewat endpoint unduhan yang memeriksa kepemilikan dan masa berlaku.
7. Backend memeriksa halaman hasil/sumber dan merender satu PNG pratinjau per halaman (dibatasi `PREVIEW_MAX_PAGES`). Metadata ukuran, orientasi, dan status per halaman disimpan pada pekerjaan; URL pratinjau hanya dibuat untuk berkas yang benar-benar ada.
8. Berkas masukan dihapus setelah selesai; hasil dan pratinjau dihapus oleh pembersih terjadwal saat kedaluwarsa.

---

## Persyaratan

- **Windows 10/11** (target utama; Linux/macOS juga berjalan)
- **Python 3.11+** (diuji dengan 3.11.5)
- **Node.js 18+** dan npm (diuji dengan Node 26)
- Opsional:
  - **Microsoft Word** (Windows) + `pywin32` — renderer DOCX → PDF paling akurat. `pywin32` dipasang otomatis dari requirements di Windows.
  - **LibreOffice** — fallback DOCX/XLSX → PDF dan syarat alat PowerPoint
  - **Tesseract OCR + data bahasa** — alternatif OCR; RapidOCR sudah tercakup di requirements

Tidak perlu Docker. Tidak perlu PostgreSQL untuk pengembangan (SQLite default).

---

## Menjalankan cepat (Windows)

```powershell
# dari root proyek
.\scripts\start-dev.ps1
```

Skrip akan:

1. Memeriksa Python, Node.js, npm, dan port 8000/3000.
2. Membuat `backend\.env` (dengan `SECRET_KEY` acak) dan `frontend\.env.local` bila belum ada.
3. Membuat `.venv`, memasang dependensi Python, dan `npm install` bila diperlukan.
4. Melaporkan status Microsoft Word, LibreOffice, dan OCR (tidak menghentikan aplikasi bila tidak ada).
5. Menjalankan `alembic upgrade head`.
6. Membuka backend di `http://127.0.0.1:8000` dan frontend di `http://127.0.0.1:3000`.

> Jika PowerShell menolak menjalankan skrip, gunakan:
> `powershell -ExecutionPolicy Bypass -File .\scripts\start-dev.ps1`

### Menjalankan manual

```powershell
# Backend
cd backend
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env          # lalu isi SECRET_KEY
.\.venv\Scripts\python -m alembic upgrade head
.\.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000

# Frontend (terminal lain)
cd frontend
npm install
copy .env.local.example .env.local
npm run dev
```

Buka `http://localhost:3000`. Dokumentasi API interaktif di `http://127.0.0.1:8000/docs`.

### Catatan Windows

- Gunakan `127.0.0.1` pada `NEXT_PUBLIC_API_URL`. `localhost` dapat diresolusi ke
  IPv6 (`::1`) sementara backend mendengarkan IPv4.
- Port frontend 3000 dan 3001 sudah diizinkan CORS secara default. Jika memakai
  port lain, tambahkan ke `CORS_ORIGINS` di `backend\.env`.

---

## Konfigurasi

Salin dari `backend/.env.example`. Variabel penting:

| Variabel | Default | Keterangan |
|---|---|---|
| `SECRET_KEY` | — | Wajib diganti di produksi (penandatangan JWT). |
| `DATABASE_URL` | SQLite `backend/docuconvert.db` | PostgreSQL: `postgresql+psycopg://user:pass@host/db` (instal `psycopg`). |
| `STORAGE_DIR` | `../storage` | Root penyimpanan lokal. |
| `MAX_UPLOAD_MB` | `25` | Batas per berkas. |
| `MAX_FILES_PER_JOB` | `10` | Batas jumlah berkas per pekerjaan. |
| `RESULT_TTL_HOURS` | `24` | Masa simpan hasil sebelum dihapus. |
| `CLEANUP_INTERVAL_MINUTES` | `15` | Interval pembersih berkas kedaluwarsa. |
| `CONVERSION_TIMEOUT_SECONDS` | `180` | Batas waktu eksekusi konversi. |
| `PREVIEW_DPI` | `96` | DPI pratinjau halaman (terpisah dari kualitas unduhan). |
| `PREVIEW_MAX_WIDTH` | `1400` | Lebar maksimum gambar pratinjau dalam piksel. |
| `PREVIEW_MAX_PAGES` | `60` | Jumlah halaman maksimum yang dirender menjadi pratinjau per pekerjaan; halaman lain berstatus `pending`. |
| `ALLOW_ANONYMOUS` | `true` | Izinkan konversi tanpa akun. |
| `RATE_LIMIT_PER_MINUTE` | `60` | Batas permintaan per IP (in-memory, per proses). |
| `CORS_ORIGINS` | localhost:3000/3001 | Origin frontend yang diizinkan. |
| `LIBREOFFICE_PATH` | otomatis | Path `soffice.exe` bila tidak ada di PATH. |
| `TESSERACT_CMD` | otomatis | Path `tesseract.exe` bila tidak ada di PATH. |

Frontend hanya membutuhkan `NEXT_PUBLIC_API_URL`.

---

## Daftar alat

Daftar otoritatif ada di `backend/app/services/registry.py` dan dapat dilihat di
UI atau `GET /api/v1/formats`. Ringkasannya:

### PDF
`pdf-to-word`, `pdf-to-excel`, `pdf-to-pptx`, `pdf-to-images`, `merge-pdf`,
`split-pdf`, `rotate-pdf`, `compress-pdf`, `extract-text`, `extract-tables`,
`add-page-numbers`, `add-watermark`, `remove-pages`, `reorder-pdf`.

### Word
`docx-to-pdf`, `docx-to-txt`, `docx-to-html`, `docx-to-markdown`, `txt-to-docx`,
`markdown-to-docx`, `merge-docx`, `clean-docx`.

### Excel
`xlsx-to-pdf`, `xlsx-to-csv`, `csv-to-xlsx`, `xlsx-to-html`.

### PowerPoint
`pptx-to-pdf`, `pptx-to-images` — hanya tersedia bila LibreOffice terdeteksi.

### Gambar & OCR
`image-to-pdf`, `convert-image`, `image-ocr`, `pdf-ocr-to-word`.

### Teks & Web
`html-to-pdf`, `txt-to-pdf`, `markdown-to-pdf`.

### Strategi konversi penting

**PDF → Word** (`app/services/pdf_to_docx.py`):
- Mendeteksi halaman dengan/ tanpa lapisan teks; OCR otomatis hanya untuk halaman pindai.
- Menggabungkan baris menjadi paragraf, mempertahankan bold/italic/superscript, ukuran huruf.
- Mendeteksi judul berdasarkan ukuran font relatif dan menebalkan sesuai level.
- Mendeteksi daftar berbutir/nomor dan memakai style *List Bullet*/*List Number*.
- Mendeteksi tabel dengan pdfplumber dan menyisipkannya sebagai tabel Word asli.
- Menyalin gambar yang tertanam.
- Menangani tata letak dua kolom dengan urutan baca per kolom dan memberi peringatan.
- Menghapus header/footer berulang agar tidak terduplikasi.
- Selalu menghasilkan DOCX yang dapat diedit — bukan satu gambar per halaman.
- Peringatan hasil (OCR, kolom, tabel kompleks, batas halaman) dikirim ke UI.

**PDF → Word: gambar dan mode**
- Mode `layout` memakai `pdf2docx`, sehingga gambar raster, JPEG2000/CMYK, gambar
  transparan, gambar berulang, grafik vektor, dan gambar di dalam tabel tetap tampil
  dan teks tetap dapat diedit. Mode `editable` menyalin gambar sebagai objek sebaris
  dan menormalkan JPX/CMYK/transparansi ke PNG lewat Pillow + PyMuPDF.
- Halaman tanpa lapisan teks dideteksi terpisah. Mode `layout` untuk PDF pindai
  otomatis dialihkan ke mode edit + OCR; gambar halaman tidak diduplikasi dengan
  teks OCR. Semua gambar yang tidak dapat disalin dilaporkan di `warnings`.

**Word → PDF: renderer referensi**
- Di Windows, DOCX diekspor lewat Microsoft Word Automation di proses terpisah
  (`word_com_helper`) dengan timeout, tanpa `shell=True`, dan Word ditutup setelah
  selesai. Hasilnya mempertahankan page setup, font, header/footer, tabel, gambar,
  dan objek mengambang seperti dokumen aslinya.
- Font yang dipakai DOCX diperiksa terhadap font sistem. Font yang tidak tersedia
  dilaporkan di `warnings` karena Word akan menggantinya dan panjang baris dapat berubah.
- Bila Word gagal/tidak ada, backend memakai LibreOffice headless dengan profil
  pengguna terpisah per pekerjaan; renderer HTML hanya dipakai sebagai pilihan terakhir.

**Ukuran halaman**
- PDF → Word mendeteksi ukuran dan orientasi setiap halaman. Jika ukuran berubah,
  dibuat section Word baru sehingga A4, Letter, F4, A3, lanskap, dan ukuran khusus
  tetap dipertahankan. Margin didekati dari kotak konten, dan gambar diskalakan
  proporsional agar tidak melebihi lebar teks.
- Word → PDF dengan Word atau LibreOffice mempertahankan page setup asli. Pada
  fallback HTML, renderer membaca ukuran kertas, orientasi, dan margin dari section
  DOCX dan menerapkannya sebagai `@page`; perbedaan antar-section dilaporkan.
- Gambar → PDF menawarkan ukuran otomatis (mengikuti gambar), A4/Letter/Legal/A3/F4,
  serta mode sesuaikan/isi/ukuran asli. Rasio aspek tidak pernah diubah; mode isi
  halaman memotong tepi secara proporsional dan memberi peringatan.
- Excel → PDF (fallback) membaca orientasi/paper size dari XML lembar kerja dan
  membagi kolom lebar menjadi blok halaman, bukan memotong sisi kanan.
- PDF → PowerPoint menskalakan setiap halaman secara proporsional ke slide sehingga
  halaman dengan ukuran campuran tidak terpotong.

**Excel → PDF tanpa LibreOffice**: XLSX dikonversi ke tabel HTML lalu dirender
dengan xhtml2pdf. Hasilnya PDF nyata, tetapi font, page break, kolom, chart,
gambar Excel, dan merge cell dapat berbeda; peringatan ditampilkan. DOCX → PDF
memakai Word di Windows, LibreOffice bila tersedia, dan HTML hanya sebagai
pilihan terakhir.

**PDF → Excel** memakai pdfplumber. Bila tidak ada tabel bergaris, baris teks
diekspor sebagai fallback (dapat dimatikan lewat opsi).

---

## Keamanan & retensi

- Validasi ekstensi **dan isi** berkas (magic bytes) serta struktur OOXML.
- Nama penyimpanan `uuid4().hex + ekstensi`; nama pengguna tidak pernah dipakai sebagai path.
- Path unduhan diverifikasi berada di dalam `storage/outputs`.
- Akses pekerjaan: pemilik akun atau cookie sesi tamu (`dc_guest`) dengan UUID acak.
- Kata sandi: bcrypt (cost 12). Token JWT HS256 dengan masa berlaku; dikirim sebagai cookie HttpOnly dan token Bearer.
- Batas ukuran per berkas, jumlah berkas, rate limit per IP, dan timeout konversi.
- Makro/konten aktif tidak dijalankan.
- Isi dokumen tidak ditulis ke log; log hanya metadata pekerjaan.
- Berkas masukan dihapus setelah pekerjaan; hasil dihapus saat kedaluwarsa.
- Unduhan diperiksa ulang terhadap status, waktu kedaluwarsa, dan kepemilikan.

---

## Pengujian

```powershell
cd backend
.\.venv\Scripts\python -m pytest -q
```

Mencakup:

- Health & daftar format, penandaan alat yang tidak tersedia.
- Registrasi/login/logout/profil, validasi input, isolasi pekerjaan antar akun dan sesi tamu.
- PDF → Word (teks, heading, tabel, DOCX dapat dibuka), PDF → Excel (nilai sel), PDF → PPTX, PDF → gambar.
- Word → PDF/TXT/HTML/Markdown, TXT/Markdown → Word, merge DOCX, clean DOCX.
- Excel → CSV (satu & banyak lembar), CSV → Excel, Excel → HTML/PDF.
- Gambar → PDF, konversi JPG, OCR gambar, PDF pindai → Word (OCR).
- Merge/split PDF, rotate, nomor halaman, watermark, kompres, hapus/urutkan halaman, ekstrak teks/tabel.
- Berkas kosong/rusak, ekstensi tidak didukung, isi tidak cocok dengan ekstensi, ukuran melebihi batas.
- PDF 1/8/12 halaman: jumlah halaman, urutan 1..N, dan pratinjau yang dapat diakses; isi semua halaman masuk ke DOCX.
- Delapan gambar sekaligus (nama berkas sama, dimensi/orientasi berbeda): urutan, rasio aspek, nama tidak tertimpa.
- PDF ukuran halaman campuran → Word: jumlah section dan ukuran tiap section.
- Gambar → PDF untuk A4/Letter/Legal/A3/F4: ukuran halaman dan rasio konten.
- Word → PDF mempertahankan ukuran/orientasi kertas dan tidak menambah halaman kosong.
- Excel → PDF lanskap memuat semua kolom (tidak terpotong di kanan).
- PDF pindai (dengan/tanpa OCR), PDF rusak, berkas besar (>3 MB), satu halaman gagal dirender
  (status `failed` dilaporkan tanpa menggagalkan pekerjaan), gambar rusak di antara gambar valid.
- Pratinjau hanya dapat diakses pemilik dan menjadi 410 setelah kedaluwarsa.
- Pekerjaan gagal dilaporkan dengan pesan, unduhan kedaluwarsa (410), penghapusan riwayat.
- Frontend: `npm run typecheck` dan `npm run build`.
- Verifikasi manual UI: konversi PDF→Word dijalankan melalui browser headless, termasuk pratinjau PDF.js dan tombol unduh.

Tes OCR otomatis di-skip bila tidak ada mesin OCR.

---

## Build produksi

Deploy VPS satu perintah (Docker, nginx, Let's Encrypt):

```bash
cp .env.production.template .env.production   # isi semua nilai
./deploy.sh production
```

Detail lengkap ada di `DEPLOYMENT.md`. Untuk menjalankan manual tanpa Docker:

```powershell
cd frontend
$env:NEXT_PUBLIC_API_URL="https://domain-anda.com"   # origin saja, tanpa /api/v1; di-inline saat build
npm run build
npm start          # atau deploy ke hosting Next.js

cd ..\backend
.\.venv\Scripts\python -m alembic upgrade head
.\.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Catatan produksi:

- Ubah `SECRET_KEY`, set `ENVIRONMENT=production`, gunakan HTTPS (cookie `Secure` aktif di mode production).
- Batasi `CORS_ORIGINS` ke domain nyata.
- Gunakan PostgreSQL untuk multi-user.
- Rate limiter in-memory hanya berlaku per proses; gunakan Redis bila memakai banyak worker.
- Pertimbangkan reverse proxy (IIS/Nginx) dengan batas ukuran unggahan dan timeout yang sesuai.
- Backup database dan `STORAGE_DIR`; pastikan kebijakan retensi sesuai kebutuhan.
- Untuk S3, ganti implementasi penyimpanan di `app/core/storage.py` dan worker.

---

## Pemecahan masalah

| Gejala | Penyebab / solusi |
|---|---|
| UI: "Backend tidak dapat dihubungi" | Backend belum jalan, URL API salah, atau CORS. Cek `http://127.0.0.1:8000/api/v1/health` dan `CORS_ORIGINS`. |
| Alat PowerPoint tidak tersedia | LibreOffice tidak terdeteksi. Pasang LibreOffice, restart backend. |
| Alat OCR tidak tersedia | Tidak ada Tesseract/RapidOCR. Jalankan `pip install rapidocr-onnxruntime` di venv backend. |
| DOCX → PDF berbeda dari Word | Periksa `capabilities.microsoft_word.available` di `/health`. Tanpa Word/LibreOffice, backend memakai renderer HTML dan menampilkan peringatan. |
| XLSX → PDF tampak sederhana | LibreOffice tidak ada; backend memakai renderer HTML dan menampilkan peringatan. |
| Font berubah di PDF | Font tidak terpasang di server; `warnings` menyebutkan font yang diganti. Pasang font berlisensi secara sah atau gunakan font yang tersedia. |
| PDF → Word kehilangan tata letak | PDF dengan kolom kompleks/rumus/tabel bergabung memang terbatas; lihat peringatan hasil. |
| Pratinjau halaman tidak muncul | Halaman melewati `PREVIEW_MAX_PAGES`, pratinjau gagal dirender (lihat status/`error` per halaman), atau hasil sudah kedaluwarsa. |
| `alembic` gagal | Periksa `DATABASE_URL`; hapus DB kosong yang korup hanya saat pengembangan. |
| Port 3000 dipakai | Next.js memilih 3001; pastikan origin 3001 ada di `CORS_ORIGINS`. |
| `ModuleNotFoundError` saat tes | Jalankan tes dengan Python dari `.venv`, bukan Python global. |

---

## Dokumentasi API

Lihat [`docs/API.md`](docs/API.md) atau Swagger di `/docs`.

## Lisensi

Kode proyek ini disediakan untuk penggunaan Anda. Pustaka pihak ketiga mengikuti
lisensi masing-masing (PyMuPDF AGPL/komersial, pdf2docx MIT, LibreOffice MPL,
Microsoft Word komersial, pywin32 PSF, Tesseract Apache 2.0, RapidOCR Apache 2.0,
dan lainnya). Tinjau lisensi sebelum distribusi komersial.
