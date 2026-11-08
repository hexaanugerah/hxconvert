# Deploy Oracle Cloud Always Free (ARM)

Panduan langkah demi langkah menjalankan stack Docker Compose HXconvertX di VM
gratis Oracle Cloud Infrastructure (OCI) Always Free. Cocok untuk ujicoba jangka
panjang: LibreOffice, Tesseract, PostgreSQL, dan penyimpanan lokal tetap tersedia.

> **Ringkasan free tier (per November 2026):** satu VM `VM.Standard.A1.Flex` Arm
> dengan total **2 OCPU dan 12 GB RAM**, **200 GB** block volume, dan kuota
> egress besar. Akun Always Free tetap gratis selama pemakaian di dalam kuota,
> tetapi pendaftaran memerlukan **kartu kredit/debit untuk verifikasi** (tidak
> ditagih kecuali Anda upgrade ke akun bayar). Region harus dipilih saat
> pendaftaran dan tidak bisa diubah.

Alternatif tanpa kartu: `docker-compose.preview.yml` + Cloudflare Tunnel dari
komputer sendiri, atau Render Free (demo terbatas, lihat `render.yaml`).

---

## 1. Buat VM

1. Daftar/masuk ke [cloud.oracle.com](https://cloud.oracle.com) dan pilih home
   region terdekat (mis. **Singapore**).
2. Buka **Compute → Instances → Create instance**.
3. Isi:
   - **Name:** `hxconvert`
   - **Image:** Ubuntu 24.04 (image Arm/aarch64)
   - **Shape:** `VM.Standard.A1.Flex`, **2 OCPU**, **12 GB memory**
   - **Boot volume:** 100 GB (total boot + block volume harus ≤ 200 GB)
   - **SSH key:** unggah public key Anda (`~/.ssh/id_ed25519.pub`)
   - **Public IPv4 address:** assign
4. Setelah instance dibuat, catat **Public IP address**.
5. Buka **Networking → VCN → Security Lists → Default Security List** dan
   tambahkan ingress:

   | Source | Protocol | Destination port |
   |---|---|---|
   | `0.0.0.0/0` | TCP | 22 |
   | `0.0.0.0/0` | TCP | 80 |
   | `0.0.0.0/0` | TCP | 443 (hanya bila memakai domain) |

Jika muncul **"Out of host capacity"**, coba availability domain lain, ulangi
beberapa saat kemudian, atau upgrade ke akun Pay-As-You-Go (kuota Always Free
tetap berlaku).

## 2. Siapkan server

```bash
ssh ubuntu@<PUBLIC_IP>

sudo apt update && sudo apt upgrade -y

# Docker + Compose plugin
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
newgrp docker

docker compose version   # harus v2+
```

Jika port 80/443 tetap tidak bisa diakses padahal Security List sudah benar,
image Ubuntu OCI biasanya masih punya aturan iptables lokal:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

## 3. Ambil kode

```bash
git clone <URL_REPO> ~/hxconvert
cd ~/hxconvert
```

Untuk repo privat, gunakan deploy key atau personal access token. Jangan
menjalankan `deploy.sh` sebagai root.

## 4. Siapkan environment

### 4a. Ujicoba tanpa domain (paling cepat)

```bash
cat > .env.preview <<'EOF'
POSTGRES_PASSWORD=ganti-dengan-password-acak
SECRET_KEY=ganti-dengan-48-karakter-acak
ALLOW_ANONYMOUS=true
MAX_UPLOAD_MB=95
MAX_FILES_PER_JOB=5
EOF
chmod 600 .env.preview

# Nilai acak:
#   openssl rand -base64 48
#   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

`MAX_UPLOAD_MB=95` aman untuk Cloudflare Tunnel gratis (batas body ~100 MB).
Tanpa tunnel, nilai ini boleh dinaikkan.

### 4b. Dengan domain + HTTPS

```bash
cp .env.production.template .env.production
nano .env.production
```

Isi minimal: `DOMAIN`, `EMAIL`, `POSTGRES_PASSWORD`, `SECRET_KEY`,
`CORS_ORIGINS` (mis. `https://domain-anda.com`), dan biarkan
`NEXT_PUBLIC_API_URL` kosong agar frontend memakai URL relatif same-origin.
Arahkan record **A** domain ke public IP sebelum lanjut.

## 5. Build dan jalankan

### Tanpa domain

```bash
docker compose --env-file .env.preview \
  -f docker-compose.yml -f docker-compose.preview.yml \
  up -d --build
```

Override `docker-compose.preview.yml` memakai `nginx/preview.conf` (HTTP port 80),
tidak menjalankan certbot, dan mengarahkan `/api/` ke backend. Frontend dibangun
dengan `NEXT_PUBLIC_API_URL` kosong, jadi cookie sesi tamu/login tetap bekerja.

Build pertama di Arm memakan waktu (LibreOffice + Tesseract ikut dipasang) dan
bisa 10–20 menit. Pantau:

```bash
docker compose --env-file .env.preview logs -f backend
```

Verifikasi:

```bash
curl -fsS http://localhost/api/v1/health
docker compose --env-file .env.preview ps
```

Buka `http://<PUBLIC_IP>`.

### Opsional: URL HTTPS sementara

Di komputer lain (bukan VM) atau di VM itu sendiri:

```bash
cloudflared tunnel --url http://localhost:80
```

Atau, bila memakai domain:

```bash
chmod +x deploy.sh
./deploy.sh production
```

`deploy.sh` akan membangun image, menjalankan migrasi, membuat sertifikat
Let's Encrypt lewat webroot, lalu memeriksa health endpoint. Setelah itu pasang
cron reload nginx mingguan seperti dijelaskan di `DEPLOYMENT.md`.

## 6. Operasional

```bash
# Log
docker compose --env-file .env.preview logs -f

# Update kode
git pull
docker compose --env-file .env.preview \
  -f docker-compose.yml -f docker-compose.preview.yml \
  up -d --build

# Bersihkan image/builder cache agar disk tidak penuh
docker image prune -f
docker builder prune -f

# Backup database
docker compose --env-file .env.preview exec -T postgres \
  pg_dump -U docuconvert docuconvert > backup_$(date +%Y%m%d).sql
```

Data hasil konversi tersimpan di volume `storage_data`; database di
`postgres_data`. Keduanya bertahan saat container dibuat ulang, tetapi tidak
bertahan bila volume dihapus.

## 7. Catatan khusus ARM

- Semua image dibangun **di VM** (tidak ada image Arm siap pakai), jadi
  `docker compose build` membutuhkan waktu dan RAM. Build paralel bisa
  menekan RAM; bila perlu: `COMPOSE_PARALLEL_LIMIT=1 docker compose ... build`.
- LibreOffice, Tesseract, dan Poppler tersedia di repo Ubuntu Arm.
- Wheel Python untuk PyMuPDF, pdf2docx, dan psycopg tersedia untuk aarch64.
  RapidOCR/onnxruntime juga punya wheel aarch64; bila suatu saat gagal, cek
  pesan `pip` di log build.
- Microsoft Word tidak ada di Linux; DOCX → PDF memakai LibreOffice (bukan Word
  Automation). Ini normal dan sudah ditangani backend.

## 8. Pemecahan masalah

| Gejala | Penyebab / solusi |
|---|---|
| `Out of host capacity` | Kapasitas A1 penuh di region/AD. Coba AD lain, ulangi, atau upgrade ke Pay-As-You-Go. |
| Situs tidak terbuka | Cek Security List **dan** iptables host (lihat langkah 2). Pastikan nginx publish port 80. |
| `POSTGRES_PASSWORD is required` | Jalankan compose dengan `--env-file` yang berisi password, atau isi `.env` root. |
| Health check gagal | `docker compose ... logs backend`; biasanya migrasi DB atau volume permission. |
| Build kehabisan disk | `docker system prune -af`; boot volume 100 GB cukup untuk image + cache. |
| Build lambat/OOM | Turunkan paralelisme build (`COMPOSE_PARALLEL_LIMIT=1`) dan tutup proses lain. |
| Sertifikat gagal | DNS A harus sudah mengarah ke IP, port 80 terbuka, dan domain belum pernah kena rate limit Let's Encrypt. |

## 9. Checklist keamanan untuk ujicoba

- [ ] `SECRET_KEY` acak, bukan default.
- [ ] `POSTGRES_PASSWORD` kuat dan file env `chmod 600`.
- [ ] Hanya port 22, 80, 443 yang terbuka.
- [ ] `CORS_ORIGINS` diisi origin nyata (bukan `*`).
- [ ] `ALLOW_ANONYMOUS=true` hanya selama ujicoba; matikan bila sudah ada akun.
- [ ] Backup database sebelum menghapus atau mengubah VM.

Always Free cocok untuk demo dan pengujian, bukan untuk produksi: tidak ada SLA,
kapasitas Arm bisa langka, dan Oracle dapat melakukan maintenance. Simpan kode
dan konfigurasi di Git, serta backup data yang penting.
