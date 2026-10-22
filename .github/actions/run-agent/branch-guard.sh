#!/usr/bin/env bash
# Push guard for the edit-test-push profile (defence in depth — the branch
# ruleset on the default branch is the real control).
#
#   GH_TOKEN        agent token; the gh credential helper reads it at push time
#   PUSH_BRANCHES   comma-separated entries: "agent/" or "agent/*" = prefix,
#                   anything else = that exact branch, "none" = no pushes
#   DEFAULT_BRANCH  the repository's default branch, never pushable
#   PUSH_CREDENTIALS "true": let `git push` authenticate with GH_TOKEN (push profile only)
#
# Installs a global pre-push hook that checks every remote ref being pushed,
# and a `git` wrapper on PATH that refuses the flags that would skip the hook
# or run arbitrary commands through git itself.
set -euo pipefail

hooks="$RUNNER_TEMP/squadhq-hooks"
bin="$RUNNER_TEMP/squadhq-bin"
mkdir -p "$hooks" "$bin"
printf '%s\n' "${DEFAULT_BRANCH:-main}" > "$hooks/default-branch"
printf '%s\n' "${PUSH_BRANCHES:-none}" > "$hooks/allowed-branches"
# Interpreter captured now, before the agent runs.
command -v python > "$hooks/python" || true
command -v gitleaks > "$hooks/gitleaks" || true

cat > "$hooks/pre-push" <<'HOOK'
#!/usr/bin/env bash
dir=$(dirname "$0")
default=$(cat "$dir/default-branch")
allowed=$(cat "$dir/allowed-branches")
zero=0000000000000000000000000000000000000000
while read -r _lref lsha rref _rsha; do
  case "$rref" in
    refs/heads/*) b="${rref#refs/heads/}" ;;
    *) echo "squadhq branch guard: only branch pushes are allowed ($rref)" >&2; exit 1 ;;
  esac
  if [ "$lsha" = "$zero" ]; then
    echo "squadhq branch guard: deleting branches is not allowed ($b)" >&2; exit 1
  fi
  if [ "$b" = "$default" ]; then
    echo "squadhq branch guard: pushing to the default branch '$default' is not allowed — open a PR" >&2; exit 1
  fi
  # Never rewrite history: a push must fast-forward the remote branch.
  if [ "$_rsha" != "$zero" ] && ! git merge-base --is-ancestor "$_rsha" "$lsha" 2>/dev/null; then
    echo "squadhq branch guard: '$b' is not a fast-forward — merge origin/<base> instead of rebasing or force-pushing" >&2; exit 1
  fi
  ok=false
  IFS=',' read -ra entries <<< "$allowed"
  for p in "${entries[@]}"; do
    case "$p" in
      ""|none) ;;
      */) [[ "$b" == "$p"* ]] && ok=true ;;
      *\*) [[ "$b" == "${p%\*}"* ]] && ok=true ;;
      *) [ "$b" = "$p" ] && ok=true ;;
    esac
  done
  if [ "$ok" != true ]; then
    echo "squadhq branch guard: branch '$b' is not pushable in this run (allowed: $allowed)" >&2; exit 1
  fi
  # Secret scan of what is leaving: the diff and the commit messages.
  if [ "$_rsha" = "$zero" ]; then range="$(git merge-base "$lsha" "origin/$default" 2>/dev/null || echo "")..$lsha"; else range="$_rsha..$lsha"; fi
  [ "${range%%..*}" = "" ] && range="$lsha"
  scan=$(mktemp)
  { git log --format='%B' "$range"; git diff "${range%%..*}" "$lsha" 2>/dev/null || git show "$lsha"; } > "$scan" 2>/dev/null
  py=$(cat "$dir/python" 2>/dev/null)
  if [ -n "$py" ] && "$py" -P -m squadhq.redact --help > /dev/null 2>&1; then
    if ! "$py" -P -m squadhq.redact check < "$scan"; then
      rm -f "$scan"; echo "squadhq branch guard: possible secret in the outgoing commits — push refused" >&2; exit 1
    fi
  elif [ -s "$dir/gitleaks" ]; then
    if ! "$(cat "$dir/gitleaks")" git --no-banner --redact --log-opts="$range" . > /dev/null 2>&1; then
      rm -f "$scan"; echo "squadhq branch guard: gitleaks found a secret in the outgoing commits — push refused" >&2; exit 1
    fi
  else
    # Fail closed: an unscanned push is not allowed.
    rm -f "$scan"; echo "squadhq branch guard: no secret scanner available — push refused" >&2; exit 1
  fi
  rm -f "$scan"
done
exit 0
HOOK
chmod +x "$hooks/pre-push"

real_git=$(command -v git)
printf '%s\n' "$real_git" > "$bin/.real-git"
cat > "$bin/git" <<'WRAP'
#!/usr/bin/env bash
real=$(cat "$(dirname "$0")/.real-git")
is_push=false
for a in "$@"; do [ "$a" = push ] && is_push=true; done
for a in "$@"; do
  case "$a" in
    --no-verify|-c|--config-env|--config-env=*|--exec-path|--exec-path=*|--receive-pack|--receive-pack=*|--exec|--exec=*|--upload-pack|--upload-pack=*|*core.hooksPath*|*core.hookspath*)
      echo "squadhq branch guard: git option '$a' is not allowed in agent runs" >&2; exit 1 ;;
    -f|--force|--force=*|--force-with-lease|--force-with-lease=*|--force-if-includes|--mirror|--delete|-d|--prune|+*)
      if [ "$is_push" = true ]; then
        echo "squadhq branch guard: '$a' is not allowed — never force-push; merge origin/<base> and push normally" >&2; exit 1
      fi ;;
  esac
done
if [ "$is_push" = true ]; then
  # At most one refspec, and no multi-ref options: the hook then sees exactly
  # the one ref this run may update.
  seen_push=false; positional=0
  for a in "$@"; do
    if [ "$seen_push" = false ]; then [ "$a" = push ] && seen_push=true; continue; fi
    case "$a" in
      --all|--tags|--mirror|--follow-tags|--prune|--delete|-d) echo "squadhq branch guard: 'git push $a' is not allowed" >&2; exit 1 ;;
      -*) ;;
      *) positional=$((positional + 1)) ;;
    esac
  done
  if [ "$positional" -gt 2 ]; then
    echo "squadhq branch guard: push exactly one refspec (git push origin HEAD:<branch>)" >&2; exit 1
  fi
fi
exec "$real" "$@"
WRAP
chmod +x "$bin/git"
echo "$bin" >> "$GITHUB_PATH"

git config --global core.hooksPath "$hooks"
if [ "${PUSH_CREDENTIALS:-false}" = true ]; then gh auth setup-git; fi
echo "Git guard: push targets: ${PUSH_BRANCHES:-none}" >> "$GITHUB_STEP_SUMMARY"
