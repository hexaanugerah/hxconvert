# Build stage
FROM node:20-alpine AS builder

WORKDIR /app

# Copy package files
COPY package*.json ./

# Install dependencies
RUN npm ci

# Copy source code
COPY . .

# NEXT_PUBLIC_* values are inlined into the client bundle at build time, so the
# API URL must be supplied as a build argument instead of a runtime environment
# variable.
ARG NEXT_PUBLIC_API_URL=http://127.0.0.1:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_TELEMETRY_DISABLED=1

# Build application
RUN npm run build

# Runtime stage
FROM node:20-alpine AS runtime

WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
# Next's standalone server binds to $HOSTNAME by default; Docker sets it to the
# container id, which makes the port mapping and healthcheck unreachable.
ENV HOSTNAME=0.0.0.0
ENV PORT=3000

# Copy built application
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

# Create non-root user
RUN addgroup --system --gid 1001 nodejs \
    && adduser --system --uid 1001 nextjs
USER nextjs

# Expose port
EXPOSE 3000

# Start server
CMD ["node", "server.js"]