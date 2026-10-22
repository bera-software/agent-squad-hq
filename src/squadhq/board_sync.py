#!/usr/bin/env python3
"""
Sync Projects v2 "Status" fields from status:* labels (and agent:implement).
  python -m squadhq.board_sync discover [--repo owner/repo]
  python -m squadhq.board_sync sync --repo <owner/repo> --issue-number <N> --state <todo|in_progress|in_review|done>
  python -m squadhq.board_sync backfill [--repo owner/repo]

Boards can be owned by the org OR linked to a specific repo (a project created
under the repo's own Projects tab) — discover() searches both and merges the
results. Board/field/option ids are resolved fresh via GraphQL on every call,
never hardcoded (boards get rebuilt).

The org is BOARD_ORG, else squadhq.settings.org(); BOARD_TITLE picks one board
when several are found (empty = auto-detect a single board). Both are read at
call time, never at import.

Requires env GH_TOKEN with Organization Projects read/write, minted per-run
from the squadhq-ops GitHub App.

Importable: other modules call sync(repo, issue_number, state) directly.
discover() and sync() raise on failure — they never call sys.exit() — so a
caller can catch the exception without its own process being killed.
"""
import argparse
import json
import os
import sys

from squadhq import github, settings
from squadhq.paths import hub_path

BACKFILL_LIMIT = 200


def board_org():
    return os.environ.get("BOARD_ORG") or settings.org()


def board_title():
    return os.environ.get("BOARD_TITLE", "")


def _warn(msg):
    print(f"warning: {msg}", file=sys.stderr)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"\n> ⚠ board_sync: {msg}\n")


REPO_PROJECTS_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    projectsV2(first: 20) {
      pageInfo { hasNextPage }
      nodes {
        id
        title
        fields(first: 50) {
          pageInfo { hasNextPage }
          nodes {
            ... on ProjectV2SingleSelectField { id name options { id name } }
          }
        }
      }
    }
  }
}
"""

ORG_PROJECTS_QUERY = """
query($org: String!) {
  organization(login: $org) {
    projectsV2(first: 20) {
      pageInfo { hasNextPage }
      nodes {
        id
        title
        fields(first: 50) {
          pageInfo { hasNextPage }
          nodes {
            ... on ProjectV2SingleSelectField { id name options { id name } }
          }
        }
      }
    }
  }
}
"""

ISSUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id
      projectItems(first: 20) { pageInfo { hasNextPage } nodes { id project { id } } }
    }
  }
}
"""

ADD_ITEM_MUTATION = """
mutation($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
    item { id }
  }
}
"""

