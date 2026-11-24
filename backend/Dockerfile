# Build stage
FROM python:3.11-slim AS builder

WORKDIR /app

# Install system dependencies for building
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Runtime stage
FROM python:3.11-slim AS runtime

WORKDIR /app

# Install runtime dependencies
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

# Source Sans Pro is not packaged by Debian bookworm, but SAP/SPPb-style PDFs
# and Word files reference it by name. Ship the OFL-licensed faces so
# LibreOffice uses them instead of substituting a wider fallback font.
COPY assets/fonts/*.ttf /usr/share/fonts/truetype/source-sans-pro/
RUN fc-cache -f

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY . .

# Create non-root user, then prepare storage subdirectories with that user's
# ownership so Docker initializes a fresh named volume mounted at /app/storage
# with the right owner; otherwise the non-root user cannot create files in it.
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
RUN mkdir -p /app/storage/inputs /app/storage/outputs /app/storage/tmp /app/storage/cache \
    /data/storage/inputs /data/storage/outputs /data/storage/tmp /data/storage/cache \
    && chown -R app:app /app/storage /data
USER app

# Expose port
EXPOSE 8000

# Run migrations then start server.
# UVICORN_WORKERS defaults to 2: each uvicorn worker owns its own persistent
# process pool for batch conversions (CPU-bound children), so more workers
# multiply RAM needs. PaaS platforms (Railway) should set UVICORN_WORKERS=1 and
# let CONVERSION_WORKERS provide the parallelism. PORT is provided by the
# platform; proxy headers make download URLs and cookies use HTTPS correctly.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${UVICORN_WORKERS:-2} --proxy-headers --forwarded-allow-ips='*'"]