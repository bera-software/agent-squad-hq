#!/usr/bin/env bash
# Agent Squad HQ — one-command self-host installer (docker compose, single VM).
#
#   ./deploy/install.sh                 # interactive
#   ./deploy/install.sh --yes           # accept defaults / read answers from env
#   ./deploy/install.sh --dry-run       # print the plan only: nothing written, nothing started
#
# Answers can be preset with environment variables:
#   SQUADHQ_HUB_REPO, SQUADHQ_MODE, SQUADHQ_GITHUB_TOKEN, SQUADHQ_PUBLIC_URL, SQUADHQ_IMAGE,
#   SQUADHQ_WEBHOOK_SECRET_TRELLO (your Trello app secret, if you use Trello)
#
# Writes deploy/.env (mode 0600) and deploy/squadhq.yml (if missing), generates
# the database password, webhook secrets and an admin API token with openssl,
# runs `docker compose up -d`, waits for /readyz and runs `squadhq server check`.
# Nothing is sent anywhere.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
CONFIG_FILE="${SCRIPT_DIR}/squadhq.yml"
ASSUME_YES=0
DRY_RUN=0
FORCE=0
READY_TRIES="${SQUADHQ_READY_TRIES:-60}"

usage() {
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
  echo
  echo "Options: --yes  --dry-run  --force (overwrite an existing .env)  -h|--help"
}

for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --force) FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

need() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required but not installed. $2"
}

ask() {
  # ask VAR "Question" "default"
  local var="$1" question="$2" default="${3:-}" current answer
  current="${!var:-}"
  if [ -n "$current" ]; then
    printf -v "$var" '%s' "$current"
    return
  fi
  if [ "$ASSUME_YES" -eq 1 ] || [ ! -t 0 ]; then
    printf -v "$var" '%s' "$default"
    return
  fi
  read -r -p "$question [${default}]: " answer
  printf -v "$var" '%s' "${answer:-$default}"
}

ask_secret() {
  local var="$1" question="$2" current answer
  current="${!var:-}"
  if [ -n "$current" ] || [ "$ASSUME_YES" -eq 1 ] || [ ! -t 0 ]; then
    printf -v "$var" '%s' "$current"
    return
  fi
  read -r -s -p "$question (input hidden, empty to skip): " answer
  echo
  printf -v "$var" '%s' "$answer"
}

rand_hex() { openssl rand -hex "$1"; }
sha256() { printf '%s' "$1" | openssl dgst -sha256 | awk '{print $NF}'; }

# --- preflight -------------------------------------------------------------
info "Checking prerequisites"
need docker "Install Docker Engine: https://docs.docker.com/engine/install/"
need openssl "Install it with your package manager (e.g. apt-get install openssl)."
docker compose version >/dev/null 2>&1 || die "docker compose v2 is required (the 'docker compose' plugin)."
if [ "$DRY_RUN" -eq 0 ]; then
  docker info >/dev/null 2>&1 || die "Cannot talk to the Docker daemon. Is it running, and is your user allowed to use it?"
  if [ -f "$ENV_FILE" ] && [ "$FORCE" -eq 0 ]; then
    die "$ENV_FILE already exists. Re-run with --force to regenerate it (existing secrets will be replaced)."
  fi
fi

