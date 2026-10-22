"""Linear tracker (GraphQL API).

Env: ``LINEAR_API_KEY`` (personal API key, sent as ``Authorization: <key>``;
an OAuth token may be passed as ``"Bearer <token>"``), optional
``LINEAR_API_URL`` (default ``https://api.linear.app/graphql``).
``project`` is the team key (``ENG``), ``key`` the identifier (``ENG-42``).

Workflow-state flow labels move the issue between workflow states; other
labels are Linear issue labels (created on the team when missing).
Association: a human creator is ``MEMBER`` (Linear has no outside
contributors in a workspace); integration-created issues are ``BOT``.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from squadhq import http
from squadhq.interfaces import AuthorAssociation, Comment, FatalError, Ticket, TransientError
from squadhq.trackers import status_maps as sm
from squadhq.trackers._common import dedupe, env, require, warn

ISSUE_FIELDS = """
  id identifier title description url createdAt updatedAt
  state { name type }
  labels(first: 100) { nodes { id name } }
  creator { name email }
  assignee { name email }
  team { key }
"""
CLOSED_TYPES = ["completed", "canceled"]


def _person(u: Mapping[str, Any] | None) -> str:
    if not u:
        return ""
    return str(u.get("email") or u.get("name") or "")


class LinearTracker:
    name = "linear"

    def __init__(self, api_key: str | None = None, *, api_url: str | None = None,
                 status_overrides: Mapping[str, Any] | None = None,
                 transport: http.Transport | None = None, timeout: float = 30.0) -> None:
        self.api_key = api_key or env("LINEAR_API_KEY")
        self.api_url = api_url or env("LINEAR_API_URL") or "https://api.linear.app/graphql"
        self.overrides = status_overrides
        self.transport = transport
        self.timeout = timeout
        self._teams: dict[str, dict[str, Any]] = {}

    def gql(self, query: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        key = require(self.api_key, "Linear API key", "LINEAR_API_KEY")
        resp = http.request("POST", self.api_url, headers={"Authorization": key},
                            json_body={"query": query, "variables": dict(variables or {})},
                            timeout=self.timeout, transport=self.transport)
        data = resp.json() or {}
        errors = data.get("errors")
        if errors:
            codes = {str((e.get("extensions") or {}).get("code", "")).upper() for e in errors}
            msg = "; ".join(str(e.get("message")) for e in errors)
            if "RATELIMITED" in codes:
                raise TransientError(f"Linear rate limited: {msg}", status=429)
            raise FatalError(f"Linear GraphQL error: {msg}", status=resp.status)
        return dict(data.get("data") or {})

    # -- helpers -----------------------------------------------------------
    def _ticket(self, it: Mapping[str, Any]) -> Ticket:
        state = it.get("state") or {}
        native = str(state.get("name") or "") or None
        creator = it.get("creator")
        assoc: AuthorAssociation = "MEMBER" if creator else "BOT"
        labels = [str(n.get("name")) for n in (it.get("labels") or {}).get("nodes") or []]
        ident = str(it.get("identifier") or "")
        return Ticket(
            key=ident,
            project=str((it.get("team") or {}).get("key") or ident.split("-")[0]),
            title=str(it.get("title") or ""),
            body=str(it.get("description") or ""),
            state="closed" if state.get("type") in CLOSED_TYPES else "open",
            labels=dedupe(labels + sm.native_to_labels("linear", native, self.overrides)),
            author=_person(creator),
            association=assoc,
            url=str(it.get("url") or ""),
            created_at=str(it.get("createdAt") or ""),
            updated_at=str(it.get("updatedAt") or ""),
            native_state=native,
            assignees=[_person(it["assignee"])] if it.get("assignee") else [],
        )

    def team(self, team_key: str) -> dict[str, Any]:
        if team_key not in self._teams:
            q = """query($key: String!) { teams(filter: {key: {eq: $key}}) { nodes {
                     id key states(first: 100) { nodes { id name type } }
                     labels(first: 250) { nodes { id name } } } } }"""
            nodes = (self.gql(q, {"key": team_key}).get("teams") or {}).get("nodes") or []
            if not nodes:
                raise FatalError(f"Linear team {team_key!r} not found")
            self._teams[team_key] = nodes[0]
        return self._teams[team_key]

    def _state_id(self, team_key: str, names: list[str]) -> str | None:
        states = (self.team(team_key).get("states") or {}).get("nodes") or []
        hit = sm.match_name(names, [str(s["name"]) for s in states])
        return next((str(s["id"]) for s in states if s["name"] == hit), None) if hit else None

    def _label_ids(self, team_key: str, names: list[str], *, create: bool) -> list[str]:
        team = self.team(team_key)
        nodes: list[dict[str, Any]] = (team.get("labels") or {}).setdefault("nodes", [])
        out: list[str] = []
        for name in names:
            hit = next((n for n in nodes if str(n["name"]).lower() == name.lower()), None)
            if hit is None and create:
                q = """mutation($input: IssueLabelCreateInput!) { issueLabelCreate(input: $input) {
                         success issueLabel { id name } } }"""
                hit = (self.gql(q, {"input": {"name": name, "teamId": team["id"]}})
                       .get("issueLabelCreate") or {}).get("issueLabel")
                if hit:
                    nodes.append(hit)
            if hit:
                out.append(str(hit["id"]))
        return out

    def _issue_update(self, key: str, payload: dict[str, Any]) -> None:
        q = "mutation($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, input: $input) { success } }"
        res = self.gql(q, {"id": key, "input": payload}).get("issueUpdate") or {}
        if not res.get("success"):
            raise FatalError(f"Linear issueUpdate {key} failed")

    # -- IssueTracker ------------------------------------------------------
    def list_tickets(self, project: str, *, state: Literal["open", "closed", "all"] = "open",
                     labels: Iterable[str] = (), limit: int = 500) -> list[Ticket]:
        flt: dict[str, Any] = {"team": {"key": {"eq": project}}}
        if state == "open":
            flt["state"] = {"type": {"nin": CLOSED_TYPES}}
        elif state == "closed":
            flt["state"] = {"type": {"in": CLOSED_TYPES}}
        state_labels, plain = sm.split_labels("linear", labels, self.overrides)
        ands: list[dict[str, Any]] = [{"labels": {"some": {"name": {"eqIgnoreCase": lb}}}} for lb in plain]
        for lb in state_labels:
            ands.append({"state": {"name": {"in": sm.candidate_states("linear", lb, self.overrides)}}})
        if ands:
            flt["and"] = ands
        q = f"""query($filter: IssueFilter, $first: Int!, $after: String) {{
                  issues(filter: $filter, first: $first, after: $after) {{
                    nodes {{ {ISSUE_FIELDS} }} pageInfo {{ hasNextPage endCursor }} }} }}"""
        out: list[Ticket] = []
        after: str | None = None
        while len(out) < limit:
            page = self.gql(q, {"filter": flt, "first": min(100, limit - len(out)), "after": after}).get("issues") or {}
            out.extend(self._ticket(n) for n in page.get("nodes") or [])
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return out[:limit]
            after = info.get("endCursor")
        warn(f"Linear list stopped at limit={limit}; more results exist")
        return out[:limit]

    def get_ticket(self, project: str, key: str) -> Ticket:
        q = f"query($id: String!) {{ issue(id: $id) {{ {ISSUE_FIELDS} }} }}"
        it = self.gql(q, {"id": key}).get("issue")
        if not it:
            raise FatalError(f"Linear issue {key} not found", status=404)
        return self._ticket(it)

    def list_comments(self, project: str, key: str) -> list[Comment]:
        q = """query($id: String!, $after: String) { issue(id: $id) { comments(first: 100, after: $after) {
                 nodes { id body createdAt user { name email } botActor { name } }
                 pageInfo { hasNextPage endCursor } } } }"""
        out: list[Comment] = []
        after: str | None = None
        while True:
            issue = self.gql(q, {"id": key, "after": after}).get("issue") or {}
            page = issue.get("comments") or {}
            for c in page.get("nodes") or []:
                user = c.get("user")
                assoc: AuthorAssociation = "MEMBER" if user else "BOT"
                author = _person(user) or str((c.get("botActor") or {}).get("name") or "")
                out.append(Comment(id=str(c.get("id")), author=author, association=assoc,
                                   body=str(c.get("body") or ""), created_at=str(c.get("createdAt") or "")))
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return out
            after = info.get("endCursor")

    def create_ticket(self, project: str, title: str, body: str, labels: Iterable[str] = ()) -> Ticket:
        state_labels, plain = sm.split_labels("linear", labels, self.overrides)
        payload: dict[str, Any] = {"teamId": self.team(project)["id"], "title": title, "description": body}
        if plain:
            payload["labelIds"] = self._label_ids(project, plain, create=True)
        if state_labels:
            sid = self._state_id(project, sm.candidate_states("linear", state_labels[-1], self.overrides))
            if sid:
                payload["stateId"] = sid
        q = f"""mutation($input: IssueCreateInput!) {{ issueCreate(input: $input) {{
                  success issue {{ {ISSUE_FIELDS} }} }} }}"""
        res = self.gql(q, {"input": payload}).get("issueCreate") or {}
        if not res.get("success") or not res.get("issue"):
            raise FatalError("Linear issueCreate failed")
        return self._ticket(res["issue"])

    def comment(self, project: str, key: str, body: str) -> Comment:
        q = """mutation($input: CommentCreateInput!) { commentCreate(input: $input) {
                 success comment { id body createdAt user { name email } } } }"""
        c = (self.gql(q, {"input": {"issueId": key, "body": body}}).get("commentCreate") or {}).get("comment") or {}
        return Comment(id=str(c.get("id", "")), author=_person(c.get("user")), association="BOT",
                       body=str(c.get("body") or body), created_at=str(c.get("createdAt") or ""))

    def add_labels(self, project: str, key: str, labels: Iterable[str]) -> None:
        state_labels, plain = sm.split_labels("linear", labels, self.overrides)
        payload: dict[str, Any] = {}
        if plain:
            payload["addedLabelIds"] = self._label_ids(project, plain, create=True)
        if state_labels:
            sid = self._state_id(project, sm.candidate_states("linear", state_labels[-1], self.overrides))
            if sid is None:
                raise FatalError(f"Linear team {project}: no workflow state for {state_labels[-1]!r}")
            payload["stateId"] = sid
        if payload:
            self._issue_update(key, payload)

    def remove_labels(self, project: str, key: str, labels: Iterable[str]) -> None:
        state_labels, plain = sm.split_labels("linear", labels, self.overrides)
        payload: dict[str, Any] = {}
        if plain:
            ids = self._label_ids(project, plain, create=False)
            if ids:
                payload["removedLabelIds"] = ids
        if state_labels:
            current = self.get_ticket(project, key)
            implied = sm.native_to_labels("linear", current.native_state, self.overrides)
            target = sm.special_state("linear", "open", self.overrides)
            if target and any(lb in implied for lb in state_labels):
                sid = self._state_id(project, [target])
                if sid:
                    payload["stateId"] = sid
        if payload:
            self._issue_update(key, payload)

    def close_ticket(self, project: str, key: str,
                     reason: Literal["completed", "not_planned"] = "completed") -> None:
        want = sm.special_state("linear", "done" if reason == "completed" else "not_planned", self.overrides)
        names = [want] if want else []
        sid = self._state_id(project, names)
        if sid is None:
            ntype = "completed" if reason == "completed" else "canceled"
            states = (self.team(project).get("states") or {}).get("nodes") or []
            sid = next((str(s["id"]) for s in states if s.get("type") == ntype), None)
        if sid is None:
            raise FatalError(f"Linear team {project}: no closing workflow state")
        self._issue_update(key, {"stateId": sid})
