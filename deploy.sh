#!/usr/bin/env bash
# deploy.sh - Deploy DocuConvert to a VPS
# Usage: ./deploy.sh [staging|production]
#
# The script builds the images, runs migrations, obtains a Let's Encrypt
# certificate on first deploy, and starts the stack. Nginx is started with a
# short-lived self-signed certificate first so that the ACME webroot challenge
# can be served before a real certificate exists.

set -euo pipefail

ENVIRONMENT="${1:-production}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$SCRIPT_DIR}"
DEFAULT_PROJECT_DIR="/opt/docuconvert"
COMPOSE_FILE="docker-compose.yml"
ENV_FILE=".env.production"
REPO_URL="${REPO_URL:-https://github.com/YOUR_USERNAME/docuconvert.git}"

echo "🚀 Deploying DocuConvert to $ENVIRONMENT..."

if [[ $EUID -eq 0 ]]; then
   echo "⚠️  Don't run as root. Use a sudo user instead."
   exit 1
fi

# Prefer the Compose v2 plugin; fall back to the legacy docker-compose binary.
if docker compose version >/dev/null 2>&1; then
    DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    DC=(docker-compose)
else
    echo "❌ Docker Compose not found. Install the Docker Compose v2 plugin first."
    exit 1
fi

for cmd in docker curl git; do
    if ! command -v "$cmd" &> /dev/null; then
        echo "❌ $cmd not found. Please install it first."
        exit 1
    fi
done

mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"

# Only touch git when deploying to the canonical server directory. Running the
# script from a local checkout must not discard uncommitted work.
if [[ "$PROJECT_DIR" == "$DEFAULT_PROJECT_DIR" ]]; then
    if [[ -d ".git" ]]; then
        echo "📥 Updating repository..."
        git fetch origin
        git reset --hard origin/main
    else
        echo "📥 Cloning repository..."
        git clone "$REPO_URL" .
    fi
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "⚠️  $ENV_FILE not found!"
    echo "📋 Copy .env.production.template and fill in your values:"
    echo "   cp .env.production.template $ENV_FILE"
    echo "   nano $ENV_FILE"
    exit 1
fi

# Export the file so Compose interpolation and the validation below see the same values.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

required_vars=("DOMAIN" "EMAIL" "POSTGRES_PASSWORD" "SECRET_KEY" "CORS_ORIGINS" "NEXT_PUBLIC_API_URL")
for var in "${required_vars[@]}"; do
    if [[ -z "${!var:-}" ]]; then
        echo "❌ Required variable $var is not set in $ENV_FILE"
        exit 1
    fi
done

if [[ "$SECRET_KEY" == "generate-a-strong-random-secret-key-here" ]] || [[ ${#SECRET_KEY} -lt 32 ]]; then
    echo "❌ SECRET_KEY must be a real random value of at least 32 characters."
    echo "   python -c \"import secrets; print(secrets.token_urlsafe(48))\""
    exit 1
fi

echo "✅ Environment variables validated"

compose() {
    "${DC[@]}" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

# Render the domain into the nginx config. The sed expressions are idempotent,
# so re-running the deploy or changing the domain both work.
echo "🔧 Configuring Nginx for $DOMAIN..."
sed -i -E "s|(server_name)[^;]+;|\1 $DOMAIN www.$DOMAIN;|g" nginx/nginx.conf
sed -i -E "s|/etc/letsencrypt/live/[^/]+/|/etc/letsencrypt/live/$DOMAIN/|g" nginx/nginx.conf

# Nginx refuses to start when the certificate files are missing. Bootstrap a
# one-day self-signed certificate so the HTTP listener can serve the ACME
# challenge; certbot replaces it immediately afterwards.
CERT_DIR="certbot/conf/live/$DOMAIN"
NEED_CERT=0
mkdir -p "$CERT_DIR" certbot/www
if [[ ! -f "$CERT_DIR/fullchain.pem" || ! -f "$CERT_DIR/privkey.pem" ]]; then
    if ! command -v openssl &> /dev/null; then
        echo "❌ openssl not found. It is required to bootstrap the TLS certificate."
        exit 1
    fi
    echo "🔐 Creating temporary self-signed certificate..."
    openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
        -keyout "$CERT_DIR/privkey.pem" \
        -out "$CERT_DIR/fullchain.pem" \
        -subj "/CN=$DOMAIN" >/dev/null 2>&1
    NEED_CERT=1
fi

echo "📦 Building images..."
compose build

echo "🗄️  Starting database and running migrations..."
compose up -d postgres redis
compose run --rm backend alembic upgrade head

echo "🚀 Starting services..."
compose up -d --remove-orphans

if [[ "$NEED_CERT" == "1" ]]; then
    echo "🔐 Obtaining SSL certificates from Let's Encrypt..."
    # The service entrypoint is a renewal loop, so override it for the one-off issuance.
    compose run --rm --entrypoint certbot certbot certonly \
        --webroot -w /var/www/certbot \
        --email "$EMAIL" \
        --agree-tos --no-eff-email \
        -d "$DOMAIN" -d "www.$DOMAIN"
    compose exec nginx nginx -s reload
fi

echo "⏳ Waiting for services to be ready..."
HEALTHY=0
for _ in $(seq 1 30); do
    if curl -fsS "https://$DOMAIN/api/v1/health" > /dev/null 2>&1; then
        HEALTHY=1
        break
    fi
    sleep 5
done

if [[ "$HEALTHY" != "1" ]]; then
    echo "❌ Backend health check failed"
    compose logs backend --tail=50
    exit 1
fi
echo "✅ Backend is healthy!"

if curl -fsS "https://$DOMAIN" > /dev/null 2>&1; then
    echo "✅ Frontend is accessible!"
else
    echo "❌ Frontend check failed"
    compose logs frontend --tail=50
    exit 1
fi

echo "🧹 Cleaning up dangling Docker images..."
docker image prune -f

echo ""
echo "🎉 Deployment complete!"
echo "🌐 Application: https://$DOMAIN"
echo "📚 API Docs: https://$DOMAIN/api/v1/docs"
echo ""
echo "📋 Useful commands:"
echo "  View logs:    docker compose logs -f [backend|frontend|nginx]"
echo "  Restart:      docker compose restart"
echo "  Stop:         docker compose down"
echo "  Reload TLS:   docker compose exec nginx nginx -s reload"
echo "  Update:       ./deploy.sh $ENVIRONMENT"
