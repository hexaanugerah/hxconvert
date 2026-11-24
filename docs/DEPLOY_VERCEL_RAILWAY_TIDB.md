# Deploy HXconvertX: Vercel + Railway + TiDB Cloud

> **Status deploy saat ini (19 Nov 2026):** frontend live di
> <https://hxconvert.vercel.app>, backend live di
> <https://hxconvert-production.up.railway.app>. Backend berjalan dengan
> **SQLite di volume Railway** (`/data/storage`); TiDB Cloud belum
> dikonfigurasi. Ikuti langkah 1 lalu set `DATABASE_URL` untuk pindah ke TiDB.

Panduan ini memasang stack produksi terpisah:

| Bagian | Platform | Isi |
|---|---|---|
| Frontend | Vercel | Next.js (`frontend/`) |
| Backend | Railway | FastAPI + LibreOffice + Tesseract (`backend/`) |
| Database | TiDB Cloud Starter | MySQL-compatible, metadata job/user |
| File hasil | Railway Volume | `/data/storage` |

> **Penting:** backend memakai proses background dan penyimpanan lokal. Jalankan
> **1 replica** Railway dan pasang volume. Tanpa volume, file input/output hilang
> setiap redeploy dan URL download menjadi 404.

---

## 1. Siapkan TiDB Cloud

