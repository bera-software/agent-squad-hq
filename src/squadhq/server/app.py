"""FastAPI application: ``create_app(settings)``.

REST API v1 (docs/architecture/CONVENTIONS.md) plus operational endpoints.
Every ``/api/v1`` route except ``/healthz`` and ``/webhooks/*`` requires a
bearer token; roles are ``viewer < approver < admin``.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from squadhq import __version__
from squadhq.audit.chain import canonical_json, verify_chain
from squadhq.server import auth as auth_mod
from squadhq.server import events as ev_mod
from squadhq.interfaces import FatalError
from squadhq.server.auth import ROLES, Principal
from squadhq.server.integrations import Integrations
from squadhq.server.observability import ServerMetrics, instrument_fastapi, request_id_var, tracing
from squadhq.server.settings import ServerSettings
from squadhq.store import ConflictError, NotFoundError, StateStore, open_store
from squadhq.store.base import Run, ts

log = logging.getLogger("squadhq.api")

API = "/api/v1"
WEBHOOK_SOURCES = ("github", "jira", "linear", "trello", "gitlab")
MANUAL_JOB_KINDS = ("sweep", "janitor", "hub_loop", "ticket_transition", "dispatch_coding", "hub_workflow")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MAX_RUN_COST_USD = 10_000.0
MAX_RUN_EXTRA_BYTES = 262_144

ASGIApp = Callable[[MutableMapping[str, Any], Callable[[], Awaitable[MutableMapping[str, Any]]],
                    Callable[[MutableMapping[str, Any]], Awaitable[None]]], Awaitable[None]]


class _TooLarge(Exception):
    pass


class RequestContextMiddleware:
    """Request id, body-size limit, security headers, metrics and access log (pure ASGI)."""

    def __init__(self, app: ASGIApp, settings: ServerSettings, metrics: ServerMetrics) -> None:
        self.app = app
        self.settings = settings
        self.metrics = metrics

    async def __call__(self, scope: MutableMapping[str, Any],
                       receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
                       send: Callable[[MutableMapping[str, Any]], Awaitable[None]]) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        rid = headers.get("x-request-id", "")
        if not _REQUEST_ID_RE.match(rid):
            rid = uuid.uuid4().hex
        token = request_id_var.set(rid)
        path: str = scope.get("path", "")
        limit = (self.settings.webhook_max_body_bytes if path.startswith(f"{API}/webhooks/")
                 else self.settings.max_body_bytes)
        status_holder = {"status": 500, "started": False}
        too_large = {"hit": False, "sent": False}
        start = time.perf_counter()
        big_body = json.dumps({"detail": "request body too large"}).encode()

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            if too_large["hit"]:
                # the app saw a truncated body (FastAPI turns read errors into 400): answer 413 instead
                if message["type"] == "http.response.start":
                    message = {"type": "http.response.start", "status": 413,
                               "headers": [(b"content-type", b"application/json"),
                                           (b"content-length", str(len(big_body)).encode())]}
                elif message["type"] == "http.response.body":
                    if too_large["sent"]:
                        return
                    too_large["sent"] = True
                    message = {"type": "http.response.body", "body": big_body}
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                status_holder["started"] = True
                hdrs = list(message.get("headers", []))
                hdrs += [(b"x-request-id", rid.encode()), (b"x-content-type-options", b"nosniff"),
                         (b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer"),
                         (b"x-frame-options", b"DENY")]
                message["headers"] = hdrs
            await send(message)

        try:
            declared = headers.get("content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > limit):
                await self._reject(send_wrapper, 413 if declared.isdigit() else 400)
                return
            seen = 0

            async def receive_wrapper() -> MutableMapping[str, Any]:
                nonlocal seen
                msg = await receive()
                if msg["type"] == "http.request":
                    seen += len(msg.get("body", b""))
                    if seen > limit:
                        too_large["hit"] = True
                        raise _TooLarge()
                return msg

            try:
                await self.app(scope, receive_wrapper, send_wrapper)
            except _TooLarge:
                if not status_holder["started"]:
                    await self._reject(send_wrapper, 413)
        finally:
            route = scope.get("route")
            route_path = getattr(route, "path", None) or ("unmatched" if status_holder["status"] == 404 else path)
            elapsed = time.perf_counter() - start
            self.metrics.http_requests.inc(method=scope.get("method", ""), route=route_path,
                                           status=str(status_holder["status"]))
            self.metrics.http_seconds_sum.inc(elapsed, route=route_path)
            self.metrics.http_seconds_count.inc(route=route_path)
            log.info("request", extra={"method": scope.get("method"), "path": path,
                                       "status": status_holder["status"], "duration_ms": round(elapsed * 1000, 2)})
            request_id_var.reset(token)

    @staticmethod
    async def _reject(send: Callable[[MutableMapping[str, Any]], Awaitable[None]], status: int) -> None:
        body = json.dumps({"detail": "request body too large" if status == 413 else "bad content-length"}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=4000)


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    role: Literal["viewer", "approver", "admin", "reporter"]
    project: str = Field(default="", max_length=200, description="required for (and only for) role=reporter")


class JobIn(BaseModel):
    kind: Literal["sweep", "janitor", "hub_loop", "ticket_transition", "dispatch_coding", "hub_workflow"]
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=200)


class RunIn(BaseModel):
    """An ``ExecutionResult`` reported back by a BYO-compute workflow."""

    project: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=64)
    outcome: Literal["success", "failure", "skipped"]
    executor: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=128)
    # bounded: a reporter must not be able to break /costs (inf/NaN/huge) or flood the ledger
    cost_usd: float = Field(default=0.0, ge=0, le=MAX_RUN_COST_USD, allow_inf_nan=False)
    duration_s: float = Field(default=0.0, ge=0, le=7 * 86400, allow_inf_nan=False)
    input_tokens: int = Field(default=0, ge=0, le=10**10)
    output_tokens: int = Field(default=0, ge=0, le=10**10)
    error_class: str = Field(default="none", max_length=32)
    run_id: str | None = Field(default=None, max_length=128)
    started_at: str | None = Field(default=None, max_length=40, description="ISO-8601; within the allowed skew")
    job_id: str | None = Field(default=None, max_length=64)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("extra")
    @classmethod
    def _extra_size(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(v, default=str)) > MAX_RUN_EXTRA_BYTES:
            raise ValueError(f"extra is larger than {MAX_RUN_EXTRA_BYTES} bytes")
        return v


class IntakeIn(BaseModel):
    """``POST /requests``: a human request that becomes a tracker ticket (untrusted text)."""

    project: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(default="", max_length=20_000)
    source: Literal["mobile", "dashboard", "voice"] = "mobile"


PushTopic = Literal["approvals", "escalations", "brief"]


class DeviceIn(BaseModel):
    expo_push_token: str = Field(max_length=256, pattern=r"^(ExponentPushToken|ExpoPushToken)\[[^\]]+\]$")
    platform: Literal["ios", "android"]
    name: str = Field(default="", max_length=128)
    topics: list[PushTopic] = Field(default_factory=lambda: list[PushTopic](["approvals", "escalations"]),
                                    max_length=3)


class BriefIn(BaseModel):
    markdown: str = Field(min_length=1, max_length=200_000)
    project: str = Field(default="", max_length=200)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(settings: ServerSettings | None = None, *, store: StateStore | None = None,
               auto_migrate: bool = True, integrations: Integrations | None = None) -> FastAPI:
    settings = settings or ServerSettings.from_env()
    st = store or open_store(settings.db_url, migrate=auto_migrate)
    metrics = ServerMetrics()

    app = FastAPI(
        title="Agent Squad HQ",
        version=__version__,
        description="Self-hosted control plane API (server mode). See docs/architecture/CONVENTIONS.md.",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # never echo request input back (it may be non-JSON-encodable, e.g. inf/NaN, or sensitive)
        errs = [{"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))}
                for e in exc.errors()]
        return JSONResponse({"detail": errs}, status_code=422)

    app.state.store = st
    app.state.settings = settings
    app.state.metrics = metrics
    app.add_middleware(RequestContextMiddleware, settings=settings, metrics=metrics)
    if settings.otel_enabled and tracing.enable():
        instrument_fastapi(app)

    def collect() -> None:
        ok = st.ping()
        metrics.up.set(1.0 if ok else 0.0)
        if ok:
            for status, n in st.count_jobs().items():
                metrics.jobs.set(float(n), status=status)
            metrics.approvals_pending.set(float(len(st.list_approvals("pending", limit=10_000))))

    metrics.registry.on_collect(collect)

    try:  # best effort: register projects from squadhq.yml
        from squadhq.scheduler.schedules import load_schedule_config
        from squadhq.server.worker import sync_projects

        cfg = load_schedule_config(settings.config_path or None, settings.inline_config, settings.config_text or None)
        sync_projects(st, cfg.projects)
        app.state.schedule_config = cfg
    except Exception as e:
        log.warning("could not load squadhq.yml", extra={"error": str(e)})
        app.state.schedule_config = None

    from squadhq.server.approvals import ApprovalExecutor
    from squadhq.server.integrations import DefaultIntegrations

    approvals_exec = ApprovalExecutor(st, integrations or DefaultIntegrations(),
                                      required_checks=settings.required_checks,
                                      allow_fork_merges=settings.allow_fork_merges,
                                          required_checks_app=settings.required_checks_app,
                                          required_checks_workflow=settings.required_checks_workflow)

    bearer_scheme = HTTPBearer(auto_error=False, description="API token (`squadhq server create-token`)")

    def _candidates() -> list[tuple[str, ...]]:
        out: list[tuple[str, ...]] = [("static:" + t.name, t.name, t.role, t.token_hash, t.project)
                                      for t in settings.static_tokens]
        out += [(t.id, t.name, t.role, t.token_hash, t.project) for t in st.list_tokens()]
        return out

    def principal(creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> Principal:  # noqa: B008
        presented = creds.credentials if creds else None
        if not presented:
            raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
        p = auth_mod.match(presented, _candidates())
        if p is None:
            raise HTTPException(401, "invalid token", headers={"WWW-Authenticate": "Bearer"})
        return p

    def require(role: str) -> Callable[[Principal], Principal]:
        def dep(p: Principal = Depends(principal)) -> Principal:  # noqa: B008
            if not p.has(role):
                raise HTTPException(403, f"requires role {role}")
            return p

        return dep

    viewer = Depends(require("viewer"))
    approver = Depends(require("approver"))
    admin = Depends(require("admin"))

    # --- health -------------------------------------------------------------
    @app.get("/healthz", tags=["ops"])
    @app.get(f"{API}/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["ops"])
    @app.get(f"{API}/readyz", tags=["ops"])
    def readyz() -> JSONResponse:
        ok = st.ping()
        body = {"status": "ready" if ok else "unavailable", "schema_version": st.schema_version() if ok else 0,
                "store": st.dialect}
        return JSONResponse(body, status_code=200 if ok else 503)

    @app.get("/metrics", tags=["ops"], response_class=PlainTextResponse)
    def metrics_endpoint(request: Request) -> PlainTextResponse:
        if not settings.metrics_public:
            creds = request.headers.get("authorization")
            tok = auth_mod.bearer(creds)
            if not tok:
                raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
            who = auth_mod.match(tok, _candidates())
            if who is None:
                raise HTTPException(401, "invalid token", headers={"WWW-Authenticate": "Bearer"})
            if not who.has("viewer"):  # reporter tokens may only report runs
                raise HTTPException(403, "requires role viewer")
        return PlainTextResponse(metrics.registry.render(), media_type="text/plain; version=0.0.4")

    # --- webhooks -------------------------------------------------------------
    @app.post(f"{API}/webhooks/{{source}}", tags=["webhooks"], status_code=202)
    async def webhook(source: str, request: Request) -> JSONResponse:
        if source not in WEBHOOK_SOURCES:
            raise HTTPException(404, "unknown webhook source")
        body = await request.body()
        headers = dict(request.headers)
        secret = settings.webhook_secrets.get(source, "")
        if not secret:
            metrics.webhooks.inc(source=source, result="not_configured")
            raise HTTPException(401, f"webhook source {source} is not configured")
        ok = ev_mod.verify(source, headers, body, secret, settings.trello_callback_url)
        if not ok:
            metrics.webhooks.inc(source=source, result="bad_signature")
            log.warning("webhook signature rejected", extra={"source": source})
            raise HTTPException(401, "invalid or missing signature")
        did = ev_mod.delivery_id(source, headers, body)
        digest = ev_mod.body_digest(body)
        seen = await run_in_threadpool(st.kv_get, "webhook_body", f"{source}:{digest}")
        if seen is not None:  # replay of a signed body under a new delivery id
            metrics.webhooks.inc(source=source, result="duplicate")
            return JSONResponse({"job_id": seen[0], "delivery_id": did, "duplicate": True}, status_code=200)
        event = await run_in_threadpool(ev_mod.normalize, source, headers, body)
        if event.get("action") == "ping":
            metrics.webhooks.inc(source=source, result="ping")
            return JSONResponse({"ok": True, "ping": True}, status_code=200)
        res = await run_in_threadpool(
            st.enqueue, "webhook_event", {"source": source, "delivery_id": did, "event": event},
            idempotency_key=f"webhook:{source}:{did}", max_attempts=settings.job_max_attempts)
        if res.created:
            try:
                await run_in_threadpool(st.kv_cas, "webhook_body", f"{source}:{digest}", res.job_id, None)
            except ConflictError:  # a concurrent replay won the race; the job is still unique per delivery id
                pass
        metrics.webhooks.inc(source=source, result="duplicate" if not res.created else "accepted")
        return JSONResponse({"job_id": res.job_id, "delivery_id": did, "duplicate": not res.created},
                            status_code=202 if res.created else 200)

    # --- read APIs ------------------------------------------------------------
    @app.get(f"{API}/whoami", tags=["auth"])
    def whoami(p: Principal = viewer) -> dict[str, str]:
        return {"name": p.name, "role": p.role}

    @app.get(f"{API}/projects", tags=["projects"])
    def projects(_: Principal = viewer) -> dict[str, Any]:
        return {"projects": [p.to_dict() for p in st.list_projects()]}

    @app.get(f"{API}/tickets", tags=["projects"])
    def tickets(project: str | None = None, state: str | None = None,
                limit: int = Query(500, ge=1, le=2000), _: Principal = viewer) -> dict[str, Any]:
        return {"tickets": [t.to_dict() for t in st.list_tickets(project, state, limit)]}

    @app.get(f"{API}/runs", tags=["runs"])
    def runs(project: str | None = None, limit: int = Query(50, ge=1, le=1000),
             _: Principal = viewer) -> dict[str, Any]:
        return {"runs": [r.to_dict() for r in st.list_runs(project, limit)]}

    def _run_started_at(value: str | None) -> str:
        """Normalise a reported start time; refuse anything outside now +/- the allowed skew."""
        now = datetime.now(timezone.utc)
        if not value:
            return ts(now)
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(422, "started_at must be an ISO-8601 timestamp") from None
        dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        skew = timedelta(days=settings.run_max_skew_days)
        if not now - skew <= dt <= now + timedelta(minutes=10):
            raise HTTPException(422, f"started_at must be within the last {settings.run_max_skew_days} days")
        return ts(dt)

    @app.post(f"{API}/runs", tags=["runs"], status_code=201)
    def report_run(body: RunIn, p: Principal = Depends(principal)) -> dict[str, Any]:  # noqa: B008
        # admin, or a reporter token bound to exactly this project (what squadhq-agent.yml holds)
        if not p.may_report_for(body.project):
            raise HTTPException(403, "this token may not report runs for that project")
        if body.job_id and not p.has("admin"):
            j = st.get_job(body.job_id)
            if j is not None and str(j.payload.get("project") or "").lower() != body.project.lower():
                raise HTTPException(403, "job belongs to another project")
        started = _run_started_at(body.started_at)
        run_id = body.run_id or uuid.uuid4().hex
        if p.is_reporter:  # a reporter cannot pre-claim (and so suppress) another project's run ids
            run_id = f"{p.project.lower()}:{run_id}"
        run = Run(id=run_id, project=body.project, role=body.role, outcome=body.outcome,
                  started_at=started, model=body.model, executor=body.executor,
                  cost_usd=body.cost_usd, duration_s=body.duration_s, input_tokens=body.input_tokens,
                  output_tokens=body.output_tokens, error_class=body.error_class, job_id=body.job_id,
                  data=body.extra)
        st.record_run(run)
        st.append_audit(f"token:{p.name}", "run.reported", f"{body.project}:{body.role}",
                        {"run_id": run.id, "outcome": run.outcome, "cost_usd": run.cost_usd})
        return run.to_dict()

    @app.get(f"{API}/capacity", tags=["runs"])
    def capacity(_: Principal = viewer) -> dict[str, Any]:
        """Coding-agent account pool: per-account breaker state (ids and secret names only, never values)."""
        from squadhq.server import accounts as pool_mod

        return pool_mod.status(st, pool_mod.load_pool(), datetime.now(timezone.utc))

    @app.get(f"{API}/costs", tags=["runs"])
    def costs(month: str | None = None, _: Principal = viewer) -> dict[str, Any]:
        m = month or datetime.now(timezone.utc).strftime("%Y-%m")
        if not _MONTH_RE.match(m):
            raise HTTPException(422, "month must be YYYY-MM")
        out = st.cost_summary(m)
        cfg = app.state.schedule_config
        out["budget_usd"] = cfg.budget_usd if cfg is not None else None
        out["budget_by_project"] = dict(cfg.budget_by_project) if cfg is not None else {}
        return out

    @app.get(f"{API}/briefs/latest", tags=["briefs"], response_model=None)
    def latest_brief(project: str | None = None, format: Literal["json", "markdown"] = "json",  # noqa: A002
                     _: Principal = viewer) -> Response | dict[str, Any]:
        b = st.latest_brief(project)
        if b is None:
            raise HTTPException(404, "no brief yet")
        if format == "markdown":
            return PlainTextResponse(b.markdown, media_type="text/markdown; charset=utf-8")
        return {"id": b.id, "created_at": b.created_at, "project": b.project, "markdown": b.markdown}

    @app.post(f"{API}/briefs", tags=["briefs"], status_code=201)
    def add_brief(body: BriefIn, p: Principal = admin) -> dict[str, Any]:
        b = st.add_brief(body.markdown, body.project)
        st.append_audit(f"token:{p.name}", "brief.added", f"brief:{b.id}", {"project": body.project})
        from squadhq.server.worker import push_brief

        push_brief(st, b.id, b.markdown)
        return {"id": b.id, "created_at": b.created_at}

    # --- intake requests (dashboard / mobile / voice) ----------------------
    def _resolve_project(name: str) -> tuple[str, str]:
        low = name.strip().lower()
        for proj in st.list_projects():
            if low in (proj.repo.lower(), proj.name.lower()):
                return proj.repo, proj.tracker
        raise HTTPException(422, f"unknown project {name!r}; see GET /api/v1/projects")

    @app.post(f"{API}/requests", tags=["requests"], status_code=202)
    def create_request(body: IntakeIn, p: Principal = approver) -> dict[str, Any]:
        repo, tracker = _resolve_project(body.project)
        req = st.create_intake(project=repo, title=body.title, body=body.body, source=body.source,
                               requested_by=f"token:{p.name}", tracker=tracker)
        return req.public()

    @app.get(f"{API}/requests/{{request_id}}", tags=["requests"])
    def get_request(request_id: str, _: Principal = viewer) -> dict[str, Any]:
        req = st.get_intake(request_id)
        if req is None:
            raise HTTPException(404, "request not found")
        return req.public()

    # --- push devices -----------------------------------------------------------
    def _owner(p: Principal) -> str:
        # keyed on the token *id* (token names are not unique)
        return f"token-id:{p.token_id}" if p.token_id else f"token:{p.name}"

    @app.post(f"{API}/devices", tags=["devices"], status_code=201)
    def register_device(body: DeviceIn, p: Principal = viewer) -> dict[str, Any]:
        topics: list[str] = sorted(set(body.topics))
        try:
            d, _created = st.upsert_device(expo_push_token=body.expo_push_token, platform=body.platform,
                                           name=body.name, topics=topics, owner=_owner(p),
                                           allow_takeover=p.has("admin"))
        except ConflictError:
            st.append_audit(f"token:{p.name}", "device.takeover_refused", "device:token", {"platform": body.platform})
            raise HTTPException(409, "this push token is registered to another principal") from None
        return {"id": d.id, "created_at": d.created_at}

    @app.delete(f"{API}/devices/{{device_id}}", tags=["devices"])
    def delete_device(device_id: str, p: Principal = viewer) -> dict[str, Any]:
        d = st.get_device(device_id)
        if d is None:
            raise HTTPException(404, "device not found")
        if d.owner != _owner(p) and not p.has("admin"):
            raise HTTPException(403, "only the device owner or an admin can remove it")
        st.delete_device(device_id)
        st.append_audit(f"token:{p.name}", "device.removed", f"device:{device_id}", {})
        return {"id": device_id, "deleted": True}

    @app.get(f"{API}/schedules", tags=["ops"])
    def schedules(_: Principal = viewer) -> dict[str, Any]:
        cfg = app.state.schedule_config
        if cfg is None:
            return {"timezone": "UTC", "schedules": [], "errors": ["squadhq.yml not loaded"]}
        from squadhq.scheduler.loop import describe

        return {"timezone": cfg.timezone, "schedules": describe(cfg), "errors": cfg.errors}

    # --- approvals ------------------------------------------------------------
    @app.get(f"{API}/approvals", tags=["approvals"])
    def approvals(status: Literal["pending", "approved", "rejected", "resolved", "all"] = "pending",
                  project: str | None = None, _: Principal = viewer) -> dict[str, Any]:
        items = st.list_approvals(None if status == "all" else status, project)
        return {"approvals": [a.to_dict() for a in items]}

    @app.get(f"{API}/approvals/{{approval_id}}", tags=["approvals"])
    def approval(approval_id: str, _: Principal = viewer) -> dict[str, Any]:
        a = st.get_approval(approval_id)
        if a is None:
            raise HTTPException(404, "approval not found")
        return a.to_dict()

    @app.post(f"{API}/approvals/{{approval_id}}/decision", tags=["approvals"], status_code=202)
    def decide(approval_id: str, body: DecisionIn, p: Principal = approver) -> dict[str, Any]:
        pending = st.get_approval(approval_id)
        if pending is not None and pending.kind == "merge" and body.decision == "approve" \
                and pending.status == "pending":
            try:
                approvals_exec.verify_head(pending)
            except FatalError as e:
                st.append_audit(f"token:{p.name}", "approval.refused", f"{pending.project}:{pending.subject}",
                                {"approval_id": approval_id, "reason": str(e)})
                raise HTTPException(409, str(e)) from None
            except Exception as e:
                log.warning("cannot verify PR head", extra={"error": type(e).__name__})
                raise HTTPException(503, "cannot verify the PR head commit right now; try again") from None
        try:
            a, event = st.decide_approval(approval_id, decision=body.decision, actor=f"token:{p.name}",
                                          comment=body.comment, action_job=("approval_action", {}))
        except NotFoundError:
            raise HTTPException(404, "approval not found") from None
        except ConflictError as e:
            raise HTTPException(409, str(e)) from None
        metrics.approval_decisions.inc(decision=body.decision)
        return {"approval": a.to_dict(), "audit_event": event.to_dict(), "action_job_id": a.action_job_id}

    # --- audit ----------------------------------------------------------------
    def _since(since: str | None) -> tuple[int, str | None]:
        if not since:
            return 0, None
        if since.isdigit():
            return int(since), None
        try:
            dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(422, "since must be a sequence number or an ISO-8601 timestamp") from None
        return 0, ts(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))

    @app.get(f"{API}/audit", tags=["audit"])
    def audit(since: str | None = None, limit: int = Query(500, ge=1, le=5000),
              _: Principal = approver) -> dict[str, Any]:
        seq, since_ts = _since(since)
        events = [e.to_dict() for e in st.iter_audit(since_seq=seq, since_ts=since_ts, limit=limit)]
        return {"events": events, "next_since": events[-1]["seq"] if events else seq}

    @app.get(f"{API}/audit/verify", tags=["audit"])
    def audit_verify(_: Principal = admin) -> dict[str, Any]:
        return verify_chain(st.iter_audit()).to_dict()

    @app.get(f"{API}/audit/export", tags=["audit"])
    def audit_export(since: str | None = None, _: Principal = admin) -> StreamingResponse:
        seq, since_ts = _since(since)

        def gen() -> Iterator[str]:
            for e in st.iter_audit(since_seq=seq, since_ts=since_ts):
                yield canonical_json(e.to_dict()) + "\n"

        return StreamingResponse(gen(), media_type="application/x-ndjson")

    # --- jobs -----------------------------------------------------------------
    def _job_view(j: Any, p: Principal) -> dict[str, Any]:
        d: dict[str, Any] = j.to_dict()
        if not p.has("admin"):  # error text, results and payloads may carry operational detail
            d["has_error"] = bool(d.get("last_error"))
            d["last_error"] = ""
            d["result"] = {}
            d["payload"] = {}
        return d

    @app.get(f"{API}/jobs", tags=["jobs"])
    def jobs(status: Literal["queued", "running", "done", "dead"] | None = None,
             limit: int = Query(100, ge=1, le=1000), p: Principal = viewer) -> dict[str, Any]:
        return {"jobs": [_job_view(j, p) for j in st.list_jobs(status, limit)], "counts": st.count_jobs()}

    @app.get(f"{API}/jobs/{{job_id}}", tags=["jobs"])
    def job(job_id: str, p: Principal = viewer) -> dict[str, Any]:
        j = st.get_job(job_id)
        if j is None:
            raise HTTPException(404, "job not found")
        return _job_view(j, p)

    @app.post(f"{API}/jobs", tags=["jobs"], status_code=202)
    def enqueue_job(body: JobIn, p: Principal = admin) -> dict[str, Any]:
        r = st.enqueue(body.kind, body.payload, idempotency_key=body.idempotency_key,
                       max_attempts=settings.job_max_attempts)
        if r.created:
            st.append_audit(f"token:{p.name}", "job.enqueued", f"job:{r.job_id}", {"kind": body.kind})
        return {"job_id": r.job_id, "created": r.created}

    @app.post(f"{API}/jobs/{{job_id}}/retry", tags=["jobs"])
    def retry_job(job_id: str, p: Principal = admin) -> dict[str, Any]:
        if not st.retry_job(job_id):
            raise HTTPException(409, "job is not dead-lettered (or does not exist)")
        st.append_audit(f"token:{p.name}", "job.retried", f"job:{job_id}", {})
        return {"job_id": job_id, "status": "queued"}

    # --- tokens ---------------------------------------------------------------
    @app.get(f"{API}/tokens", tags=["auth"])
    def tokens(_: Principal = admin) -> dict[str, Any]:
        stored = [t.public() for t in st.list_tokens()]
        static = [{"id": "static:" + t.name, "name": t.name, "role": t.role, "project": t.project or None,
                   "source": "env"}
                  for t in settings.static_tokens]
        return {"tokens": stored + static, "roles": list(ROLES)}

    @app.post(f"{API}/tokens", tags=["auth"], status_code=201)
    def create_token(body: TokenIn, p: Principal = admin) -> dict[str, Any]:
        try:
            auth_mod.check_scope(body.role, body.project)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        if body.project and body.project.lower() not in {x.repo.lower() for x in st.list_projects()}:
            raise HTTPException(422, f"unknown project {body.project!r}; see GET /api/v1/projects")
        token = auth_mod.generate_token()
        rec = st.add_token(body.name, body.role, auth_mod.hash_token(token), project=body.project)
        st.append_audit(f"token:{p.name}", "token.issued", f"token:{rec.id}",
                        {"name": body.name, "role": body.role, "project": body.project})
        return {**rec.public(), "token": token, "note": "store this token now; it is not shown again"}

    @app.delete(f"{API}/tokens/{{token_id}}", tags=["auth"])
    def revoke_token(token_id: str, p: Principal = admin) -> dict[str, Any]:
        if not st.revoke_token(token_id, actor=f"token:{p.name}"):
            raise HTTPException(404, "token not found or already revoked")
        return {"id": token_id, "revoked": True}

    return app