SET_STATUS_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId, itemId: $itemId, fieldId: $fieldId,
    value: { singleSelectOptionId: $optionId }
  }) {
    projectV2Item { id }
  }
}
"""


def run_graphql(query, fields=None, raw_fields=None):
    """GraphQL via the shared client. Raises RuntimeError on transport errors
    AND on a populated "errors" array (HTTP 200 with a null field, e.g. a
    missing 'Organization projects: read' permission)."""
    try:
        return github.client().graphql(query, fields, raw_fields)
    except github.GitHubError as e:
        raise RuntimeError(str(e)) from None


def _truncated(conn, what):
    if ((conn or {}).get("pageInfo") or {}).get("hasNextPage"):
        _warn(f"{what}: more results than this query fetches — later ones were not considered")


def _norm(name):
    return name.lower().replace(" ", "").replace("_", "").replace("-", "")


def resolve_option(options, state):
    target = _norm(state)
    for opt in options:
        if _norm(opt["name"]) == target:
            return opt
    return None


def _find_boards(repo=None):
    """Boards linked to `repo` (if given) merged with boards owned by org, deduped by id."""
    boards = {}
    notes = []
    if repo:
        owner, name = repo.split("/")
        data = run_graphql(REPO_PROJECTS_QUERY, fields={"owner": owner, "name": name})
        repository = data["data"].get("repository")
        if repository is None:
            notes.append(f"repository({repo}).projectsV2 came back null — repo not found, or the "
                         f"token can't see it")
        else:
            _truncated(repository["projectsV2"], f"repository({repo}).projectsV2")
            for b in repository["projectsV2"]["nodes"]:
                boards[b["id"]] = b
    org = board_org()
    org_data = run_graphql(ORG_PROJECTS_QUERY, fields={"org": org})
    organization = org_data["data"].get("organization")
    if organization is None:
        notes.append(f"organization({org}).projectsV2 came back null — org not found, or the token "
                     f"lacks the 'Organization projects: read' permission")
    else:
        _truncated(organization["projectsV2"], f"organization({org}).projectsV2")
        for b in organization["projectsV2"]["nodes"]:
            boards[b["id"]] = b
    return list(boards.values()), notes


def discover(repo=None):
    boards, notes = _find_boards(repo)
    org, title = board_org(), board_title()
    if not boards:
        if notes:
            detail = "Likely cause: " + "; ".join(notes) + "."
        else:
            detail = (f"Both organization({org}) and repository({repo}) resolved fine but "
                      f"returned zero projectsV2 — the board genuinely isn't linked to "
                      f"either. Check github.com/orgs/{org}/projects for its actual owner.")
        raise RuntimeError(f"No Projects v2 boards found (repo={repo!r}, org={org!r}). {detail}")
    if title:
        board = next((b for b in boards if b["title"] == title), None)
        if not board:
            titles = [b["title"] for b in boards]
            raise RuntimeError(f"No board titled {title!r} among: {titles}")
    elif len(boards) == 1:
        board = boards[0]
    else:
        titles = [b["title"] for b in boards]
        raise RuntimeError(
            f"Multiple boards found and BOARD_TITLE is unset — set BOARD_TITLE to "
            f"pick one: {titles}"
        )
    _truncated(board.get("fields"), f"board {board['title']!r} fields")
    status_field = next((f for f in board["fields"]["nodes"] if f and f.get("name") == "Status"), None)
    if not status_field:
        raise RuntimeError(f"No single-select 'Status' field found on board {board['title']!r}.")
    return {"project_id": board["id"], "board_title": board["title"],
            "field_id": status_field["id"], "options": status_field["options"]}


def sync(repo, issue_number, state):
    owner, name = repo.split("/")
    info = discover(repo)
    target_opt = resolve_option(info["options"], state)
    if not target_opt:
        names = [o["name"] for o in info["options"]]
        raise RuntimeError(f"No Status option matches {state!r} on board {info['board_title']!r}. Available: {names}")

    issue_data = run_graphql(ISSUE_QUERY, fields={"owner": owner, "name": name}, raw_fields={"number": int(issue_number)})
    repository = issue_data["data"].get("repository")
    issue = repository.get("issue") if repository else None
    if not issue:
        raise RuntimeError(f"Issue {repo}#{issue_number} not found (or repository not visible to the token).")
    issue_id = issue["id"]
    _truncated(issue.get("projectItems"), f"{repo}#{issue_number} projectItems")

    item = next((pi for pi in issue["projectItems"]["nodes"] if pi["project"]["id"] == info["project_id"]), None)
    if item:
        item_id = item["id"]
    else:
        added = run_graphql(ADD_ITEM_MUTATION, fields={"projectId": info["project_id"], "contentId": issue_id})
        item_id = added["data"]["addProjectV2ItemById"]["item"]["id"]

    run_graphql(SET_STATUS_MUTATION, fields={
        "projectId": info["project_id"], "itemId": item_id,
        "fieldId": info["field_id"], "optionId": target_opt["id"],
    })
    print(f"Synced {repo}#{issue_number} -> {target_opt['name']}")


def label_to_state(label_names):
    if "status:in-progress" in label_names:
        return "in_progress"
    if "status:in-review" in label_names:
        return "in_review"
    if "agent:implement" in label_names:
        return "todo"
    return None


def registered_repos():
    with open(hub_path("config", "projects.json")) as f:
        return [p["repo"] for p in json.load(f)["projects"]]


def backfill(repo=None):
    """Walk open issues (one repo, or every registered repo) and sync board Status
    from whatever status:*/agent:implement label is currently on each ticket."""
    repos = [repo] if repo else registered_repos()
    results = []
    for r in repos:
        try:
            items = github.client().list_capped(["issue", "list", "-R", r, "--state", "open",
                                                 "--json", "number,labels"], BACKFILL_LIMIT,
                                                what=f"board backfill {r}")
        except github.GitHubError as e:
            results.append((r, None, None, f"FAILED to list issues: {e.stderr.strip()}"))
            continue
        for it in items or []:
            names = {l["name"] for l in it["labels"]}
            state = label_to_state(names)
            if not state:
                continue
            try:
                sync(r, it["number"], state)
                results.append((r, it["number"], state, "ok"))
            except Exception as e:
                results.append((r, it["number"], state, f"FAILED: {e}"))
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m squadhq.board_sync")
    sub = parser.add_subparsers(dest="mode", required=True)

    dp = sub.add_parser("discover")
    dp.add_argument("--repo")

    sp = sub.add_parser("sync")
    sp.add_argument("--repo", required=True)
    sp.add_argument("--issue-number", required=True, type=int)
    sp.add_argument("--state", required=True, choices=["todo", "in_progress", "in_review", "done"])

    bp = sub.add_parser("backfill")
    bp.add_argument("--repo")

    args = parser.parse_args(argv)
    try:
        if args.mode == "discover":
            print(json.dumps(discover(args.repo), indent=2))
        elif args.mode == "sync":
            sync(args.repo, args.issue_number, args.state)
        else:
            results = backfill(args.repo)
            failed = 0
            for r, n, state, status in results:
                print(f"{r}#{n} -> {state}: {status}")
                if status != "ok":
                    failed += 1
            print(f"backfill: {len(results) - failed} ok / {failed} failed of {len(results)}")
            if failed:
                sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
