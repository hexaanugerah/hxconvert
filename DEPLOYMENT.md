# HXconvertX Deployment Guide

## Quick Start (Development)

```bash
# Start development environment with hot reload
docker compose -f docker-compose.dev.yml up --build

# Access:
# Frontend: http://localhost:3000
# Backend API: http://localhost:8000
# API Docs: http://localhost:8000/docs
```

## Production Deployment

### Free trial options

- **Oracle Cloud Always Free (ARM VM, full stack):** step-by-step in
  [`docs/DEPLOY_ORACLE_FREE.md`](docs/DEPLOY_ORACLE_FREE.md).
- **No domain / Cloudflare Tunnel:**
  `docker compose -f docker-compose.yml -f docker-compose.preview.yml up -d --build`
  (see the comments in `docker-compose.preview.yml`).
- **Render Free (limited demo, no LibreOffice/OCR):** deploy the Blueprint in
  `render.yaml` (image: `render/Dockerfile`).

### Option 1: VPS with Docker Compose (Recommended)

**Requirements:**
- Ubuntu 22.04+ / Debian 12+
- Docker 24+ & Docker Compose v2+
- Domain name pointing to server IP
- 2GB+ RAM, 20GB+ disk

**Steps:**

1. **Prepare server:**
```bash
# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# Install Docker Compose (if not included)
sudo apt install docker-compose-plugin
```

2. **Clone and configure:**
```bash
git clone https://github.com/YOUR_USERNAME/docuconvert.git
cd docuconvert

# Copy and edit production config
cp .env.production.template .env.production
nano .env.production  # Fill in ALL values
```

3. **Deploy:**
```bash
chmod +x deploy.sh
./deploy.sh production
```

4. **Verify:**
- Visit `https://HXconvert.com`
- API docs at `https://HXconvert.com/api/v1/docs`

#### Deploy otomatis dari GitHub Actions (VPS)

Job `deploy-production` di `.github/workflows/ci-cd.yml` men-deploy lewat SSH
setiap push ke `main`, setelah job lint/test dan build image selesai. Isi
secrets repository (Settings → Secrets and variables → Actions):

| Secret | Wajib | Isi |
|--------|-------|-----|
| `PROD_HOST` | Ya | IP atau hostname server |
| `PROD_USER` | Ya | User SSH non-root (pemilik Docker) |
| `PROD_SSH_KEY` | Ya | Private key SSH (OpenSSH) untuk user tersebut |
| `PROD_PORT` | No | Port SSH, default `22` |
| `PROD_PATH` | No | Direktori checkout di server, default `/opt/docuconvert` |

Langkah di server: `git fetch origin main`, `git reset --hard origin/main`,
lalu `bash ./deploy.sh production`. Artinya server harus sudah punya
`.env.production` dan akses git ke repo ini (deploy key atau kredensial HTTPS).

Bila ketiga secret wajib belum diisi, langkah deploy **dilewati dengan pesan
notice** dan job tetap hijau — bukan gagal dan bukan pura-pura berhasil.
Aktifkan juga *required reviewers* pada environment `production` bila deploy
perlu persetujuan manual.

Untuk membatalkan deploy VPS sepenuhnya, hapus job `deploy-production` dari
workflow; Railway dan Vercel tetap men-deploy sendiri karena terhubung langsung
ke repo git.

### Option 2: Vercel + Railway + TiDB Cloud

Split deployment: Next.js on Vercel, FastAPI on Railway, and TiDB Cloud
(MySQL-compatible) for metadata. This keeps the heavy LibreOffice/Tesseract
image off Vercel and needs no server to manage.

Full runbook: [`docs/DEPLOY_VERCEL_RAILWAY_TIDB.md`](docs/DEPLOY_VERCEL_RAILWAY_TIDB.md).

1. Create a TiDB Cloud Starter instance, a database, and a URL-encoded
   `DATABASE_URL` (`mysql+pymysql://...`).
2. Railway: deploy the repo with **Root Directory** `backend`, attach a volume
   at `/data`, set `STORAGE_DIR=/data/storage`, `RAILWAY_RUN_UID=0`,
   `UVICORN_WORKERS=1`, `DATABASE_SSL=true`, and a random `SECRET_KEY`.
3. Vercel: import the repo with **Root Directory** `frontend` and set
   `BACKEND_URL` to the Railway domain so `/api/*` is proxied same-origin
   (cookies stay first-party). Leave `NEXT_PUBLIC_API_URL` unset.
4. Verify `https://<project>.vercel.app/api/v1/health` and run a batch
   conversion from the browser.

> Railway runs one replica only: background jobs and uploaded files live on the
> attached volume, not in TiDB.

### Option 3: Render

Blueprint siap pakai ada di `render.yaml` (satu container: nginx + Next.js +
FastAPI, lihat `render/Dockerfile`).

1. Buka
   `https://dashboard.render.com/blueprint/new?repo=https://github.com/hexaanugerah/hxconvert`
   (atau Dashboard → **New** → **Blueprint** → pilih repo ini), lalu **Apply**.
2. Render membaca `render.yaml`: service `hxconvert-demo`, region Singapore,
   plan Free, health check `/api/v1/health`, dan auto-deploy setiap commit.
3. Setelah aktif, setiap push ke `main` langsung men-deploy ulang. URL demo
   menjadi `https://hxconvert-demo.onrender.com`.

