"""``python -m squadhq.server serve|worker|scheduler|migrate|create-token|hash-token|check``.

(Also reachable as ``squadhq server ...`` once the main CLI delegates here.)
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from typing import Any

from squadhq.server.auth import ROLES, check_scope, generate_token, hash_token
from squadhq.server.observability import configure_logging
from squadhq.server.settings import ServerSettings

log = logging.getLogger("squadhq.server")


def _store(settings: ServerSettings, migrate: bool = True) -> Any:
    from squadhq.store import open_store

    return open_store(settings.db_url, migrate=migrate)


def _install_signals(stop: Any) -> None:  # pragma: no cover - process wiring
    def handler(signum: int, frame: Any) -> None:
        log.info("signal received, stopping", extra={"signal": signum})
        stop()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def cmd_serve(settings: ServerSettings, args: argparse.Namespace) -> int:  # pragma: no cover - runs uvicorn
    import uvicorn

    from squadhq.server.app import create_app

    for p in settings.validate():
        log.warning("configuration", extra={"problem": p})
    app = create_app(settings)
    uvicorn.run(app, host=args.host or settings.host, port=args.port or settings.port, log_config=None,
                proxy_headers=True, forwarded_allow_ips=settings.trusted_proxies, server_header=False,
                timeout_keep_alive=5, limit_concurrency=args.limit_concurrency)
    return 0


def cmd_worker(settings: ServerSettings, args: argparse.Namespace) -> int:
    from squadhq.server.worker import Worker

    store = _store(settings)
    w = Worker(store, settings)
    if args.once:
        processed = 0
        while processed < args.max_jobs and w.run_once() is not None:
            processed += 1
        print(json.dumps({"processed": processed}))
        return 0
    _install_signals(w.stop)  # pragma: no cover
    w.run_forever()  # pragma: no cover
    return 0  # pragma: no cover


def cmd_scheduler(settings: ServerSettings, args: argparse.Namespace) -> int:
    from squadhq.scheduler import Scheduler, describe, load_schedule_config

    cfg = load_schedule_config(settings.config_path or None, settings.inline_config, settings.config_text or None)
    store = _store(settings)
    s = Scheduler(store, cfg)
    if args.list:
        print(json.dumps({"timezone": cfg.timezone, "schedules": describe(cfg), "errors": cfg.errors}, indent=2))
        return 1 if cfg.errors else 0
    if args.once:
        r = s.tick()
        print(json.dumps({"leader": r.leader, "enqueued": r.enqueued}))
        return 0
    _install_signals(s.stop)  # pragma: no cover
    s.run_forever(settings.scheduler_interval_s)  # pragma: no cover
    return 0  # pragma: no cover


def cmd_migrate(settings: ServerSettings, args: argparse.Namespace) -> int:
    store = _store(settings, migrate=False)
    v = store.migrate()
    print(json.dumps({"schema_version": v, "store": store.dialect}))
    store.close()
    return 0


def cmd_create_token(settings: ServerSettings, args: argparse.Namespace) -> int:
    project = (args.project or "").strip()
    try:
        check_scope(args.role, project)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    token = generate_token()
    if args.print_only:
        entry = f"{args.name}:{args.role}:{hash_token(token)}" + (f":{project}" if project else "")
        print(json.dumps({"name": args.name, "role": args.role, "project": project or None, "token": token,
                          "env_entry": entry}))
        return 0
    store = _store(settings)
    rec = store.add_token(args.name, args.role, hash_token(token), project=project)
    store.close()
    print(json.dumps({**rec.public(), "token": token}))
    print("Store this token now; only its hash is kept.", file=sys.stderr)
    return 0


def cmd_hash_token(settings: ServerSettings, args: argparse.Namespace) -> int:
    token = sys.stdin.readline().strip() if args.token == "-" else args.token  # noqa: S105
    print(hash_token(token))
    return 0


def cmd_check(settings: ServerSettings, args: argparse.Namespace) -> int:
    problems = settings.validate()
    print(json.dumps({"mode": settings.mode, "problems": problems}, indent=2))
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="squadhq server", description="Agent Squad HQ server mode")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the HTTP API")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--limit-concurrency", type=int, default=200)
    w = sub.add_parser("worker", help="run the queue worker")
    w.add_argument("--once", action="store_true", help="drain up to --max-jobs and exit")
    w.add_argument("--max-jobs", type=int, default=100)
    sc = sub.add_parser("scheduler", help="run the cron scheduler (leader-elected)")
    sc.add_argument("--once", action="store_true", help="run one tick and exit")
    sc.add_argument("--list", action="store_true", help="print schedules and next fire times")
    sub.add_parser("migrate", help="apply database migrations")
    def token_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--role", required=True, choices=ROLES)
        p.add_argument("--project", default="",
                       help="owner/repo; required for --role reporter (a project's SQUADHQ_SERVER_TOKEN)")
        p.add_argument("--name", default="cli")
        p.add_argument("--print-only", action="store_true",
                       help="do not store; print a SQUADHQ_API_TOKENS entry instead")

    token_args(sub.add_parser("create-token", help="create an API token (printed once)"))
    tk = sub.add_parser("token", help="token management")
    tk_sub = tk.add_subparsers(dest="token_cmd", required=True)
    token_args(tk_sub.add_parser("create", help="create an API token (printed once)"))
    ht = sub.add_parser("hash-token", help="print sha256 of a token ('-' reads stdin)")
    ht.add_argument("token")
    sub.add_parser("check", help="validate settings")
    return ap


COMMANDS = {"serve": cmd_serve, "worker": cmd_worker, "scheduler": cmd_scheduler, "migrate": cmd_migrate,
            "create-token": cmd_create_token, "token": cmd_create_token, "hash-token": cmd_hash_token,
            "check": cmd_check}


# Third-party modules behind the [server] and [postgres] extras.
OPTIONAL_DEPS = frozenset({"fastapi", "starlette", "uvicorn", "pydantic", "psycopg", "psycopg_binary"})
INSTALL_HINT = "pip install 'agent-squad-hq[server,postgres]'"


def _missing_extra(exc: BaseException) -> str | None:
    """The optional dependency ``exc`` (or its cause) failed to import, if any."""
    e: BaseException | None = exc
    while e is not None:
        if isinstance(e, ImportError) and (e.name or "").split(".")[0] in OPTIONAL_DEPS:
            return (e.name or "").split(".")[0]
        e = e.__cause__
    return None


def main(argv: list[str] | None = None, settings: ServerSettings | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = settings or ServerSettings.from_env()
    configure_logging(settings.log_level, settings.log_format)
    try:
        return COMMANDS[args.cmd](settings, args)
    except (ImportError, RuntimeError) as e:
        name = _missing_extra(e)
        if name is None:
            raise
        print(f"error: squadhq server {args.cmd} needs {name}, which is not installed\n    hint: {INSTALL_HINT}",
              file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
