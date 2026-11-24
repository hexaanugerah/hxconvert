# HXconvertX API

Base URL pengembangan: `http://127.0.0.1:8000`

Semua endpoint berada di bawah prefix `/api/v1`. Swagger interaktif tersedia di
`/docs`, dan skema OpenAPI di `/openapi.json`.

---

## Konvensi umum

### Format respons

Respons sukses berupa JSON (kecuali unduhan berkas dan `204 No Content`).
Kesalahan selalu berbentuk:

```json
{
  "error": {
    "code": "validation_error",
    "message": "The request data is invalid.",
    "details": [{ "field": "body.email", "message": "value is not a valid email address" }]
  }
}
```

Kode kesalahan yang umum:

| HTTP | `code` | Arti |
|---|---|---|
| 400 | `bad_request` | Permintaan tidak valid secara umum. |
| 401 | `unauthorized` | Belum login atau token tidak valid. |
| 403 | `forbidden` | Hasil belum siap atau akses ditolak. |
| 404 | `not_found` | Pekerjaan/alat tidak ditemukan (termasuk milik pengguna lain). |
| 409 | `conflict` | Email sudah terdaftar. |
| 410 | `expired` | Tautan unduhan sudah kedaluwarsa. |
| 413 | `file_too_large` | Melebihi `MAX_UPLOAD_MB`. |
| 415 | `unsupported_format` | Ekstensi/isi berkas tidak didukung alat. |
| 422 | `validation_error` / `conversion_failed` | Data tidak valid atau konversi gagal. |
| 429 | `rate_limited` | Terlalu banyak permintaan. |
| 500 | `internal_error` | Kesalahan tak terduga (detail tidak dibocorkan). |

### Autentikasi

Dua mekanisme yang didukung bersamaan:

1. **Cookie sesi** — `dc_access` (HttpOnly, SameSite=Lax) di-set saat login/register.
2. **Bearer token** — nilai `access_token` dari respons login/register:

```http
Authorization: Bearer <token>
```

Pengguna anonim mendapat cookie `dc_guest` saat pertama kali membuat pekerjaan.
Pekerjaan anonim hanya dapat diakses oleh cookie tersebut.

### Batas dan kebijakan

Nilai aktual dapat dibaca dari `GET /api/v1/formats` (`capabilities.limits`) dan
`GET /api/v1/conversions/stats` (`usage`).

- Ukuran maksimum per berkas: default 25 MB.
- Maksimum berkas per pekerjaan: default 10 (beberapa alat hanya menerima 1).
- Masa simpan hasil: default 24 jam.
- Rate limit: default 60 permintaan/menit per IP (in-memory per proses).
- Timeout konversi: default 180 detik.
- Kuota harian per identitas (jendela bergulir 24 jam, dihitung dari database):
  default 100 pekerjaan untuk pengguna masuk dan 20 untuk tamu, ditambah batas
  total ukuran unggahan 2048 MB per 24 jam (`DAILY_JOB_QUOTA`,
  `GUEST_DAILY_JOB_QUOTA`, `DAILY_MEGABYTE_QUOTA`). Setel `0` untuk mematikan
  batas tersebut. Pelanggaran menghasilkan `429 rate_limited` dan pesan berisi
  sisa kebijakan; tamu diarahkan untuk masuk agar mendapat kuota lebih besar.
- `GET /api/v1/metrics` dilindungi `METRICS_TOKEN` bila variabel itu diisi.
- Banding visual: `COMPARE_DPI` (default 100) dan `COMPARE_MAX_PAGES` (default 25)
membatasi resolusi serta jumlah halaman yang dirender.

### Status pekerjaan

```text
queued ──▶ processing ──▶ completed
   │                └───▶ failed
   └──(dibatalkan)──▶ cancelled ──(retry)──▶ queued
failed ──(retry)──▶ queued
completed ──(kedaluwarsa)──▶ expired
```