# --- questions ---------------------------------------------------------------
PLACEHOLDER_HUB_REPO="your-org/agent-squad-hq"
ask SQUADHQ_HUB_REPO "Hub repository (owner/name)" "$PLACEHOLDER_HUB_REPO"
case "$SQUADHQ_HUB_REPO" in
  */*) ;;
  *) die "Hub repository must look like owner/name" ;;
esac
ask SQUADHQ_MODE "Execution mode: byo-compute (GitHub Actions runners) or worker (containers on this host)" "byo-compute"
case "$SQUADHQ_MODE" in
  byo-compute|worker) ;;
  *) die "Mode must be byo-compute or worker" ;;
esac
ask SQUADHQ_PUBLIC_URL "Public base URL of this server (for webhooks)" "https://hq.example.com"
ask SQUADHQ_IMAGE "Container image" "ghcr.io/${SQUADHQ_HUB_REPO%%/*}/agent-squad-hq:latest"
ask_secret SQUADHQ_GITHUB_TOKEN "GitHub token for the ops identity"
# Trello signs webhooks with your Trello app's API secret (trello.com/power-ups/admin).
# Trello issues it, so it cannot be generated here; without it Trello webhooks stay rejected.
ask_secret SQUADHQ_WEBHOOK_SECRET_TRELLO "Trello app secret (only if you use Trello)"

# Defaults that leave the server unusable are called out, never silently accepted.
NEEDS_CONFIG=()
if [ "$SQUADHQ_HUB_REPO" = "$PLACEHOLDER_HUB_REPO" ]; then
  NEEDS_CONFIG+=("hub repository is the placeholder ${PLACEHOLDER_HUB_REPO}: set SQUADHQ_HUB_REPO in ${ENV_FILE} and hub.repo in ${CONFIG_FILE}")
fi
if [ -z "${SQUADHQ_GITHUB_TOKEN:-}" ]; then
  NEEDS_CONFIG+=("no GitHub token: set SQUADHQ_GITHUB_TOKEN in ${ENV_FILE} (the server cannot reach GitHub or dispatch agent runs without it)")
fi
for problem in ${NEEDS_CONFIG[@]+"${NEEDS_CONFIG[@]}"}; do
  warn "WARNING: $problem"
done

# --- secrets -------------------------------------------------------------------
POSTGRES_PASSWORD="$(rand_hex 24)"
GITHUB_WEBHOOK_SECRET="$(rand_hex 32)"
LINEAR_WEBHOOK_SECRET="$(rand_hex 32)"
JIRA_WEBHOOK_SECRET="$(rand_hex 32)"
GITLAB_WEBHOOK_SECRET="$(rand_hex 32)"
ADMIN_TOKEN="sqh_$(rand_hex 32)"
ADMIN_TOKEN_HASH="$(sha256 "$ADMIN_TOKEN")"

write_env() {
  # write_env FILE — mode 600
  (
    umask 077
    {
      echo "# Generated by deploy/install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Keep this file secret."
      echo "SQUADHQ_IMAGE=${SQUADHQ_IMAGE}"
      echo "SQUADHQ_HTTP_PORT=8080"
      echo "SQUADHQ_HUB_REPO=${SQUADHQ_HUB_REPO}"
      echo "SQUADHQ_ORG=${SQUADHQ_HUB_REPO%%/*}"
      echo "SQUADHQ_MODE=${SQUADHQ_MODE}"
      echo "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}"
      echo "SQUADHQ_API_TOKENS=admin:admin:${ADMIN_TOKEN_HASH}"
      echo "SQUADHQ_WEBHOOK_SECRET_GITHUB=${GITHUB_WEBHOOK_SECRET}"
      echo "SQUADHQ_WEBHOOK_SECRET_LINEAR=${LINEAR_WEBHOOK_SECRET}"
      echo "SQUADHQ_WEBHOOK_SECRET_JIRA=${JIRA_WEBHOOK_SECRET}"
      echo "SQUADHQ_WEBHOOK_SECRET_GITLAB=${GITLAB_WEBHOOK_SECRET}"
      echo "SQUADHQ_WEBHOOK_SECRET_TRELLO=${SQUADHQ_WEBHOOK_SECRET_TRELLO:-}"
      echo "SQUADHQ_TRELLO_CALLBACK_URL=${SQUADHQ_PUBLIC_URL%/}/api/v1/webhooks/trello"
      echo "SQUADHQ_GITHUB_TOKEN=${SQUADHQ_GITHUB_TOKEN:-}"
      echo "SQUADHQ_LOG_FORMAT=json"
      echo "SQUADHQ_METRICS_PUBLIC=false"
    } > "$1"
  )
  chmod 600 "$1"
}

COMPOSE=(docker compose --project-directory "$SCRIPT_DIR" -f "${SCRIPT_DIR}/docker-compose.yml")
if [ "$SQUADHQ_MODE" = "worker" ]; then
  COMPOSE+=(-f "${SCRIPT_DIR}/docker-compose.worker-mode.yml")
fi

# --- dry run: print the plan, touch nothing -----------------------------------
if [ "$DRY_RUN" -eq 1 ]; then
  PLAN_ENV="$(mktemp)"
  trap 'rm -f "$PLAN_ENV"' EXIT
  write_env "$PLAN_ENV"
  "${COMPOSE[@]}" --env-file "$PLAN_ENV" config --quiet
  SERVICES="$("${COMPOSE[@]}" --env-file "$PLAN_ENV" config --services | tr '\n' ' ')"
  if [ -n "${SQUADHQ_WEBHOOK_SECRET_TRELLO:-}" ]; then TRELLO_NOTE="Trello app secret provided"; else TRELLO_NOTE="no Trello secret: Trello webhooks stay rejected"; fi
  if [ -f "$CONFIG_FILE" ]; then CONFIG_NOTE="keep the existing ${CONFIG_FILE}"; else CONFIG_NOTE="create ${CONFIG_FILE} from squadhq.example.yml"; fi
  if [ -f "$ENV_FILE" ] && [ "$FORCE" -eq 0 ]; then ENV_NOTE=" (exists: a real run needs --force)"; else ENV_NOTE=""; fi
  echo
  echo "Dry run: nothing was written or started. A real run would:"
  echo "  1. write ${ENV_FILE} (mode 600)${ENV_NOTE}"
  echo "     hub ${SQUADHQ_HUB_REPO}, mode ${SQUADHQ_MODE}, image ${SQUADHQ_IMAGE}"
  echo "     new Postgres password, GitHub/Linear/Jira/GitLab webhook secrets, an admin API token (${TRELLO_NOTE})"
  echo "  2. ${CONFIG_NOTE}"
  echo "  3. pull ${SQUADHQ_IMAGE} or build it locally, then docker compose up -d"
  echo "     services: ${SERVICES}"
  echo "  4. wait for /readyz (up to $((READY_TRIES * 2)) s), run squadhq server check and print the admin token once"
  for problem in ${NEEDS_CONFIG[@]+"${NEEDS_CONFIG[@]}"}; do
    echo "  needs configuration: $problem"
  done
  echo "Compose configuration: valid."
  exit 0
fi

# --- install -------------------------------------------------------------------
write_env "$ENV_FILE"
info "Wrote $ENV_FILE (mode 600)"

if [ ! -f "$CONFIG_FILE" ]; then
  if [ -f "${SCRIPT_DIR}/../squadhq.example.yml" ]; then
    # The example's hub.repo is the only top-level `  repo:` line; point it at this hub.
    sed -e "s|^  repo: acme/agent-squad-hq\( *\)|  repo: ${SQUADHQ_HUB_REPO}\1|" \
      "${SCRIPT_DIR}/../squadhq.example.yml" > "$CONFIG_FILE"
    info "Copied squadhq.example.yml to $CONFIG_FILE (hub.repo: ${SQUADHQ_HUB_REPO}); edit it before going live"
  else
    printf 'version: 1\norg:\n  name: %s\n  timezone: UTC\nhub:\n  repo: %s\n' \
      "${SQUADHQ_HUB_REPO%%/*}" "$SQUADHQ_HUB_REPO" > "$CONFIG_FILE"
    info "Wrote a minimal $CONFIG_FILE"
  fi
fi
chmod 644 "$CONFIG_FILE"

info "Starting Agent Squad HQ"
if docker pull --quiet "$SQUADHQ_IMAGE" >/dev/null 2>&1; then
  info "Pulled ${SQUADHQ_IMAGE}"
else
  warn "Could not pull ${SQUADHQ_IMAGE}; building it locally"
  # only `api` carries build: (parallel builds of one tag race)
  "${COMPOSE[@]}" build api
fi
if [ "$SQUADHQ_MODE" = "worker" ]; then
  "${COMPOSE[@]}" build worker
fi
"${COMPOSE[@]}" up -d

info "Waiting for the API to become ready"
READY=0
for _ in $(seq 1 "$READY_TRIES"); do
  if "${COMPOSE[@]}" exec -T api python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=3).status == 200 else 1)" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 2
done
if [ "$READY" -ne 1 ]; then
  "${COMPOSE[@]}" ps >&2 || true
  "${COMPOSE[@]}" logs --tail 40 migrate api >&2 || true
  die "The API did not become ready within $((READY_TRIES * 2)) s; see the logs above. After fixing: docker compose --project-directory ${SCRIPT_DIR} up -d"
fi

info "Checking the server configuration (squadhq server check)"
CHECK_OK=1
CHECK_OUT="$("${COMPOSE[@]}" exec -T api python -m squadhq.server check 2>&1)" || CHECK_OK=0
printf '%s\n' "$CHECK_OUT"
if [ "$CHECK_OK" -eq 1 ] && [ "${#NEEDS_CONFIG[@]}" -eq 0 ]; then
  STATUS="Agent Squad HQ is installed and ready."
else
  STATUS="Agent Squad HQ is running, but needs configuration (see the problems above and below)."
  for problem in ${NEEDS_CONFIG[@]+"${NEEDS_CONFIG[@]}"}; do
    warn "$problem"
  done
  [ "$CHECK_OK" -eq 1 ] || warn "squadhq server check failed; fix ${ENV_FILE}, then: docker compose --project-directory ${SCRIPT_DIR} up -d"
fi

cat <<EOF

${STATUS}

  Admin API token (shown once, store it in your password manager):
    ${ADMIN_TOKEN}

  Next steps
  1. Put the API behind TLS (Caddy/nginx/cloud LB) at ${SQUADHQ_PUBLIC_URL}
     The API listens on 127.0.0.1:8080 on this host.
  2. GitHub: add an org webhook to ${SQUADHQ_PUBLIC_URL%/}/api/v1/webhooks/github
     content type application/json, secret = SQUADHQ_WEBHOOK_SECRET_GITHUB in ${ENV_FILE}
     events: issues, issue_comment, pull_request, pull_request_review, check_run, push
     Jira/Linear/GitLab: SQUADHQ_WEBHOOK_SECRET_JIRA / _LINEAR / _GITLAB in the same file.
  3. Register your projects in ${CONFIG_FILE} (events for unregistered repos are dropped), then:
     docker compose --project-directory ${SCRIPT_DIR} restart api worker scheduler
  4. Check it:
     curl -fsS -H "Authorization: Bearer <token>" http://127.0.0.1:8080/api/v1/schedules
     docker compose --project-directory ${SCRIPT_DIR} logs -f worker
  5. Create approver tokens for your leads:
     docker compose --project-directory ${SCRIPT_DIR} exec api python -m squadhq.server create-token --role approver --name lead

  Docs: docs/deploy/docker-compose.md, docs/deploy/security-hardening.md
EOF
