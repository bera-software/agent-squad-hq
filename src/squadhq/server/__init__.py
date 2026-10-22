"""Self-hostable server mode: API (webhooks, approvals, audit), queue worker, scheduler.

Entry points::

    from squadhq.server.app import create_app      # FastAPI app factory
    python -m squadhq.server serve|worker|scheduler|migrate|create-token

FastAPI is an optional dependency (``pip install 'agent-squad-hq[server]'``);
importing this package does not import it.
"""
from __future__ import annotations

__all__ = ["create_app"]


def create_app(*args: object, **kwargs: object) -> object:
    from squadhq.server.app import create_app as _create_app

    return _create_app(*args, **kwargs)  # type: ignore[arg-type]
