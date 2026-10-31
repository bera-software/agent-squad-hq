#!/usr/bin/env bash
# Keeps open PRs current with a moved base branch, in three isolated stages.
#
#   update-prs.sh plan     (privileged job, no repository code runs)
#       GH_TOKEN REPO BRANCH -> PRs with auto-merge armed: update-branch API; human PRs
#       with conflicts: a comment; conflicting bot PRs: written to
#       $CANDIDATES as a JSON list for the merge stage.
#   update-prs.sh merge    (tokenless job, one PR per matrix entry)
#       NUMBER HEAD BASE BUILD_CMDS -> merges origin/BASE into the PR branch
#       (never a rebase), auto-resolves only safe patterns, build-verifies any
#       auto-resolution, and writes $OUT_DIR/result.json + merged.bundle.
#   update-prs.sh publish  (privileged job on a fresh runner)
#       GH_TOKEN REPO BRANCH IN_DIR -> for each result: re-reads the PR from
#       the API, checks the bundle tip descends from the PR's current head and
#       contains the base, then pushes normally (never a force-push).
#
# Safe patterns: graphify-out/** -> the base's version; package-lock.json /
# pnpm-lock.yaml -> regenerated with --ignore-scripts (merge stage only);
# CLAUDE.md, AGENTS.md, docs/lessons.md, CHANGELOG.md -> git merge-file
# --union. Build manifests are never auto-resolved. Human-authored PRs are
# never touched.
set +e +o pipefail
MODE="${1:-}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/stdout}"
WORK="${RUNNER_TEMP:-$(mktemp -d)}/update-prs"
mkdir -p "$WORK"
TRIGGER="${SQUADHQ_TRIGGER:-@squad}"
TODAY=$(date -u +%Y-%m-%d)
ATTEMPT_MARKER="<!-- auto-update-attempt $TODAY -->"

row() { echo "| #$1 | $2 |" >> "$SUMMARY"; }

plan() {
  {
    echo "## update-prs — $REPO"
    echo
    echo "Base pushed: \`$BRANCH\`"
    echo
    echo "| PR | Result |"
    echo "|---|---|"
  } >> "$SUMMARY"
  local prs out
  prs=$(gh pr list -R "$REPO" --base "$BRANCH" --state open --limit 100 \
    --json number,headRefName,headRefOid,author,baseRefName,labels,reviewDecision,autoMergeRequest,isDraft,isCrossRepository)
  echo "[]" > "$CANDIDATES"
  [ "$prs" = "[]" ] && { echo "| — | no open PRs target \`$BRANCH\` |" >> "$SUMMARY"; return 0; }
  echo "$prs" | jq -c '.[]' | while read -r pr; do
    local num head approved m n attempts
    num=$(jq -r .number <<< "$pr"); head=$(jq -r .headRefName <<< "$pr")
    case "$(jq -r .baseRefName <<< "$pr")" in agent/*|design/*) row "$num" "⏭ stacked PR — left alone"; continue ;; esac
    [ "$(jq -r .isCrossRepository <<< "$pr")" = true ] && { row "$num" "⏭ fork PR — left to its author"; continue; }
    # Only a PR that merges itself (auto-merge armed) is brought up to date on
    # every base move: under a strict ruleset it cannot merge otherwise. A PR
    # waiting for a person is updated once, when that person merges (GitHub's
    # "Update branch"), so its CI is not re-run after every merge to the base
    # while it waits; conflicts are still handled below.
    approved=$(jq -r '.autoMergeRequest != null' <<< "$pr")
    if [ "$approved" = true ]; then
      if out=$(gh api -X PUT "repos/$REPO/pulls/$num/update-branch" 2>&1); then row "$num" "✅ updated (auto-merge armed)"; continue; fi
      printf '%s' "$out" | grep -qi "up to date" && { row "$num" "⏭ already up to date"; continue; }
    else
      m=""
      for _ in 1 2 3 4; do
        m=$(gh api "repos/$REPO/pulls/$num" --jq .mergeable 2>/dev/null)
        { [ "$m" = true ] || [ "$m" = false ]; } && break
        sleep 5
      done
      [ "$m" != false ] && { row "$num" "⏭ no conflict — updated when it merges (auto-merge or the approver's merge)"; continue; }
      [ "$(jq -r .isDraft <<< "$pr")" = true ] && { row "$num" "⏭ draft with conflicts — left to its author"; continue; }
    fi
    # Only PRs our own Apps authored are updated automatically (gh reports Apps
    # as app/<slug>); everything else is treated as human-authored.
    author=$(jq -r .author.login <<< "$pr")
    ops="${SQUADHQ_BOT_LOGIN:-squadhq-ops[bot]}"; agent="${SQUADHQ_AGENT_BOT_LOGIN:-squadhq-agent[bot]}"
    ours=false
    if [ "$(jq -r .author.is_bot <<< "$pr")" = true ]; then
      case "$author" in "app/${ops%\[bot\]}"|"app/${agent%\[bot\]}"|"$ops"|"$agent") ours=true ;; esac
    fi
    if [ "$ours" != true ]; then
      local marker="Base moved and this branch conflicts"
      n=$(gh api "repos/$REPO/issues/$num/comments" --paginate --jq ".[] | select(.body | contains(\"$marker\")) | .id" | wc -l)
      [ "$n" -eq 0 ] && gh pr comment "$num" -R "$REPO" -b "$marker — this PR is human-authored, so it is not updated automatically. Run \`git fetch origin $BRANCH && git merge origin/$BRANCH\` and push, or comment \`$TRIGGER update this branch from $BRANCH\`." > /dev/null 2>&1
      row "$num" "⚠ conflicted (human-authored, not auto-updated)"
      continue
    fi
    attempts=$(gh api "repos/$REPO/issues/$num/comments" --paginate \
      --jq ".[] | select(.body | contains(\"$ATTEMPT_MARKER\")) | .id" 2>/dev/null | wc -l)
    [ "${attempts:-0}" -ge 3 ] && { row "$num" "⚠ conflicted (3 auto-update attempts today)"; continue; }
    jq --argjson n "$num" --arg h "$head" '. + [{"number": $n, "head": $h}]' "$CANDIDATES" > "$CANDIDATES.tmp" && mv "$CANDIDATES.tmp" "$CANDIDATES"
    row "$num" "🔀 conflicting bot PR — merge attempted in an isolated job"
  done
  return 0
}

try_resolve_file() {  # echoes "1|label" when handled, "0|file" otherwise
  local f="$1" dir
  case "$f" in
    graphify-out/*)
      git checkout --theirs -- "$f" > /dev/null 2>&1 && git add "$f" && { echo "1|$f(base)"; return; } ;;
    package-lock.json|*/package-lock.json)
      dir=$(dirname "$f"); rm -f "$f"
      (cd "$dir" && npm install --package-lock-only --ignore-scripts --silent > /dev/null 2>&1) \
        && git add "$f" > /dev/null 2>&1 && { echo "1|$f(regenerated)"; return; } ;;
    pnpm-lock.yaml|*/pnpm-lock.yaml)
      dir=$(dirname "$f"); rm -f "$f"
      (cd "$dir" && corepack enable > /dev/null 2>&1; pnpm install --lockfile-only --ignore-scripts > /dev/null 2>&1) \
        && git add "$f" > /dev/null 2>&1 && { echo "1|$f(regenerated)"; return; } ;;
    CLAUDE.md|AGENTS.md|docs/lessons.md|CHANGELOG.md)
      if git show ":1:$f" > "$WORK/base" 2>/dev/null && git show ":2:$f" > "$WORK/ours" 2>/dev/null \
         && git show ":3:$f" > "$WORK/theirs" 2>/dev/null; then
        git merge-file --union "$WORK/ours" "$WORK/base" "$WORK/theirs" > /dev/null 2>&1
        cp "$WORK/ours" "$f" && git add "$f" && { echo "1|$f(union — review recommended)"; return; }
      fi ;;
  esac
  echo "0|$f"
}

