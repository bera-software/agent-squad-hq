"""Fence untrusted text before it reaches a model.

Every prompt that interpolates issue/PR/comment/web/tracker content wraps it
with :func:`fence`. ``prompts/_untrusted-input.md`` tells the model that
anything inside ``<untrusted>`` is data and never instructions.
"""
from __future__ import annotations

import html
import re

_TAG_RE = re.compile(r"<\s*/?\s*untrusted\b[^>]*>", re.IGNORECASE)

TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def _attr(value: object) -> str:
    return html.escape(str(value), quote=True)


def neutralise(text: str) -> str:
    """Defang any ``<untrusted …>`` / ``</untrusted>`` tag inside ``text`` so
    the content cannot close its own fence and smuggle instructions out."""
    return _TAG_RE.sub(lambda m: m.group(0).replace("<", "&lt;").replace(">", "&gt;"), text or "")


def fence(text: str | None, *, source: str, author: str = "unknown",
          association: str = "NONE", max_chars: int | None = None) -> str:
    body = neutralise(text or "")
    if max_chars is not None and len(body) > max_chars:
        body = body[:max_chars] + "\n[…truncated]"
    return (f'<untrusted source="{_attr(source)}" author="{_attr(author)}" '
            f'association="{_attr(association)}">\n{body}\n</untrusted>')


def is_trusted(association: str | None, *, allowed: frozenset[str] | set[str] = TRUSTED_ASSOCIATIONS) -> bool:
    return (association or "").upper() in allowed


# ---------------------------------------------------------------------------
# Who may speak for the control plane. Anyone who can comment can TYPE a
# hidden marker or protocol string, so code that acts on one checks its
# author first: markers count only from our bot logins; deliberate human
# input counts only from a trusted association.
# ---------------------------------------------------------------------------


def trusted_associations() -> frozenset[str]:
    """``SQUADHQ_TRUSTED_ASSOCIATIONS`` (comma-separated) or OWNER/MEMBER/COLLABORATOR."""
    import os

    raw = os.environ.get("SQUADHQ_TRUSTED_ASSOCIATIONS", "")
    vals = frozenset(v.strip().upper() for v in raw.split(",") if v.strip())
    return vals or TRUSTED_ASSOCIATIONS


def author_login(item: dict[str, object] | None) -> str:
    """Login of a REST (``user.login``) or GraphQL/gh-json (``author.login``) comment, issue or PR."""
    if not isinstance(item, dict):
        return ""
    for key in ("user", "author"):
        who = item.get(key)
        if isinstance(who, dict) and isinstance(who.get("login"), str):
            return str(who["login"])
    return ""


def author_is_bot(item: dict[str, object] | None) -> bool:
    """The API says the author is a bot account: REST ``user.type == "Bot"``,
    gh ``--json`` ``author.is_bot``, or GraphQL ``__typename == "Bot"``."""
    if not isinstance(item, dict):
        return False
    for key in ("user", "author", "actor"):
        who = item.get(key)
        if isinstance(who, dict):
            if who.get("is_bot") is True or who.get("type") == "Bot" or who.get("__typename") == "Bot":
                return True
    return False


def _login_in(login: str | None, names: set[str], is_bot: bool = False) -> bool:
    """``names`` holds full ``slug[bot]`` logins. Accepted forms:

    * ``slug[bot]`` (REST ``user.login``): unforgeable, a user login cannot contain ``[``;
    * ``app/slug`` (gh ``--json`` author of an App): unforgeable, a user login cannot contain ``/``;
    * bare ``slug`` (GraphQL ``login``) ONLY with ``is_bot`` from the API: a human
      may register ``squadhq-ops``.
    """
    if not login:
        return False
    if login.endswith("[bot]"):
        return login in names
    if login.startswith("app/"):
        return f"{login[4:]}[bot]" in names
    return is_bot and f"{login}[bot]" in names


def is_bot_login(login: str | None, *, is_bot: bool = False) -> bool:
    """True only for OUR OPS identity (``settings.bot_logins()``) — not the
    agent App and not any other ``[bot]`` account. Use for protocol markers,
    state comments, ledgers and merge authorship. Pass ``is_bot`` (the API's
    bot flag) when ``login`` may be the bare GraphQL form."""
    from squadhq import settings

    return _login_in(login, settings.bot_logins(), is_bot)


def by_bot(item: dict[str, object] | None) -> bool:
    """Authored by the ops identity (see :func:`is_bot_login`)."""
    return is_bot_login(author_login(item), is_bot=author_is_bot(item))


def is_agent_login(login: str | None, *, is_bot: bool = False) -> bool:
    """True for OUR coding-agent identity (``settings.agent_bot_logins()``);
    the ops login in single-app mode. Use for "this PR/comment is agent work"."""
    from squadhq import settings

    return _login_in(login, settings.agent_bot_logins(), is_bot)


def by_agent(item: dict[str, object] | None) -> bool:
    return is_agent_login(author_login(item), is_bot=author_is_bot(item))


def is_automation_login(login: str | None, *, is_bot: bool = False) -> bool:
    """Ops OR agent: our automation as opposed to a human or another app."""
    return is_bot_login(login, is_bot=is_bot) or is_agent_login(login, is_bot=is_bot)


def by_automation(item: dict[str, object] | None) -> bool:
    return is_automation_login(author_login(item), is_bot=author_is_bot(item))


def association_of(item: dict[str, object] | None) -> str:
    if not isinstance(item, dict):
        return ""
    v = item.get("author_association") or item.get("authorAssociation") or ""
    return str(v).upper()


def by_trusted_human(item: dict[str, object] | None) -> bool:
    """A comment/issue authored by a trusted association (and not a bot)."""
    return association_of(item) in trusted_associations() and not by_automation(item)


def by_bot_or_trusted(item: dict[str, object] | None) -> bool:
    """Ops identity or a trusted association (code-written statements)."""
    return by_bot(item) or association_of(item) in trusted_associations()


def by_automation_or_trusted(item: dict[str, object] | None) -> bool:
    """Ops, agent or a trusted association: bodies agents write as part of
    their role (Closes, Gate-for, Why this matters) count, anybody else's do not."""
    return by_automation(item) or association_of(item) in trusted_associations()
