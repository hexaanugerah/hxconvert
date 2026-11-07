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
    libpq5 \
    libgl1 \
    libglib2.0-0 \
    libreoffice \
    fonts-dejavu-core \
    fonts-liberation \
    fonts-crosextra-carlito \
    fonts-crosextra-caladea \
    tesseract-ocr \
    tesseract-ocr-ind \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

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
    && chown -R app:app /app/storage
USER app

# Expose port
EXPOSE 8000

# Run migrations then start server.
# --workers 2: each uvicorn worker owns its own persistent process pool for
# batch conversions (CPU-bound children), so 4 workers multiplies RAM needs;
# 2 is the safe default for a single conversion host.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2"]