result() {  # result <status> <files> <head sha>
  jq -n --argjson n "$NUMBER" --arg head "$PR_HEAD" --arg status "$1" --arg files "$2" --arg sha "$3" \
    '{number: $n, head: $head, status: $status, files: $files, head_sha: $sha}' > "$OUT_DIR/result.json"
}

merge() {
  mkdir -p "$OUT_DIR"
  git config user.name "${SQUADHQ_BOT_LOGIN:-squadhq-ops[bot]}"
  git config user.email "${SQUADHQ_BOT_LOGIN:-squadhq-ops[bot]}@users.noreply.github.com"
  git fetch -q origin "$BASE" || { result error "" ""; return 0; }
  local head_sha resolved="" unresolved="" r f
  head_sha=$(git rev-parse HEAD)
  if ! git merge --no-edit "origin/$BASE" > "$WORK/merge.log" 2>&1; then
    for f in $(git diff --name-only --diff-filter=U); do
      r=$(try_resolve_file "$f")
      if [ "${r%%|*}" = 1 ]; then resolved="${resolved}${resolved:+;}${r#*|}"; else unresolved="${unresolved}${unresolved:+,}${r#*|}"; fi
    done
    if [ -n "$unresolved" ] || [ -z "$resolved" ]; then
      git merge --abort > /dev/null 2>&1; result aborted "${unresolved:-merge-failed}" "$head_sha"; return 0
    fi
    git commit -q --no-edit || { git merge --abort > /dev/null 2>&1; result aborted commit-failed "$head_sha"; return 0; }
    # Build-verify the auto-resolution. This job holds no token.
    local root cmd
    while IFS=$'\t' read -r root cmd; do
      [ -n "$cmd" ] || continue
      if ! (cd "$root" && bash -ec "$cmd") > "$WORK/build.log" 2>&1; then
        result build-failed "$resolved" "$head_sha"; return 0
      fi
    done < <(printf '%s' "${BUILD_CMDS:-[]}" | jq -r '.[] | [(.root // "."), ([.install, .build] | map(select(. != null and . != "")) | join(" && "))] | @tsv')
  fi
  git bundle create "$OUT_DIR/merged.bundle" "$head_sha..HEAD" > /dev/null 2>&1 \
    || { result error "bundle" "$head_sha"; return 0; }
  git rev-parse HEAD > "$OUT_DIR/tip"
  result merged "$resolved" "$head_sha"
}

