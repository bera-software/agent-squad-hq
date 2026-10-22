"""Trello tracker (REST API v1): boards are projects, cards are tickets.

Env: ``TRELLO_API_KEY``, ``TRELLO_TOKEN``; optional ``TRELLO_API_URL``
(default ``https://api.trello.com/1``). ``project`` is the board id,
``key`` the card id. Lists are workflow states (see status maps); card
labels are labels (created on the board when missing). Closing moves the
card to the ``done`` list when the board has one, then archives it.

Association: creating a card requires board membership, so card authors are
``MEMBER``; comment authors are ``MEMBER`` when they are on the board's
member list and ``NONE`` otherwise (public boards can allow outside comments).
"""
from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from squadhq import http
from squadhq.interfaces import AuthorAssociation, Comment, FatalError, Ticket
from squadhq.trackers import status_maps as sm
from squadhq.trackers._common import dedupe, env, require, warn

CARD_FIELDS = "id,idShort,name,desc,closed,idList,idBoard,labels,url,dateLastActivity,idMembers"
CARD_CAP = 1000


def created_from_id(object_id: str) -> str:
    """Trello ids embed the creation time in their first 8 hex digits."""
    try:
        ts = int(object_id[:8], 16)
    except (TypeError, ValueError):
        return ""
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TrelloTracker:
    name = "trello"

    def __init__(self, api_key: str | None = None, token: str | None = None, *, api_url: str | None = None,
                 status_overrides: Mapping[str, Any] | None = None,
                 transport: http.Transport | None = None, timeout: float = 30.0) -> None:
        self.api_key = api_key or env("TRELLO_API_KEY")
        self.token = token or env("TRELLO_TOKEN")
        self.api_url = (api_url or env("TRELLO_API_URL") or "https://api.trello.com/1").rstrip("/")
        self.overrides = status_overrides
        self.transport = transport
        self.timeout = timeout
        self._lists: dict[str, list[dict[str, Any]]] = {}
        self._labels: dict[str, list[dict[str, Any]]] = {}
        self._members: dict[str, set[str]] = {}

    def _req(self, method: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        key = require(self.api_key, "Trello API key", "TRELLO_API_KEY")
        tok = require(self.token, "Trello token", "TRELLO_TOKEN")
        # Trello authenticates with key/token; the header form keeps them out of URLs and logs.
        headers = {"Authorization": f'OAuth oauth_consumer_key="{key}", oauth_token="{tok}"'}
        return http.request(method, f"{self.api_url}/{path.lstrip('/')}", headers=headers, params=params,
                            timeout=self.timeout, transport=self.transport).json()

    # -- board metadata ----------------------------------------------------
    def lists(self, board: str) -> list[dict[str, Any]]:
        if board not in self._lists:
            self._lists[board] = list(self._req("GET", f"boards/{board}/lists", {"fields": "name,closed"}) or [])
        return self._lists[board]

    def board_labels(self, board: str) -> list[dict[str, Any]]:
        if board not in self._labels:
            self._labels[board] = list(self._req("GET", f"boards/{board}/labels",
                                                 {"fields": "name,color", "limit": 1000}) or [])
        return self._labels[board]

    def members(self, board: str) -> set[str]:
        if board not in self._members:
            ms = self._req("GET", f"boards/{board}/members", {"fields": "username"}) or []
            self._members[board] = {str(m.get("id")) for m in ms} | {str(m.get("username")) for m in ms}
        return self._members[board]

    def _list_name(self, board: str, list_id: str | None) -> str | None:
        return next((str(lst["name"]) for lst in self.lists(board) if lst.get("id") == list_id), None)

    def _list_id(self, board: str, names: list[str]) -> str | None:
        open_lists = [lst for lst in self.lists(board) if not lst.get("closed")]
        hit = sm.match_name(names, [str(lst["name"]) for lst in open_lists])
        return next((str(lst["id"]) for lst in open_lists if lst["name"] == hit), None) if hit else None

    def _label_id(self, board: str, name: str, *, create: bool) -> str | None:
        hit = next((lb for lb in self.board_labels(board) if str(lb.get("name", "")).lower() == name.lower()), None)
        if hit is None and create:
            hit = self._req("POST", "labels", {"name": name, "color": "null", "idBoard": board})
            if hit:
                self.board_labels(board).append(hit)
        return str(hit["id"]) if hit else None

    def _ticket(self, board: str, c: Mapping[str, Any]) -> Ticket:
        native = self._list_name(board, c.get("idList"))
        labels = [str(lb.get("name")) for lb in c.get("labels") or [] if lb.get("name")]
        cid = str(c.get("id", ""))
        return Ticket(
            key=cid,
            project=str(c.get("idBoard") or board),
            title=str(c.get("name") or ""),
            body=str(c.get("desc") or ""),
            state="closed" if c.get("closed") else "open",
            labels=dedupe(labels + sm.native_to_labels("trello", native, self.overrides)),
            author="",
            association="MEMBER",
            url=str(c.get("url") or ""),
            created_at=created_from_id(cid),
            updated_at=str(c.get("dateLastActivity") or ""),
            native_state=native,
            assignees=[str(m) for m in c.get("idMembers") or []],
        )

    # -- IssueTracker ------------------------------------------------------
    def list_tickets(self, project: str, *, state: Literal["open", "closed", "all"] = "open",
                     labels: Iterable[str] = (), limit: int = 500) -> list[Ticket]:
        cards = self._req("GET", f"boards/{project}/cards/{state}", {"fields": CARD_FIELDS}) or []
        if len(cards) >= CARD_CAP:
            warn(f"Trello board {project}: {len(cards)} cards returned (API cap); older cards may be missing")
        want = list(labels)
        out: list[Ticket] = []
        for c in cards:
            t = self._ticket(project, c)
            if all(w in t.labels for w in want):
                out.append(t)
        if len(out) > limit:
            warn(f"Trello list stopped at limit={limit}")
        return out[:limit]

    def get_ticket(self, project: str, key: str) -> Ticket:
        c = self._req("GET", f"cards/{key}", {"fields": CARD_FIELDS}) or {}
        return self._ticket(str(c.get("idBoard") or project), c)

    def list_comments(self, project: str, key: str) -> list[Comment]:
        actions = self._req("GET", f"cards/{key}/actions", {"filter": "commentCard", "limit": 1000}) or []
        members = self.members(project)
        out: list[Comment] = []
        for a in reversed(actions):  # API returns newest first
            mc = a.get("memberCreator") or {}
            author = str(mc.get("username") or a.get("idMemberCreator") or "")
            assoc: AuthorAssociation = ("MEMBER" if (a.get("idMemberCreator") in members or author in members)
                                        else "NONE")
            out.append(Comment(id=str(a.get("id")), author=author, association=assoc,
                               body=str((a.get("data") or {}).get("text") or ""), created_at=str(a.get("date") or "")))
        return out

    def create_ticket(self, project: str, title: str, body: str, labels: Iterable[str] = ()) -> Ticket:
        state_labels, plain = sm.split_labels("trello", labels, self.overrides)
        names = sm.candidate_states("trello", state_labels[-1], self.overrides) if state_labels else []
        names += [s for s in (sm.special_state("trello", "open", self.overrides),) if s]
        list_id = self._list_id(project, names)
        if list_id is None:
            first = next((lst for lst in self.lists(project) if not lst.get("closed")), None)
            if first is None:
                raise FatalError(f"Trello board {project} has no open list")
            list_id = str(first["id"])
        ids = [i for i in (self._label_id(project, lb, create=True) for lb in plain) if i]
        params: dict[str, Any] = {"idList": list_id, "name": title, "desc": body}
        if ids:
            params["idLabels"] = ",".join(ids)
        return self._ticket(project, self._req("POST", "cards", params) or {})

    def comment(self, project: str, key: str, body: str) -> Comment:
        a = self._req("POST", f"cards/{key}/actions/comments", {"text": body}) or {}
        mc = a.get("memberCreator") or {}
        return Comment(id=str(a.get("id", "")), author=str(mc.get("username") or ""), association="BOT",
                       body=body, created_at=str(a.get("date") or ""))

    def _move(self, key: str, list_id: str) -> None:
        self._req("PUT", f"cards/{key}", {"idList": list_id})

    def add_labels(self, project: str, key: str, labels: Iterable[str]) -> None:
        state_labels, plain = sm.split_labels("trello", labels, self.overrides)
        for lb in plain:
            lid = self._label_id(project, lb, create=True)
            if lid:
                self._add_label_id(key, lid)
        if state_labels:
            list_id = self._list_id(project, sm.candidate_states("trello", state_labels[-1], self.overrides))
            if list_id is None:
                raise FatalError(f"Trello board {project}: no list for {state_labels[-1]!r}")
            self._move(key, list_id)

    def _add_label_id(self, key: str, label_id: str) -> bool:
        try:
            self._req("POST", f"cards/{key}/idLabels", {"value": label_id})
            return True
        except FatalError as e:
            # "that label is already on the card" is the desired end state.
            if e.status == 400 and "already" in e.body.lower():
                return False
            raise

    def remove_labels(self, project: str, key: str, labels: Iterable[str]) -> None:
        state_labels, plain = sm.split_labels("trello", labels, self.overrides)
        for lb in plain:
            lid = self._label_id(project, lb, create=False)
            if lid:
                try:
                    self._req("DELETE", f"cards/{key}/idLabels/{lid}")
                except FatalError as e:
                    if e.status not in (400, 404):
                        raise
        if state_labels:
            current = self.get_ticket(project, key)
            implied = sm.native_to_labels("trello", current.native_state, self.overrides)
            target = sm.special_state("trello", "open", self.overrides)
            if target and any(lb in implied for lb in state_labels):
                list_id = self._list_id(project, [target])
                if list_id:
                    self._move(key, list_id)

    def close_ticket(self, project: str, key: str,
                     reason: Literal["completed", "not_planned"] = "completed") -> None:
        target = sm.special_state("trello", "done" if reason == "completed" else "not_planned", self.overrides)
        list_id = self._list_id(project, [target]) if target else None
        params: dict[str, Any] = {"closed": "true"}
        if list_id:
            params["idList"] = list_id
        self._req("PUT", f"cards/{key}", params)
