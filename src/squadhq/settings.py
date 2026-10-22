"""Deployment identity: which hub, which org, which bots.

Two GitHub Apps (CONVENTIONS "Identities"):

* the **ops** App (``SQUADHQ_APP_CLIENT_ID`` / ``SQUADHQ_APP_PRIVATE_KEY``,
  login ``SQUADHQ_BOT_LOGIN``): deterministic code — routing, markers, state,
  merging. Its comments and labels are protocol.
* the **agent** App (``SQUADHQ_AGENT_APP_CLIENT_ID`` /
  ``SQUADHQ_AGENT_APP_PRIVATE_KEY``, login ``SQUADHQ_AGENT_BOT_LOGIN``): the
  LLM coding agents. It can push branches, open PRs and comment, but it is
  never trusted for protocol and can never approve its own PR to a merge.

Single-app mode (no agent App configured) is a supported fallback: the agent
login IS the ops login, and every auto/trivial/self merge path falls back to
"an approver merges" (see :func:`single_app_mode`).

Nothing in the package hardcodes an organisation, a hub repository name or a
bot login. Workflows export these from repository/organisation variables
(see docs/architecture/CONVENTIONS.md); the server reads them from its
environment.
"""
from __future__ import annotations

import os

DEFAULT_HUB_NAME = "agent-squad-hq"
DEFAULT_BOT_LOGIN = "squadhq-ops[bot]"
DEFAULT_AGENT_BOT_LOGIN = "squadhq-agent[bot]"
DEFAULT_TRIGGER = "@squad"


def _first_env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def hub_repo() -> str:
    """``owner/name`` of the hub repository.

    Raises ``RuntimeError`` when nothing identifies it — callers must never
    silently fall back to somebody else's organisation.
    """
    v = _first_env("SQUADHQ_HUB_REPO", "HQ_REPO", "HUB_REPO")
    if v:
        return v
    owner = _first_env("SQUADHQ_ORG", "GITHUB_REPOSITORY_OWNER")
    if owner:
        return f"{owner}/{DEFAULT_HUB_NAME}"
    raise RuntimeError(
        "Hub repository unknown: set SQUADHQ_HUB_REPO (owner/name) or SQUADHQ_ORG."
    )


def org() -> str:
    v = _first_env("SQUADHQ_ORG")
    if v:
        return v
    return hub_repo().split("/", 1)[0]


def _full_bot_login(login: str) -> str:
    """``slug`` / ``app/slug`` / ``slug[bot]`` -> ``slug[bot]``. GitHub user
    logins cannot contain ``[``, so only the ``[bot]`` form is unforgeable."""
    login = login.strip().removeprefix("app/")
    return login if login.endswith("[bot]") else f"{login}[bot]"


def bot_login() -> str:
    """Login of the ops automation identity, always ``<slug>[bot]``."""
    return _full_bot_login(_first_env("SQUADHQ_BOT_LOGIN") or DEFAULT_BOT_LOGIN)


def _login_set(base: str, aliases_env: str) -> set[str]:
    """Full ``<slug>[bot]`` logins only. A bare ``slug`` is never in the set:
    a human may register that name (``squadhq.untrusted`` accepts the bare
    form only together with a bot-type flag from the API)."""
    names = {_full_bot_login(base)}
    extra = _first_env(aliases_env)
    if extra:
        for n in extra.split(","):
            if n.strip():
                names.add(_full_bot_login(n))
    return names


def bot_logins() -> set[str]:
    """The OPS identity only (current login + ``SQUADHQ_BOT_ALIASES``). Only
    these logins may author protocol markers, state comments and merges."""
    return _login_set(bot_login(), "SQUADHQ_BOT_ALIASES")


def token_broker() -> str:
    """Token-broker URL (``SQUADHQ_TOKEN_BROKER``, ADR-0011); "" = App-key mode."""
    return _first_env("SQUADHQ_TOKEN_BROKER") or ""


def single_app_mode() -> bool:
    """True when no separate agent App is configured
    (``SQUADHQ_AGENT_APP_CLIENT_ID`` unset): agents then run as the ops bot,
    so nothing may merge without a human approval. Never in token-broker mode:
    the broker mints agent tokens from its own agent App (ADR-0011)."""
    return not (_first_env("SQUADHQ_AGENT_APP_CLIENT_ID") or token_broker())


def agent_bot_login() -> str:
    """Login of the coding-agent identity, e.g. ``squadhq-agent[bot]``; the
    ops login in single-app mode."""
    if single_app_mode():
        return bot_login()
    return _full_bot_login(_first_env("SQUADHQ_AGENT_BOT_LOGIN") or DEFAULT_AGENT_BOT_LOGIN)


def agent_bot_logins() -> set[str]:
    """The agent identity (+ ``SQUADHQ_AGENT_BOT_ALIASES``). Equal to
    :func:`bot_logins` in single-app mode."""
    if single_app_mode():
        return bot_logins()
    return _login_set(agent_bot_login(), "SQUADHQ_AGENT_BOT_ALIASES")


def automation_logins() -> set[str]:
    """Ops + agent: everything that is *our* automation (activity, authorship
    of agent role comments and PRs), as opposed to humans or other apps."""
    return bot_logins() | agent_bot_logins()


def bot_git_identity() -> tuple[str, str]:
    """``(name, email)`` for commits authored by the bot.

    GitHub attributes ``<id>+<login>@users.noreply.github.com``; the id is
    optional (``SQUADHQ_BOT_USER_ID``) — without it GitHub still shows the
    login, just without the avatar link.
    """
    login = bot_login()
    uid = _first_env("SQUADHQ_BOT_USER_ID")
    email = f"{uid}+{login}@users.noreply.github.com" if uid else f"{login}@users.noreply.github.com"
    return login, email


def trigger_phrase() -> str:
    return _first_env("SQUADHQ_TRIGGER") or DEFAULT_TRIGGER
