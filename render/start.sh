#!/bin/bash
# Jalankan nginx (port publik Render), Next.js, dan FastAPI dalam satu container.
set -Eeuo pipefail

export PORT="${PORT:-10000}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
BACKEND_PORT="${BACKEND_PORT:-8000}"

# Hanya ${PORT} yang disubstitusi; variabel nginx seperti $host dibiarkan.
envsubst '${PORT}' < /etc/nginx/nginx.template.conf > /etc/nginx/nginx.conf

pids=()
cleanup() {
    trap - EXIT TERM INT
    if ((${#pids[@]})); then
        kill "${pids[@]}" 2>/dev/null || true
        wait 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

cd /app/backend
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" --workers 1 &
pids+=($!)

cd /app/frontend
HOSTNAME=127.0.0.1 PORT="$FRONTEND_PORT" node server.js &
pids+=($!)

nginx -g 'daemon off;' &
pids+=($!)

# Keluar bila salah satu proses mati, supaya Render menandai deploy gagal
# daripada menyajikan halaman setengah rusak.
set +e
wait -n
status=$?
set -e
exit "$status"