> Plan Free tidur saat tidak dipakai (cold start ~50 detik) dan memakai SQLite
> di disk ephemeral: data hilang saat redeploy. LibreOffice dan OCR tidak
> terpasang, jadi alat yang membutuhkannya dilaporkan tidak tersedia.

Alternatif tanpa Blueprint: buat Web Service dari GitHub dengan build command
`cd backend && pip install -r requirements.txt` dan start command
`cd backend && alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT`,
lalu tambahkan PostgreSQL dan environment variables.

### Option 4: Fly.io

```bash
# Install flyctl
curl -L https://fly.io/install.sh | sh

# Launch
fly launch --name docuconvert
# Follow prompts, select PostgreSQL

# Set secrets
fly secrets set SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
fly secrets set CORS_ORIGINS="https://HXconvert.com"
fly deploy
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DOMAIN` | Yes | Your domain (e.g., `docuconvert.example.com`) |
| `EMAIL` | Yes | Email for Let's Encrypt SSL |
| `POSTGRES_PASSWORD` | Yes | Strong database password |
| `SECRET_KEY` | Yes | 48+ char random string |
| `CORS_ORIGINS` | Yes | Frontend URL(s), comma-separated |
| `NEXT_PUBLIC_API_URL` | No | Kosongkan (disarankan) untuk same-origin lewat nginx relatif; isi origin hanya jika API di domain terpisah |
| `ALLOW_ANONYMOUS` | No | `true`/`false` - allow unauthenticated conversions |
| `RATE_LIMIT_PER_MINUTE` | No | API rate limit per IP |
| `MAX_UPLOAD_MB` | No | Max file upload size |
| `MAX_FILES_PER_JOB` | No | Max files per job (default `50`) |
| `MAX_BATCH_SIZE_MB` | No | Max combined upload size per job (default `2048`) |

## SSL Certificates

`./deploy.sh` bootstraps a short-lived self-signed certificate, starts nginx, asks
Let's Encrypt for a real certificate through the ACME webroot challenge, then reloads
nginx. The `certbot` container renews certificates every 12 hours.

Nginx keeps the old certificate in memory until it is reloaded, so schedule a reload
after renewal (weekly is enough for 90-day certificates):

```bash
# crontab -e
0 3 * * 1 cd /opt/docuconvert && docker compose exec -T nginx nginx -s reload
```

**First deploy:** certificate obtained during `./deploy.sh`
**Renewal:** automatic via the `certbot` container; reload nginx with the cron job above.

## Environment Files

Compose only loads `.env` automatically. Production values live in `.env.production`, so
either use `./deploy.sh` (it passes `--env-file .env.production` for you) or add the flag
to every manual command:

```bash
docker compose --env-file .env.production up -d
```

`NEXT_PUBLIC_API_URL` is baked into the frontend bundle at **build** time. After changing
it, rebuild the frontend (`docker compose --env-file .env.production build frontend`)
instead of only restarting it.

## Database Migrations

```bash
# Run migrations manually
docker compose run --rm backend alembic upgrade head

# Create new migration
docker compose run --rm backend alembic revision --autogenerate -m "description"

# Check current revision
docker compose run --rm backend alembic current
```

## Monitoring & Logs

```bash
# View all logs
docker compose logs -f

# Specific service
docker compose logs -f backend
docker compose logs -f frontend
docker compose logs -f nginx

# Health check
curl https://HXconvert.com/api/v1/health
```

## Backup & Restore

```bash
# Backup database
docker compose exec postgres pg_dump -U docuconvert docuconvert > backup_$(date +%Y%m%d).sql

# Backup storage
tar -czf storage_backup_$(date +%Y%m%d).tar.gz -C /opt/docuconvert storage_data

# Restore database
docker compose exec -T postgres psql -U docuconvert docuconvert < backup_20240101.sql
```

## Troubleshooting

**Backend won't start:**
```bash
docker compose logs backend
# Check: DATABASE_URL, SECRET_KEY, migrations
```

**Frontend shows "API not found":**
```bash
# Check NEXT_PUBLIC_API_URL in .env.production
# Verify nginx proxy_pass to backend
```

**SSL certificate errors:**
```bash
# Check certbot logs
docker compose logs certbot

# Force renewal
docker compose run --rm --entrypoint certbot certbot certonly --webroot \
  -w /var/www/certbot --force-renewal -d HXconvert.com
docker compose exec nginx nginx -s reload
```

**File upload fails:**
```bash
# Check nginx client_max_body_size (100M in config)
# Check backend MAX_UPLOAD_MB
# Check storage volume permissions
```

## Updating

```bash
# Pull latest code
git pull origin main

# Rebuild and deploy
./deploy.sh production

# Or manually:
docker compose pull
docker compose run --rm backend alembic upgrade head
docker compose up -d --remove-orphans
```

## Security Checklist

- [ ] Change default `SECRET_KEY`
- [ ] Use strong `POSTGRES_PASSWORD`
- [ ] Set `ALLOW_ANONYMOUS=false`
- [ ] Configure `CORS_ORIGINS` to your domain only
- [ ] Enable firewall (ufw allow 22,80,443)
- [ ] Set up fail2ban for SSH
- [ ] Regular security updates: `apt update && apt upgrade`
- [ ] Monitor logs for suspicious activity