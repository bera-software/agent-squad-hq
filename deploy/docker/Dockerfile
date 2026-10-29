# syntax=docker/dockerfile:1.7
#
# Agent Squad HQ — server image (api / worker / scheduler share it).
#
#   docker build -f deploy/docker/Dockerfile -t agent-squad-hq:dev .
#
# Build context is the repository root. Pin the base image by digest for
# releases (renovate/dependabot keep it fresh):
#   docker buildx imagetools inspect python:3.12-slim-bookworm
#   ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:<digest>
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
# GitHub CLI: the control-plane modules (sweeper, transitions, janitor) shell out to `gh`.
ARG GH_CLI_VERSION=2.62.0

# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
# The wheel force-includes hub content (prompts, agents, packs, schema, workflows,
# evals/golden, ...; see [tool.hatch.build.targets.wheel.force-include]), so the
# whole (dockerignored) context is needed here.
COPY . ./
# psycopg is the optional Postgres driver (SQLite needs nothing extra).
RUN pip install --upgrade pip wheel \
 && pip install ".[server]" "psycopg[binary]>=3.1,<4"

ARG GH_CLI_VERSION
# No default: BuildKit sets TARGETARCH from the target platform (amd64, arm64).
# ADD --chmod already requires BuildKit, so it is always set for this stage.
ARG TARGETARCH
ADD --chmod=0644 https://github.com/cli/cli/releases/download/v${GH_CLI_VERSION}/gh_${GH_CLI_VERSION}_linux_${TARGETARCH}.tar.gz /tmp/gh.tgz
ADD --chmod=0644 https://github.com/cli/cli/releases/download/v${GH_CLI_VERSION}/gh_${GH_CLI_VERSION}_checksums.txt /tmp/gh_checksums.txt
RUN cd /tmp \
 && grep "gh_${GH_CLI_VERSION}_linux_${TARGETARCH}.tar.gz" gh_checksums.txt | sed "s#gh_${GH_CLI_VERSION}_linux_${TARGETARCH}.tar.gz#gh.tgz#" | sha256sum -c - \
 && tar -xzf gh.tgz \
 && install -m 0755 "gh_${GH_CLI_VERSION}_linux_${TARGETARCH}/bin/gh" /usr/local/bin/gh

# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="agent-squad-hq" \
      org.opencontainers.image.description="Agent Squad HQ server mode (API, worker, scheduler)" \
      org.opencontainers.image.source="https://github.com/berasoftware/agent-squad-hq" \
      org.opencontainers.image.licenses="Apache-2.0"

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 squadhq \
 && useradd --system --uid 10001 --gid 10001 --home-dir /var/lib/squadhq --shell /usr/sbin/nologin squadhq \
 && mkdir -p /var/lib/squadhq /opt/squadhq/hub \
 && chown 10001:10001 /var/lib/squadhq

COPY --from=build /opt/venv /opt/venv
COPY --from=build /usr/local/bin/gh /usr/local/bin/gh
# Hub content the control-plane modules read (prompts, flows, packs, agents).
COPY --chown=0:0 config /opt/squadhq/hub/config
COPY --chown=0:0 prompts /opt/squadhq/hub/prompts
COPY --chown=0:0 agents /opt/squadhq/hub/agents
COPY --chown=0:0 org /opt/squadhq/hub/org
COPY --chown=0:0 packs /opt/squadhq/hub/packs
COPY --chown=0:0 schema /opt/squadhq/hub/schema
COPY --chown=0:0 squadhq.example.yml /opt/squadhq/hub/squadhq.example.yml

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SQUADHQ_HOME=/opt/squadhq/hub \
    SQUADHQ_CONFIG=/etc/squadhq/squadhq.yml \
    SQUADHQ_DB_URL=sqlite:////var/lib/squadhq/squadhq.db \
    SQUADHQ_PORT=8080 \
    HOME=/var/lib/squadhq \
    GH_CONFIG_DIR=/tmp/gh \
    GH_NO_UPDATE_NOTIFIER=1

# Read-only-root friendly: the only writable paths are /var/lib/squadhq (volume,
# SQLite only) and /tmp (tmpfs). Mount squadhq.yml at /etc/squadhq/squadhq.yml.
VOLUME ["/var/lib/squadhq"]
WORKDIR /opt/squadhq/hub
USER 10001:10001
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('SQUADHQ_PORT','8080')+'/healthz', timeout=4).status==200 else 1)"]

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "squadhq.server"]
CMD ["serve"]

# ---------------------------------------------------------------------------
# Worker-mode variant: adds a static docker CLI so the worker can start
# sandboxed executor containers through a docker socket *proxy*
# (deploy/docker-compose.worker-mode.yml). Build with --target worker-mode.
FROM ${PYTHON_IMAGE} AS docker-cli
ARG DOCKER_CLI_VERSION=27.3.1
# No default: BuildKit sets it per target platform; amd64 only as a fallback.
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && arch="$(case "${TARGETARCH:-amd64}" in arm64) echo aarch64 ;; *) echo x86_64 ;; esac)" \
 && curl -fsSL "https://download.docker.com/linux/static/stable/${arch}/docker-${DOCKER_CLI_VERSION}.tgz" -o /tmp/docker.tgz \
 && tar -xzf /tmp/docker.tgz -C /tmp \
 && install -m 0755 /tmp/docker/docker /usr/local/bin/docker

FROM runtime AS worker-mode
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
ENV SQUADHQ_MODE=worker
CMD ["worker"]

# The default target stays the plain runtime image.
FROM runtime
