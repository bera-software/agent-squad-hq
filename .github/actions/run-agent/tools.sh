#!/usr/bin/env bash
# Builds the executor's tool allowlist for a tool profile.
#
#   PROFILE  read-only | comment | edit-test | edit-test-push
#   EXTRA    extra comma-separated entries from the caller (role needs)
#   REPO     owner/name, used to ask the packs which build/test commands apply
#
# The profiles are defined once in squadhq.executors.tool_profiles; this
# script adds the packs' build/test tools and the caller's extras, drops any
# extra that module rejects, and writes `allowed=` / `disallowed=` to
# $GITHUB_OUTPUT. Never emits an unrestricted shell entry.
set -euo pipefail
profile="${PROFILE:-read-only}"

declare -a extras=()
add_csv() {
  local t
  IFS=',' read -ra parts <<< "$1"
  for t in "${parts[@]}"; do
    t="${t#"${t%%[![:space:]]*}"}"; t="${t%"${t##*[![:space:]]}"}"
    [ -n "$t" ] && extras+=("$t")
  done
  return 0
}

# Build/test commands: packs first, a small detection fallback otherwise.
pack_tools() {
  local out
  if out=$(python -P -m squadhq.packs tools --repo "$REPO" 2>/dev/null) && [ -n "$out" ]; then
    printf '%s\n' "$out"
    return 0
  fi
  echo "::notice::no pack tools for $REPO — detecting build tools from the checkout" >&2
  if [ -f gradlew ] || compgen -G "*/gradlew" > /dev/null; then echo "Bash(./gradlew *)"; fi
  if [ -f pom.xml ] || compgen -G "*/pom.xml" > /dev/null; then printf '%s\n' "Bash(mvn *)" "Bash(./mvnw *)"; fi
  if [ -f package.json ] || compgen -G "*/package.json" > /dev/null; then
    printf '%s\n' "Bash(npm ci)" "Bash(npm run *)" "Bash(npm test)" "Bash(npm test *)" \
      "Bash(pnpm install *)" "Bash(pnpm run *)" "Bash(pnpm test *)" "Bash(yarn install *)" "Bash(yarn run *)" "Bash(yarn test *)"
  fi
  if [ -f pyproject.toml ] || [ -f setup.py ] || [ -f requirements.txt ]; then
    printf '%s\n' "Bash(pytest *)" "Bash(python -P -m pytest *)"
  fi
  if [ -f go.mod ]; then printf '%s\n' "Bash(go build *)" "Bash(go test *)" "Bash(go vet *)"; fi
  return 0
}

if [ "$profile" = edit-test ] || [ "$profile" = edit-test-push ]; then
  while IFS= read -r t; do [ -n "$t" ] && extras+=("$t"); done < <(pack_tools)
fi
add_csv "${EXTRA:-}"
# Research roles: WebFetch only to configured domains (no open fetch = no
# exfiltration to arbitrary URLs).
if [ -n "${WEBFETCH_DOMAINS:-}" ]; then
  kept=(); for e in ${extras[@]+"${extras[@]}"}; do [ "$e" = WebFetch ] || kept+=("$e"); done; extras=(${kept[@]+"${kept[@]}"})
  IFS=',' read -ra doms <<< "$WEBFETCH_DOMAINS"
  for d in "${doms[@]}"; do
    d="${d//[[:space:]]/}"
    [[ "$d" =~ ^[A-Za-z0-9.-]+$ ]] && extras+=("WebFetch(domain:$d)")
  done
fi

# Role-specific rules (push targets, narrow gh api) when the module supports --role.
role_args=()
if [ -n "${ROLE:-}" ] && python -P -m squadhq.executors.tool_profiles --help 2>/dev/null | grep -q -- '--role'; then
  role_args=(--role "$ROLE")
fi

profile_cli() {  # profile_cli <extra...>
  local args=(--executor claude-code --profile "$profile" ${role_args[@]+"${role_args[@]}"})
  local e
  for e in "$@"; do args+=(--extra "$e"); done
  python -P -m squadhq.executors.tool_profiles "${args[@]}"
}

allowed="" disallowed=""
rc=0
allowed=$(profile_cli "${extras[@]}" 2>/dev/null) || rc=$?
if [ "$rc" = 2 ]; then
  # At least one extra widens the profile beyond policy: keep the others.
  kept=()
  for e in "${extras[@]}"; do
    if profile_cli "$e" > /dev/null 2>&1; then kept+=("$e"); else echo "::warning::tool entry rejected by policy: $e"; fi
  done
  rc=0
  allowed=$(profile_cli "${kept[@]}") || rc=$?
fi
if [ "$rc" = 0 ]; then
  disallowed=$(python -P -m squadhq.executors.tool_profiles --executor claude-code --profile "$profile" ${role_args[@]+"${role_args[@]}"} --disallowed 2>/dev/null || true)
else
  # squadhq.executors.tool_profiles unavailable: a conservative built-in copy.
  echo "::warning::squadhq.executors.tool_profiles unavailable (rc=$rc) — using the built-in read-only/comment lists"
  allowed="Read,Glob,Grep,LS,Bash(git status),Bash(git status *),Bash(git diff *),Bash(git log *),Bash(git show *),Bash(gh issue view *),Bash(gh issue list *),Bash(gh pr view *),Bash(gh pr list *),Bash(gh pr diff *),Bash(gh pr checks *)"
  case "$profile" in
    comment|edit-test|edit-test-push) allowed="$allowed,Bash(gh issue comment *),Bash(gh pr comment *),Bash(gh pr review *)" ;;
  esac
  disallowed="Bash(gh pr merge *),Bash(gh api *),Bash(git push --force *),Bash(git push -f *),Bash(rm -rf *),Bash(curl *),Bash(wget *)"
fi

# Final belt and braces: no entry may re-open the whole shell.
clean=""
IFS=',' read -ra entries <<< "$allowed"
for t in "${entries[@]}"; do
  case "$t" in
    ""|Bash|"Bash(*)"|"Bash( *)"|"Bash(**)") echo "::warning::dropping unrestricted shell entry: '$t'"; continue ;;
    *\"*) echo "::warning::dropping malformed entry: $t"; continue ;;
  esac
  clean="${clean:+$clean,}$t"
done
# TOOLS_OUT: a private file (agent-prep); otherwise this step's outputs.
{
  echo "allowed=$clean"
  echo "disallowed=$disallowed"
} >> "${TOOLS_OUT:-$GITHUB_OUTPUT}"
n=$(printf '%s' "$clean" | tr ',' '\n' | grep -c . || true)
echo "Tool profile \`$profile\`: $n allowed entries" >> "$GITHUB_STEP_SUMMARY"
