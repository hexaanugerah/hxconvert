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

### Option 2: Railway (Easiest PaaS)

1. Push to GitHub
2. Connect Railway to repo
3. Add PostgreSQL plugin
4. Set environment variables in Railway dashboard:
   - `DATABASE_URL` (auto from PostgreSQL plugin)
   - `SECRET_KEY` (generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"`)
   - `CORS_ORIGINS` (your frontend domain)
   - `ENVIRONMENT=production`
   - `ALLOW_ANONYMOUS=false`
5. Deploy!

### Option 3: Render

1. Create new Web Service from GitHub
2. Build command: `cd backend && pip install -r requirements.txt`
3. Start command: `cd backend && alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Add PostgreSQL database
5. Set environment variables

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