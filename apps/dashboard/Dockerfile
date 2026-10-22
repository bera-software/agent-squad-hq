# syntax=docker/dockerfile:1.7
# Build from the apps/ workspace root so @squadhq/shared-api is available:
#   docker build -f dashboard/Dockerfile -t squadhq-dashboard apps/
# (BuildKit picks up dashboard/Dockerfile.dockerignore automatically.)
ARG NODE_VERSION=22-alpine

FROM node:${NODE_VERSION} AS deps
WORKDIR /repo
COPY package.json package-lock.json ./
COPY shared-api/package.json shared-api/
COPY dashboard/package.json dashboard/
COPY mobile/package.json mobile/
RUN npm ci --workspace @squadhq/dashboard --include-workspace-root --no-audit --no-fund

FROM node:${NODE_VERSION} AS build
WORKDIR /repo
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /repo/node_modules ./node_modules
COPY package.json package-lock.json ./
COPY shared-api ./shared-api
COPY dashboard ./dashboard
RUN npm run build --workspace @squadhq/dashboard

FROM node:${NODE_VERSION} AS runtime
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
RUN addgroup -S app && adduser -S -G app app
# Standalone output mirrors the workspace layout (outputFileTracingRoot = apps/).
COPY --from=build --chown=app:app /repo/dashboard/.next/standalone ./
COPY --from=build --chown=app:app /repo/dashboard/.next/static ./dashboard/.next/static
COPY --from=build --chown=app:app /repo/dashboard/public ./dashboard/public
USER app
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://127.0.0.1:3000/api/health || exit 1
CMD ["node", "dashboard/server.js"]
