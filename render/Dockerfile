# syntax=docker/dockerfile:1

# Image demo untuk Render Free (0.1 CPU / 512 MB).
#
# Satu container menjalankan nginx + Next.js + FastAPI supaya browser tetap
# same-origin (cookie sesi tamu dan login tidak terkena aturan cookie lintas situs).
# LibreOffice dan Tesseract sengaja TIDAK dipasang; RapidOCR juga dibuang dari
# requirements. Alat yang membutuhkannya akan dilaporkan "tidak tersedia" oleh
# backend, bukan gagal saat dipakai.

# ---------------------------------------------------------------- frontend
FROM node:20-bookworm-slim AS frontend-build
WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
ENV NEXT_TELEMETRY_DISABLED=1
# Kosong = permintaan /api relatif, diproksi nginx ke container yang sama.
ENV NEXT_PUBLIC_API_URL=""
RUN npm run build

# ---------------------------------------------------------------- backend
FROM python:3.11-slim AS backend-build
WORKDIR /app

COPY backend/requirements.txt ./
# Demo: tanpa RapidOCR (onnxruntime) agar hemat RAM. Tanpa Tesseract di runtime,
# capabilities melaporkan OCR tidak tersedia.
RUN grep -v '^rapidocr-onnxruntime' requirements.txt > requirements.demo.txt \
    && pip install --no-cache-dir --prefix=/install -r requirements.demo.txt

# ---------------------------------------------------------------- runtime
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

# nginx sebagai reverse proxy; libgl1/libglib2.0 untuk OpenCV (pdf2docx).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        nginx \
        gettext-base \
        libpq5 \
        libgl1 \
        libglib2.0-0 \
        libstdc++6 \
        fonts-dejavu-core \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node:20-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=backend-build /install /usr/local

COPY --from=frontend-build /app/frontend/.next/standalone /app/frontend
COPY --from=frontend-build /app/frontend/.next/static /app/frontend/.next/static
COPY --from=frontend-build /app/frontend/public /app/frontend/public

COPY backend /app/backend
COPY render/nginx.conf /etc/nginx/nginx.template.conf
COPY render/start.sh /usr/local/bin/start.sh

RUN sed -i 's/\r$//' /usr/local/bin/start.sh \
    && chmod +x /usr/local/bin/start.sh \
    && mkdir -p /app/storage/inputs /app/storage/outputs /app/storage/tmp /app/storage/cache

EXPOSE 10000

CMD ["/usr/local/bin/start.sh"]
