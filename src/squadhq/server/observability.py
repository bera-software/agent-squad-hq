"""Structured logging, request ids, Prometheus metrics and optional OpenTelemetry.

* JSON logs (one object per line) carrying ``request_id`` / ``job_id`` from
  context variables and any ``extra=`` fields.
* A hand-rolled Prometheus registry (counters, gauges, a summary-style
  duration) rendered in text exposition format 0.0.4 — no client library.
* OpenTelemetry is used only when ``SQUADHQ_OTEL_ENABLED=true`` *and* the
  ``opentelemetry`` packages are importable; otherwise spans are no-ops.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sys
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
job_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("job_id", default="")

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid, jid = request_id_var.get(), job_id_var.get()
        if rid:
            out["request_id"] = rid
        if jid:
            out["job_id"] = jid
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class _StdoutHandler(logging.StreamHandler):  # type: ignore[type-arg,unused-ignore]
    """Writes to whatever ``sys.stdout`` is at emit time (survives stream swaps)."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stdout
        super().emit(record)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_squadhq", False):
            root.removeHandler(h)
    handler = _StdoutHandler(sys.stdout)
    handler._squadhq = True  # type: ignore[attr-defined]
    handler.setFormatter(JSONFormatter() if fmt == "json" else
                         logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)


# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------

def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(names: tuple[str, ...], values: tuple[str, ...]) -> str:
    if not names:
        return ""
    return "{" + ",".join(f'{n}="{_escape(v)}"' for n, v in zip(names, values, strict=True)) + "}"


class _Metric:
    kind = "untyped"

    def __init__(self, name: str, help_: str, labelnames: tuple[str, ...] = ()) -> None:
        self.name, self.help, self.labelnames = name, help_, labelnames
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def _key(self, labels: dict[str, str]) -> tuple[str, ...]:
        return tuple(str(labels.get(n, "")) for n in self.labelnames)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]
        with self._lock:
            for k, v in sorted(self._values.items()):
                lines.append(f"{self.name}{_labels(self.labelnames, k)} {v:g}")
        return lines

    def get(self, **labels: str) -> float:
        return self._values.get(self._key(labels), 0.0)


class Counter(_Metric):
    kind = "counter"

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        k = self._key(labels)
        with self._lock:
            self._values[k] = self._values.get(k, 0.0) + amount


class Gauge(_Metric):
    kind = "gauge"

    def set(self, value: float, **labels: str) -> None:
        with self._lock:
            self._values[self._key(labels)] = value


class Registry:
    def __init__(self) -> None:
        self._metrics: list[_Metric] = []
        self._collectors: list[Callable[[], None]] = []

    def counter(self, name: str, help_: str, labelnames: tuple[str, ...] = ()) -> Counter:
        c = Counter(name, help_, labelnames)
        self._metrics.append(c)
        return c

    def gauge(self, name: str, help_: str, labelnames: tuple[str, ...] = ()) -> Gauge:
        g = Gauge(name, help_, labelnames)
        self._metrics.append(g)
        return g

    def on_collect(self, fn: Callable[[], None]) -> None:
        self._collectors.append(fn)

    def render(self) -> str:
        for fn in self._collectors:
            try:
                fn()
            except Exception:
                logging.getLogger("squadhq.metrics").exception("metrics collector failed")
        lines: list[str] = []
        for m in self._metrics:
            lines.extend(m.render())
        return "\n".join(lines) + "\n"


class ServerMetrics:
    def __init__(self) -> None:
        r = self.registry = Registry()
        self.http_requests = r.counter("squadhq_http_requests_total", "HTTP requests", ("method", "route", "status"))
        self.http_seconds_sum = r.counter("squadhq_http_request_duration_seconds_sum",
                                          "Total HTTP request time", ("route",))
        self.http_seconds_count = r.counter("squadhq_http_request_duration_seconds_count",
                                            "HTTP requests timed", ("route",))
        self.webhooks = r.counter("squadhq_webhooks_total", "Webhook deliveries", ("source", "result"))
        self.approval_decisions = r.counter("squadhq_approval_decisions_total", "Approval decisions",
                                            ("decision",))
        self.jobs = r.gauge("squadhq_jobs", "Jobs in the queue by status", ("status",))
        self.approvals_pending = r.gauge("squadhq_approvals_pending", "Approvals awaiting a human")
        self.up = r.gauge("squadhq_up", "1 when the store answers")


# ---------------------------------------------------------------------------
# OpenTelemetry (optional)
# ---------------------------------------------------------------------------

class _Tracing:
    def __init__(self) -> None:
        self.tracer: Any = None

    def enable(self, service_name: str = "agent-squad-hq") -> bool:
        try:
            from opentelemetry import trace  # type: ignore[import-not-found,unused-ignore]
        except ImportError:
            logging.getLogger("squadhq.otel").warning("SQUADHQ_OTEL_ENABLED but opentelemetry is not installed")
            return False
        self.tracer = trace.get_tracer(service_name)
        return True

    @contextlib.contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[None]:
        if self.tracer is None:
            yield
            return
        with self.tracer.start_as_current_span(name) as sp:  # pragma: no cover - needs otel installed
            for k, v in attrs.items():
                sp.set_attribute(k, str(v))
            yield


tracing = _Tracing()


def instrument_fastapi(app: Any) -> bool:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        return False
    FastAPIInstrumentor.instrument_app(app)  # pragma: no cover - needs otel installed
    return True  # pragma: no cover