1. Masuk ke [TiDB Cloud](https://tidbcloud.com/) dan buat instance
   **Starter** (MySQL compatible) di region terdekat dengan Railway
   (mis. Singapore).
2. Buka instance → **Connect** → **Public** → **Generate Password**.
   Simpan host, port (`4000`), user, dan password.
3. Buat database aplikasi. Dari **SQL Editor** atau `mysql` client:

   ```sql
   CREATE DATABASE IF NOT EXISTS hxconvert
     DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
   ```

4. Bentuk `DATABASE_URL`. Username TiDB Starter memiliki prefix, misalnya
   `3KxAbCdEf.root`. Password harus **URL-encoded**:

   ```bash
   python -c "from urllib.parse import quote; print(quote('PASSWORD_ANDA', safe=''))"
   ```

   ```text
   mysql+pymysql://<prefix>.root:<password-encoded>@<host>:4000/hxconvert
   ```

   Aplikasi juga menerima URL `mysql://...`; otomatis diarahkan ke driver
   PyMySQL.

5. TiDB Cloud mewajibkan TLS. Backend memakai CA bundle sistem Debian
   (`/etc/ssl/certs/ca-certificates.crt`) yang sudah dipasang di image Docker.
   Jangan mematikan TLS.

Jika koneksi dari Railway timeout, buka **Firewall Rules** instance TiDB dan
izinkan akses publik (untuk percobaan: `0.0.0.0/0`; produksi sebaiknya
Private Endpoint atau IP statis Railway Pro).

---

## 2. Deploy backend ke Railway

1. Railway → **New Project** → **Deploy from GitHub repo** → pilih repo ini.
2. Buka service → **Settings → Source**:
   - **Root Directory:** `backend`
   - Builder: **Dockerfile** (Railway mendeteksi `backend/Dockerfile`)
3. **Settings → Volumes** → **Add Volume**:
   - Mount path: `/data`
   - Ukuran: sesuaikan paket (Hobby default 5 GB)
4. **Variables** (Settings → Variables):

   | Variable | Nilai | Wajib |
   |---|---|---|
   | `ENVIRONMENT` | `production` | ya |
   | `SECRET_KEY` | hasil `python -c "import secrets; print(secrets.token_urlsafe(48))"` | ya |
   | `DATABASE_URL` | `mysql+pymysql://...` dari langkah 1 | ya |
   | `DATABASE_SSL` | `true` | ya |
   | `DATABASE_SSL_CA` | `/etc/ssl/certs/ca-certificates.crt` | ya |
   | `DATABASE_SSL_VERIFY_IDENTITY` | `true` | ya |
   | `STORAGE_DIR` | `/data/storage` | ya |
   | `RAILWAY_RUN_UID` | `0` | ya untuk volume |
   | `UVICORN_WORKERS` | `1` | ya |
   | `CONVERSION_WORKERS` | `2` (1 vCPU) / `4` (2+ vCPU); set explicitly because `os.cpu_count()` may report host cores | ya |
   | `ALLOW_ANONYMOUS` | `true` atau `false` | ya |
   | `CORS_ORIGINS` | `https://<domain-vercel>` (boleh beberapa, pisah koma) | ya |
   | `COOKIE_SAMESITE` | `lax` untuk mode proxy/custom domain; `none` untuk direct lintas situs | disarankan |
   | `MAX_UPLOAD_MB` | mis. `100` | tidak |
   | `MAX_FILES_PER_JOB` | mis. `50` | tidak |
   | `RESULT_TTL_HOURS` | `24` | tidak |

   `RAILWAY_RUN_UID=0` diperlukan karena image berjalan sebagai user non-root
   `app`, sedangkan volume Railway dimount sebagai root.

5. **Settings → Networking** → **Generate Domain**. Catat URL
   `https://<service>.up.railway.app`.
6. **Settings → Deploy**:
   - Healthcheck Path: `/api/v1/health`
   - Healthcheck Timeout: `300` (build pertama dan migrasi bisa lama)
   - Restart Policy: `ON_FAILURE`
7. Deploy. Container menjalankan `alembic upgrade head` lalu Uvicorn pada
   `$PORT`. Cek log sampai muncul `HXconvertX API started`.
8. Verifikasi:

   ```bash
   curl https://<service>.up.railway.app/api/v1/health
   # {"status":"ok", ..., "database":"ok"}
   ```

Catatan kapasitas:

- Image memuat LibreOffice + Tesseract + OCR, jadi build pertama bisa
  10–20 menit dan RAM idle cukup besar. Minimal 2 GB RAM; 4 GB lebih aman.
- `UVICORN_WORKERS=1` menghindari dua process pool konversi. Paralelisme
  diatur `CONVERSION_WORKERS`.
- `os.cpu_count()` di container bisa melaporkan core host, bukan kuota
  container. Karena itu set `CONVERSION_WORKERS` eksplisit.
- Jangan menaikkan replica: volume dan job background terikat ke satu proses.

---

## 3. Deploy frontend ke Vercel

1. Vercel → **Add New → Project** → import repo yang sama.
2. **Root Directory:** `frontend`
   Framework: **Next.js** (otomatis). Install/Build command biarkan default
   (`npm ci`, `npm run build`).
3. **Environment Variables** untuk Production (dan Preview bila perlu):

   **Mode A — proxy same-origin (disarankan tanpa domain sendiri):**

   | Variable | Nilai |
   |---|---|
   | `BACKEND_URL` | `https://<service>.up.railway.app` |
   | `NEXT_PUBLIC_API_URL` | *kosongkan / jangan dibuat* |

   Browser memanggil `/api/v1/...` di domain Vercel; `next.config.ts`
   mem-proxy ke Railway. Cookie guest/login tetap first-party, CORS tidak
   dipakai, dan `COOKIE_SAMESITE=lax` aman.

   **Mode B — direct ke Railway (upload besar / domain sendiri):**

   | Variable | Nilai |
   |---|---|
   | `NEXT_PUBLIC_API_URL` | `https://<service>.up.railway.app` atau `https://api.domain.com` |

   Set `CORS_ORIGINS` di Railway ke origin Vercel. Jika origin browser dan API
   berbeda **site** (mis. `*.vercel.app` → `*.up.railway.app`), set
   `COOKIE_SAMESITE=none`. Safari/Firefox dapat memblokir cookie pihak ketiga;
   untuk produksi serius gunakan Mode C.

   **Mode C — custom domain satu apex (paling stabil):**

   - `app.domain.com` → Vercel
   - `api.domain.com` → Railway
   - `NEXT_PUBLIC_API_URL=https://api.domain.com`
   - `CORS_ORIGINS=https://app.domain.com`
   - `COOKIE_SAMESITE=lax` (masih same-site)

4. Deploy. Vercel membekukan `NEXT_PUBLIC_API_URL` dan `BACKEND_URL` saat
   **build**; setelah mengubah nilainya lakukan **Redeploy**.
5. Verifikasi dari domain Vercel:

   ```bash
   curl https://<project>.vercel.app/api/v1/health
   ```

   Jika Mode A berhasil, endpoint health di domain Vercel mengembalikan JSON
   backend.

---

## 4. Uji alur utama

1. Buka domain Vercel.
2. Konversi satu file kecil, tunggu status **Selesai**, lalu download.
3. Konversi 15 file sekaligus untuk memastikan batch + ZIP bekerja.
4. Cek di Railway:
   - **Logs** menampilkan `stage=batch files=15 ok=15 failed=0`.
   - **Volume** `/data/storage/outputs` berisi hasil.
5. Register/login (jika `ALLOW_ANONYMOUS=false`) dan pastikan cookie sesi
   bertahan setelah refresh.

---

## 5. Troubleshooting

| Gejala | Penyebab / solusi |
|---|---|
| Health `database: error` | `DATABASE_URL` salah, password belum URL-encoded, database belum dibuat, atau TLS mati. Cek log Alembic. |
| `Access denied` / `Unknown database` | Username harus berprefix TiDB; buat database `hxconvert` lebih dulu. |
| `SSL connection error` | Set `DATABASE_SSL=true`; pastikan `ca-certificates` ada di image (sudah default). |
| Download 404 setelah redeploy | Volume belum dipasang atau `STORAGE_DIR` bukan `/data/storage`. |
| `Permission denied` di `/data/storage` | Set `RAILWAY_RUN_UID=0`, lalu redeploy. |
| CORS error di Mode B | `CORS_ORIGINS` harus origin persis (skema + host, tanpa slash), dan tidak boleh `*` karena credentials. |
| Guest job hilang / polling 401 | Cookie lintas situs diblokir. Pakai Mode A/C, atau `COOKIE_SAMESITE=none` + HTTPS. |
| Upload 413 | Naikkan `MAX_UPLOAD_MB`/`MAX_BATCH_SIZE_MB`, atau gunakan Mode B/C agar file tidak melewati proxy Vercel. |
| Vercel 504 saat upload besar | Proxy Vercel membatasi durasi/ukuran. Gunakan Mode B/C. |
| Build Railway gagal di `COPY requirements.txt` | Root Directory layanan belum diset `backend`. |
| `Free plan resource provision limit exceeded` | Railway Free menolak project/service/volume baru. Upgrade ke Hobby, hapus resource yang tidak dipakai, atau tambahkan service ke project yang sudah ada. |
| Container restart terus | Cek `alembic upgrade head` di log; healthcheck butuh database siap. |

---

## 6. Perintah CLI (opsional)

Railway CLI (sudah login di mesin pengembangan):

```bash
railway login
railway link                # pilih project + service backend
railway up                  # deploy dari direktori backend
railway logs                # log runtime
railway variables           # cek env
railway run alembic upgrade head
```

Vercel CLI:

```bash
vercel login
cd frontend
vercel link                 # pilih project
vercel env add BACKEND_URL production
vercel --prod
```

Migrasi manual (biasanya otomatis saat container start):

```bash
railway run alembic upgrade head
```

---

## 7. Checklist keamanan

- [ ] `SECRET_KEY` acak, bukan nilai default.
- [ ] `DATABASE_SSL=true` dan verifikasi identitas aktif.
- [ ] `CORS_ORIGINS` berisi origin nyata, bukan `*`.
- [ ] `ALLOW_ANONYMOUS` sesuai kebutuhan.
- [ ] Volume Railway terpasang sebelum menerima trafik.
- [ ] `RESULT_TTL_HOURS` dan `CLEANUP_INTERVAL_MINUTES` sesuai kapasitas volume.
- [ ] Rate limit disesuaikan (`RATE_LIMIT_PER_MINUTE`).