Frontend melakukan polling `GET /conversions/{id}` sampai status terminal
(`completed`, `failed`, `cancelled`, `expired`). Tidak ada perhitungan
persentase karena backend tidak melaporkan progres per tahap. Unggahan disimpan
sampai hasil kedaluwarsa: pekerjaan yang gagal atau dibatalkan dapat dijalankan
ulang lewat `POST /conversions/{id}/retry` tanpa unggah baru, dan halaman banding
visual merender berkas asli terhadap hasilnya. Saat kedaluwarsa, seluruh folder
pekerjaan (input, hasil, pratinjau, dan gambar perbandingan) dihapus.

### Idempotensi dan format unggahan

Endpoint pembuatan pekerjaan memakai `multipart/form-data`:

- `tool` — ID alat (lihat daftar di bawah).
- `options` — string JSON berisi opsi (opsional, default `{}`).
- `files` — satu atau lebih bagian berkas dengan nama field `files`.

---

## Health

### `GET /api/v1/health`

```json
{
  "status": "ok",
  "version": "1.0.0",
  "database": "ok",
  "capabilities": {
    "libreoffice": { "available": false, "path": null, "used_for": ["..."] },
    "microsoft_word": {
      "available": true,
      "path": "C:\\Program Files\\Microsoft Office\\root\\Office16\\WINWORD.EXE",
      "used_for": ["docx -> PDF (highest fidelity, reference renderer)"]
    },
    "ocr": { "available": true, "engine": "rapidocr", "languages": ["eng", "ind"] },
    "libraries": { "PyMuPDF": "1.28.2", "pdfplumber": "0.11.9" },
    "limits": { "max_upload_mb": 25, "max_files_per_job": 50, "result_ttl_hours": 24 }
  }
}
```

`status` bernilai `degraded` bila koneksi database gagal.

---

## Format dan alat

### `GET /api/v1/formats`

Mengembalikan:

```json
{
  "tools": [
    {
      "id": "pdf-to-word",
      "label": "PDF ke Word",
      "description": "Ekstrak teks, tabel, dan gambar PDF menjadi dokumen DOCX yang dapat diedit.",
      "category": "pdf",
      "inputs": [".pdf"],
      "output": "docx",
      "available": true,
      "unavailable_reason": null,
      "multiple": false,
      "min_files": 1,
      "max_files": 1,
      "processing_note": "Tata letak kompleks ...",
      "options": [
        {
          "name": "mode",
          "label": "Mode konversi",
          "type": "select",
          "default": "layout",
          "choices": [
            { "value": "layout", "label": "Prioritaskan tata letak" },
            { "value": "editable", "label": "Prioritaskan pengeditan" }
          ],
          "help": null,
          "placeholder": null,
          "min": null,
          "max": null
        },
        {
          "name": "ocr",
          "label": "Deteksi hasil pindai (OCR)",
          "type": "select",
          "default": "auto",
          "choices": [
            { "value": "auto", "label": "Otomatis" },
            { "value": "always", "label": "Selalu gunakan OCR" },
            { "value": "never", "label": "Jangan gunakan OCR" }
          ],
          "help": "Mode otomatis menggunakan OCR hanya untuk halaman tanpa teks.",
          "placeholder": null,
          "min": null,
          "max": null
        }
      ]
    }
  ],
  "categories": [{ "id": "pdf", "label": "PDF" }],
  "formats": { "input": ["pdf", "docx"], "output": ["docx", "pdf"] },
  "capabilities": { "...": "sama seperti health" }
}
```

Alat dengan `available: false` **tidak boleh** ditampilkan sebagai alat yang bisa
dipakai. Alasannya ada di `unavailable_reason`.

### ID alat

PDF: `pdf-to-word`, `pdf-to-excel`, `pdf-to-pptx`, `pdf-to-images`, `merge-pdf`,
`split-pdf`, `rotate-pdf`, `compress-pdf`, `extract-text`, `extract-tables`,
`add-page-numbers`, `add-watermark`, `remove-pages`, `reorder-pdf`.

Word: `docx-to-pdf`, `docx-to-txt`, `docx-to-html`, `docx-to-markdown`,
`txt-to-docx`, `markdown-to-docx`, `merge-docx`, `clean-docx`.

Excel: `xlsx-to-pdf`, `xlsx-to-csv`, `csv-to-xlsx`, `xlsx-to-html`.

PowerPoint: `pptx-to-pdf`, `pptx-to-images` (memerlukan LibreOffice).

