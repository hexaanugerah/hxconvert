# Railway builds this repository from its root, so this image mirrors
# backend/Dockerfile while copying from the backend/ subdirectory. Docker
# Compose and the GitHub image workflow keep using backend/Dockerfile.
FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libpq5 \
    libgl1 \
    libglib2.0-0 \
    libreoffice \
    fontconfig \
    fonts-dejavu-core \
    fonts-liberation \
    fonts-crosextra-carlito \
    fonts-crosextra-caladea \
    tesseract-ocr \
    tesseract-ocr-ind \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

COPY backend/assets/fonts/*.ttf /usr/share/fonts/truetype/source-sans-pro/
RUN fc-cache -f

COPY --from=builder /install /usr/local
COPY backend/ .

RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
RUN mkdir -p /app/storage/inputs /app/storage/outputs /app/storage/tmp /app/storage/cache \
    /data/storage/inputs /data/storage/outputs /data/storage/tmp /data/storage/cache \
    && chown -R app:app /app/storage /data
USER app

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${UVICORN_WORKERS:-2} --proxy-headers --forwarded-allow-ips='*'"]