publish() {
  {
    echo "## update-prs — publish"
    echo
    echo "| PR | Result |"
    echo "|---|---|"
  } >> "$SUMMARY"
  local d num status files head_sha info cur_ref cur_sha tip
  for d in "$IN_DIR"/*/; do
    [ -f "$d/result.json" ] || continue
    # The artifact is data from a tokenless job: its directory name, the plan
    # job's candidate list and result.json must all agree on the PR number.
    dnum=$(basename "$d"); dnum="${dnum#squadhq-update-}"
    num=$(jq -r '.number' "$d/result.json"); status=$(jq -r '.status' "$d/result.json")
    if ! [[ "$dnum" =~ ^[0-9]+$ ]] || [ "$num" != "$dnum" ] \
       || ! printf '%s' "${CANDIDATES_JSON:-[]}" | jq -e --argjson n "$dnum" 'any(.[]; .number == $n)' > /dev/null 2>&1; then
      row "${dnum:-?}" "⚠ artifact does not match a planned candidate — skipped"
      echo "::warning::update-prs: ignoring artifact $(basename "$d") (not a planned candidate / number mismatch)"
      continue
    fi
    files=$(jq -r '.files' "$d/result.json" | tr -cd 'A-Za-z0-9_./:;,() —-' | head -c 400)
    [[ "$num" =~ ^[0-9]+$ ]] || continue
    # The PR's real head comes from the API, never from the artifact.
    info=$(gh api "repos/$REPO/pulls/$num" --jq '"\(.head.ref) \(.head.sha) \(.head.repo.full_name) \(.base.ref)"' 2>/dev/null) || { row "$num" "⚠ PR unreadable"; continue; }
    read -r cur_ref cur_sha cur_repo cur_base <<< "$info"
    if [ "$cur_repo" != "$REPO" ] || [ "$cur_base" != "$BRANCH" ] || [ "$cur_ref" = "$BRANCH" ]; then row "$num" "⚠ PR changed — skipped"; continue; fi
    case "$status" in
      merged)
        head_sha=$(jq -r '.head_sha' "$d/result.json")
        if [ "$head_sha" != "$cur_sha" ]; then row "$num" "⏭ branch moved since the merge — next push retries"; continue; fi
        tip=$(tr -cd '0-9a-f' < "$d/tip" | head -c 40)
        git fetch -q origin "$cur_ref" "$BRANCH" || { row "$num" "⚠ fetch failed"; continue; }
        if ! git bundle verify "$d/merged.bundle" > /dev/null 2>&1 || ! git fetch -q "$d/merged.bundle" "+HEAD:refs/squadhq/pr-$num"; then
          row "$num" "⚠ invalid bundle"; continue
        fi
        if [ "$(git rev-parse "refs/squadhq/pr-$num")" != "$tip" ] \
           || ! git merge-base --is-ancestor "$cur_sha" "$tip" \
           || ! git merge-base --is-ancestor "origin/$BRANCH" "$tip"; then
          row "$num" "❌ bundle does not extend the PR head with the base — not pushed"; continue
        fi
        if git push -q origin "$tip:refs/heads/$cur_ref"; then
          gh pr comment "$num" -R "$REPO" -b "$ATTEMPT_MARKER"$'\n'"🔀 Merged \`$BRANCH\` into this branch${files:+; auto-resolved: $files (build verified)}." > /dev/null 2>&1
          row "$num" "✅ base merged${files:+ ($files)}"
        else
          row "$num" "⚠ push rejected (concurrent push)"
        fi ;;
      build-failed)
        gh pr comment "$num" -R "$REPO" -b "$ATTEMPT_MARKER"$'\n'"Merging \`$BRANCH\` resolved conflicts ($files) but the build failed afterwards, so nothing was pushed. Comment \`$TRIGGER update this branch from $BRANCH\` or resolve locally." > /dev/null 2>&1
        gh pr edit "$num" -R "$REPO" --add-label needs-human > /dev/null 2>&1
        row "$num" "❌ build failed after auto-resolve — not pushed" ;;
      aborted)
        gh pr comment "$num" -R "$REPO" -b "$ATTEMPT_MARKER"$'\n'"Merging \`$BRANCH\` stopped: these files need judgment: $files. Comment \`$TRIGGER update this branch from $BRANCH\` or resolve locally." > /dev/null 2>&1
        gh pr edit "$num" -R "$REPO" --add-label needs-human > /dev/null 2>&1
        row "$num" "⚠ conflicted (needs-human)" ;;
      *) row "$num" "⚠ merge stage error" ;;
    esac
  done
  return 0
}

case "$MODE" in
  plan) plan ;;
  merge) merge ;;
  publish) publish ;;
  *) echo "usage: update-prs.sh plan|merge|publish" >&2; exit 2 ;;
esac
exit 0