Gambar & OCR: `image-to-pdf`, `convert-image`, `image-ocr`, `pdf-ocr-to-word`.

Teks & Web: `html-to-pdf`, `txt-to-pdf`, `markdown-to-pdf`.

Opsi yang tidak dideklarasikan alat akan diabaikan. Nilai select di luar pilihan
jatuh ke default; angka di-clamp ke `min`/`max`.

---

## Autentikasi

### `POST /api/v1/auth/register`

```json
{ "name": "Ani", "email": "ani@example.com", "password": "Password123" }
```

Respons `201`:

```json
{
  "user": { "id": "uuid", "name": "Ani", "email": "ani@example.com", "created_at": "..." },
  "access_token": "<jwt>",
  "token_type": "bearer"
}
```

Pekerjaan tamu pada peramban yang sama otomatis dipindahkan ke akun baru.

Aturan kata sandi: 8–128 karakter, mengandung huruf dan angka, tanpa spasi di awal/akhir.

### `POST /api/v1/auth/login`

```json
{ "email": "ani@example.com", "password": "Password123" }
```

Respons `200` sama seperti register. Kredensial salah menghasilkan `401 unauthorized`.

### `POST /api/v1/auth/logout`

`204 No Content`; menghapus cookie `dc_access`.

### `GET /api/v1/auth/me`

Memerlukan autentikasi. Mengembalikan objek `user`.

### `PATCH /api/v1/auth/me`

```json
{ "name": "Ani Baru", "email": "ani.baru@example.com" }
```

Kedua field opsional. Email duplikat → `409`.

### `POST /api/v1/auth/change-password`

```json
{ "current_password": "Password123", "new_password": "Password456" }
```

`204 No Content`. Kata sandi saat ini salah → `401`.

### `GET /api/v1/auth/session`

Endpoint publik untuk hidrasi frontend:

```json
{ "authenticated": true, "user": { "...": "..." }, "anonymous_allowed": true }
```

---

## Pekerjaan konversi

### `POST /api/v1/conversions`

Membuat pekerjaan. Rate-limited.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/conversions \
  -F "tool=pdf-to-word" \
  -F 'options={"mode":"layout","ocr":"auto","language":"eng"}' \
  -F "files=@report.pdf"
```

Respons `202 Accepted` (status awal `queued`; pemrosesan berjalan setelah respons):

```json
{
  "id": "4e87...",
  "tool": "pdf-to-word",
  "original_filename": "report.pdf",
  "input_format": "pdf",
  "output_format": "docx",
  "status": "queued",
  "file_size": 2382,
  "output_filename": null,
  "output_size": null,
  "error_message": null,
  "warnings": [],
  "options": { "mode": "layout", "ocr": "auto", "language": "eng", "page_breaks": false, "tables": true },
  "created_at": "2026-10-22T12:59:55",
  "started_at": null,
  "completed_at": null,
  "expires_at": "2026-10-23T12:59:55",
  "download_url": null,
  "page_count": null,
  "pages": []
}
```

Setelah pekerjaan selesai, `page_count` dan `pages` berisi hasil pemeriksaan nyata.
Contoh PDF 8 halaman:

```json
{
  "page_count": 8,
  "pages": [
    {
      "page_number": 1,
      "status": "completed",
      "preview_url": "/api/v1/conversions/<job_id>/pages/1/preview",
      "width_pt": 595.0,
      "height_pt": 842.0,
      "orientation": "portrait",
      "pixel_width": 793,
      "pixel_height": 1123,
      "dpi": 96.0
    }
  ]
}
```

- Urutan `pages` selalu naik dari 1 sampai `page_count`.
- `status` bernilai `completed` (pratinjau tersedia), `pending` (melewati
  `PREVIEW_MAX_PAGES`), atau `failed` (disertai `error`).
- `preview_url` bernilai `null` bila tidak ada berkas pratinjau yang benar-benar
  tersedia. Tidak ada URL yang dibuat tanpa berkas.
- Untuk keluaran yang tidak berhalaman (mis. DOCX → TXT), `page_count` bernilai
  `null` dan `pages` kosong.

Kemungkinan error: `413`, `415`, `422` (opsi/JSON atau jumlah berkas), `429`.

#### Mode `pdf-to-word`

| `mode` | Perilaku |
|---|---|
| `auto` (default) | Klasifikasi per halaman: halaman pindai jadi gambar, halaman teks jadi teks edit, halaman campuran memakai `pdf2docx`. Dokumen desain/slide (teks menyatu dengan gambar atau bidang warna, mis. ekspor Canva/Figma) otomatis dirender sebagai gambar penuh agar tampilannya sama persis dengan PDF. |
| `fast` | Setiap halaman dirender sebagai **JPEG** pada `fast_dpi` (default 72) tanpa analisis tata letak. Paling cepat untuk batch besar: terukur 4,5 detik untuk 20 PDF 12 halaman berfoto, dibanding 26,8 detik pada mode `auto`. Teks tidak dapat diedit dan hasilnya sekualitas pratinjau layar; naikkan `fast_dpi` (72–200) bila perlu lebih tajam. |
| `layout` | Memakai `pdf2docx`: ukuran halaman, posisi teks, tabel, gambar raster, dan grafik vektor dipertahankan. Gambar JPEG2000/CMYK dinormalisasi. Teks tetap dapat diedit, tetapi posisinya berada dalam kotak mengambang. |
| `editable` | Alur Word sebaris (heading, paragraf, tabel asli, gambar sebaris, section per ukuran halaman). Lebih mudah diedit, posisi visual tidak persis. |
| `full-image` | "Seperti foto": setiap halaman dirender sebagai PNG 300 DPI. Paling setia secara visual, tanpa OCR; teks tidak dapat diedit. |

Jika mode `layout` diminta untuk PDF yang seluruhnya hasil pindai, backend otomatis memakai mode edit + OCR (bila OCR tersedia) dan mengirim peringatan. Peringatan pada `warnings` selalu menyebut gambar, font, atau halaman yang tidak dapat dipertahankan persis; tidak ada kehilangan yang disembunyikan.

Mode `editable` juga memakai jenis huruf asli PDF: setiap run diberi nama keluarga font dari PDF (bila font itu dapat disematkan) dan font tersebut disematkan ke DOCX, sehingga tipografi tetap sama di komputer yang tidak memiliki font tersebut. Run yang fontnya tidak dapat disematkan memakai font bawaan Word, dan hal itu dilaporkan di `warnings`.

Di antarmuka web, alat `pdf-to-word` sekarang **hanya memakai `auto`** dan tidak menampilkan pemilih mode. Opsi `mode`, `fast_dpi`, `ocr`, `language`, `page_breaks`, dan `tables` tetap diterima API untuk pemanggil lanjutan, tetapi disembunyikan dari UI agar pengguna cukup mengunggah PDF lalu mengonversi. Hasil `auto` bersifat editable untuk PDF berteks, dan menjadi gambar per halaman hanya bila halaman memang tampak seperti gambar (scan/foto/desain).

Trade-off kecepatan, ketajaman, dan ukuran hasil (diukur dengan `backend/benchmark_large_batch.py` pada mesin 8 core, PDF 12 halaman berfoto 1,7 MB; satu pekerjaan dibatasi 50 berkas):

| `mode` | 20 berkas | 50 berkas | Ukuran DOCX hasil |
|---|---|---|---|
| `fast` (72 DPI) | 4,45–4,71 dtk | 7,42–7,62 dtk | ±0,6 MB/berkas |
| `auto` | 33,8–38,6 dtk | — | ±13 MB/berkas |
| `full-image` (300 DPI PNG) | — | 84–104 dtk (ZIP 12–26 dtk) | ±21 MB/berkas (ZIP 1,07 GB) |

`fast` adalah satu-satunya mode yang berada di bawah 5 detik untuk 20 PDF bergambar, dengan konsekuensi teks tidak dapat diedit dan ketajaman setara pratinjau layar. `full-image` mempertahankan ketajaman cetak, tetapi 50 berkas menghasilkan arsip 1,07 GB sehingga pengemasan ZIP menjadi bottleneck (12–26 dtk), bukan konversinya.

Batas ZIP batch: arsip tetap dibuat dan pengguna diberi peringatan di `warnings` bila total hasil melebihi 250 MB; arsip tidak dibuat sama sekali bila melebihi 2048 MB, dan setiap berkas tetap dapat diunduh sendiri melalui `batch_results[].download_url`.

#### Word ke PDF

`docx-to-pdf` memilih renderer lewat opsi `engine`. **Konversi tunggal dan batch memakai urutan mesin yang sama**, jadi satu dokumen tidak berubah tampilan hanya karena diunggah bersama berkas lain:

| `engine` | Perilaku |
|---|---|
| `auto` (default) | **Microsoft Word** bila tersedia (paling mirip Word), lalu **LibreOffice headless**, lalu renderer Python. Setiap perpindahan mesin dicatat di `warnings`. |
| `libreoffice` | Hanya LibreOffice; gagal dengan pesan jelas bila tidak terpasang. |
| `python` | Hanya renderer Python: mempertahankan ukuran halaman, margin, paragraf, tabel, gambar, dan pemisah halaman eksplisit; header/footer, kolom, dan objek mengambang kompleks dapat berbeda. |
| `word` | Hanya Microsoft Word Automation (`WINWORD.EXE` + `pywin32`, Windows). Paling mirip aslinya, termasuk header/footer dan font. Font yang tidak terpasang dilaporkan di `warnings`. |

Peringatan font yang sama (Word maupun LibreOffice) kini muncul untuk hasil batch seperti pada konversi tunggal. Trade-off kecepatan: pada 20 DOCX 6 bagian bergambar, `auto` di mesin tanpa Word memakai 9,3–11,2 detik, sedangkan melalui Word COM 22,5–28,5 detik — kualitas lebih tinggi, waktu lebih lama.

### `GET /api/v1/conversions`

Riwayat milik identitas saat ini.

Query:

| Parameter | Default | Keterangan |
|---|---|---|
| `page` | 1 | Halaman (≥ 1). |
| `page_size` | 20 | 1–100. |
| `status` | — | `queued`, `processing`, `completed`, `failed`, `cancelled`, `expired`. |
| `tool` | — | ID alat. |
| `q` | — | Pencarian nama berkas/alat (case-insensitive). |

Respons:

```json
{ "items": [{ "...": "..." }], "total": 12, "page": 1, "page_size": 20 }
```

### `GET /api/v1/conversions/stats`

```json
{
  "total": 12,
  "completed": 9,
  "failed": 1,
  "processing": 2,
  "success_rate": 90.0,
  "processed_bytes": 412000,
  "recent": [{ "...": "5 pekerjaan terakhir" }],
  "usage": {
    "max_upload_mb": 25,
    "max_files_per_job": 50,
    "result_ttl_hours": 24,
    "allow_anonymous": true,
    "daily_job_quota": 100,
    "guest_daily_job_quota": 20,
    "daily_megabyte_quota": 2048
  },
  "used_jobs_24h": 3,
  "used_megabytes_24h": 12.4
}
```

`used_jobs_24h` dan `used_megabytes_24h` menghitung pekerjaan identitas pemanggil
dalam 24 jam terakhir (dasar yang sama dengan penegakan kuota).

### `GET /api/v1/conversions/{job_id}`

Detail status terkini. `404` bila tidak ada atau bukan milik pemanggil.

### `GET /api/v1/conversions/{job_id}/download`

Mengunduh hasil sebagai lampiran (`Content-Disposition`).

- `200` — hasil tersedia.
- `404` — tidak ditemukan/bukan milik Anda.
- `410` — kedaluwarsa (berkas dihapus).
- `422` — pekerjaan gagal (berisi pesan kegagalan).
- `403` — hasil belum siap.

### `POST /api/v1/conversions/{job_id}/cancel`

Meminta pembatalan. Pekerjaan `queued` langsung menjadi `cancelled`; pekerjaan
`processing` tetap berjalan sampai engine selesai, lalu hasilnya dibuang dan
status menjadi `cancelled` (pembatalan bersifat kooperatif). Berkas sumber tetap
disimpan agar bisa diulang.

- `200` — status terbaru (`cancelled` atau `processing` dengan
  `cancel_requested: true`).
- `404` — tidak ditemukan/bukan milik Anda.
- `422` — status sudah terminal (`completed`, `failed`, `cancelled`, `expired`).

### `POST /api/v1/conversions/{job_id}/retry`

Menjalankan ulang pekerjaan `failed` atau `cancelled` memakai berkas sumber yang
tersimpan, tanpa unggah ulang. Status pekerjaan yang sama direset ke `queued`
(keluaran, peringatan, pesan galat, dan jumlah halaman dibersihkan) dan
`expires_at` diperbarui.

- `202` — pekerjaan diantrekan kembali.
- `404` — tidak ditemukan/bukan milik Anda.
- `422` — status tidak dapat diulang, atau berkas sumber sudah dihapus karena
  hasilnya kedaluwarsa (unggah ulang diperlukan).

### `GET /api/v1/conversions/{job_id}/pages/{page_number}/preview`

Mengembalikan PNG pratinjau halaman yang benar-benar dirender saat pemrosesan.

- `200` — `image/png`.
- `404` — pekerjaan bukan milik pemanggil, nomor halaman tidak ada, atau halaman
  tersebut tidak memiliki pratinjau (mis. melewati batas `PREVIEW_MAX_PAGES`).
- `410` — hasil kedaluwarsa dan berkas sudah dihapus.

Akses memakai aturan kepemilikan yang sama dengan unduhan. Pratinjau memakai DPI
terpisah (`PREVIEW_DPI`, default 96) sehingga lebih ringan; berkas unduhan tetap
mengikuti opsi kualitas konversi.

### `GET /api/v1/conversions/{job_id}/compare`

Banding visual halaman asli terhadap hasil konversi. Kedua sisi dirender menjadi
PDF pada DPI yang sama (`COMPARE_DPI`, default 100), dinormalisasi, lalu
dibandingkan piksel per piksel (selisih kecil akibat anti-aliasing diabaikan,
perpindahan beberapa piksel dilaporkan sebagai offset). Hanya
`COMPARE_MAX_PAGES` halaman pertama (default 25) yang dibandingkan.

Perbandingan dihitung saat pertama kali diminta lalu disimpan di folder pekerjaan,
sehingga permintaan berikutnya tidak merender ulang. Objek pekerjaan memuat
`compare_url` bila perbandingan memungkinkan (hasil `pdf`, `docx`, `xlsx`, atau
`pptx`, satu berkas sumber, belum kedaluwarsa).

```json
{
  "available": true,
  "reason": null,
  "dpi": 100,
  "page_count": 2,
  "original_page_count": 2,
  "result_page_count": 2,
  "original_name": "laporan.pdf",
  "result_name": "laporan.docx",
  "average_ratio": 0.0103,
  "identical_pages": 0,
  "changed_pages": [1, 2],
  "truncated": false,
  "max_pages": 25,
  "only_original": [],
  "only_result": [],
  "pages": [
    {
      "page": 1,
      "ratio": 0.0095,
      "mean_difference": 7.1,
      "text_similarity": 0.9968,
      "status": "Mirip",
      "offset": [0, 1],
      "original_url": "/api/v1/conversions/{id}/compare/pages/1/original",
      "result_url": "/api/v1/conversions/{id}/compare/pages/1/result",
      "difference_url": "/api/v1/conversions/{id}/compare/pages/1/difference"
    }
  ]
}
```

`status` bernilai `Sama` (< 0,1% piksel berubah), `Mirip` (< 1%), atau
`Berbeda`. Bila pekerjaan tidak dapat dibandingkan (hasil berupa teks/CSV, batch
ZIP, berkas asli hilang, atau hasil belum selesai/metode render tidak tersedia),
respons tetap `200` dengan `available: false` dan `reason` berisi alasannya.

- `404` — tidak ditemukan/bukan milik Anda.
- `410` — hasil kedaluwarsa dan berkas sudah dihapus.

### `GET /api/v1/conversions/{job_id}/compare/pages/{page_number}/{kind}`

Menyajikan satu gambar perbandingan (`kind`: `original`, `result`, atau
`difference`) sebagai `image/png`. `404` bila halaman atau jenis gambar tersebut
tidak ada. Perbandingan harus diminta lebih dulu lewat endpoint di atas.

### `GET /api/v1/metrics`

Metrik format teks Prometheus (`text/plain; version=0.0.4`), tidak memakai
`prometheus_client`. Isinya:

- `hx_database_up` — `1` bila database menjawab permintaan scrape.
- `hx_conversion_jobs{status}` — jumlah pekerjaan tersimpan per status (dari DB).
- `hx_conversion_jobs_active` — pekerjaan `queued` + `processing`.
- `hx_conversion_jobs_total{tool}` — jumlah pekerjaan per alat (dari DB).
- `hx_job_outcomes_total{outcome}` — hasil pekerjaan yang dicatat proses ini.
- `hx_stage_duration_seconds_total{stage}` dan `hx_stage_runs_total{stage}` —
  waktu dan jumlah eksekusi tiap tahap pada proses ini.

Metrik proses (outcome/tahap) bersifat per proses dan reset saat restart;
pekerjaan yang berjalan di proses worker terpisah mencatat metriknya sendiri.
Bila `METRICS_TOKEN` diisi, sertakan header `X-Metrics-Token: <token>` (atau
`Authorization: Bearer <token>`); tanpa token yang benar respons `403`.

### `DELETE /api/v1/conversions/{job_id}`

`204 No Content`. Menghapus pekerjaan, log terkait, berkas masukan, dan hasil.

### `DELETE /api/v1/conversions`

Menghapus seluruh riwayat milik identitas saat ini.

```json
{ "deleted": 12 }
```

---

## Endpoint PDF khusus

Endpoint berikut adalah pembungkus tipis di atas pipeline pekerjaan yang sama.
Semuanya mengembalikan `202` dengan objek pekerjaan dan menerima
`multipart/form-data`.

### `POST /api/v1/pdf/merge`

Field: `files` (2–10 PDF).

### `POST /api/v1/pdf/split`

Field: `files` (1 PDF), `options` (JSON):

```json
{ "mode": "each" }
{ "mode": "ranges", "ranges": "1-3,4-6" }
```

Hasil berupa ZIP.

### `POST /api/v1/pdf/compress`

Field: `files` (1 PDF). Hasil PDF yang dioptimalkan; bila sudah padat, respons
berisi peringatan bahwa ukuran tidak mengecil.

---

## OCR

### `POST /api/v1/ocr`

Field:

- `files` — tepat satu gambar atau PDF.
- `output_format` — `txt` (default) atau `docx`.
- `language` — `eng`, `ind`, atau `eng+ind` (default `eng`).

Untuk gambar alatnya `image-ocr` (hasil TXT). Untuk PDF:
- `txt` → `extract-text` (OCR hanya pada halaman tanpa lapisan teks),
- `docx` → `pdf-ocr-to-word` (OCR semua halaman).

Bila tidak ada mesin OCR, endpoint mengembalikan `415 unsupported_format` dengan
alasan bahwa OCR tidak tersedia.

---

## Contoh alur klien

```ts
// 1. Buat pekerjaan
const form = new FormData();
form.append("tool", "pdf-to-word");
form.append("options", JSON.stringify({ ocr: "auto", language: "eng" }));
form.append("files", file);
const job = await fetch(`${API}/api/v1/conversions`, {
  method: "POST",
  body: form,
  credentials: "include",
}).then((r) => r.json());

// 2. Polling sampai status terminal
let current = job;
while (current.status === "queued" || current.status === "processing") {
  await new Promise((resolve) => setTimeout(resolve, 1500));
  current = await fetch(`${API}/api/v1/conversions/${job.id}`, {
    credentials: "include",
  }).then((r) => r.json());
}

// 3. Unduh hasil
if (current.status === "completed") {
  const blob = await fetch(`${API}${current.download_url}`, { credentials: "include" }).then((r) => r.blob());
}
```

---

## CORS

Origin yang diizinkan diambil dari `CORS_ORIGINS` (dipisah koma). Default
mengizinkan `http://localhost:3000`, `http://127.0.0.1:3000`,
`http://localhost:3001`, dan `http://127.0.0.1:3001`. Kredensial diizinkan
(`Access-Control-Allow-Credentials: true`), sehingga wildcard origin tidak dapat
dipakai.
