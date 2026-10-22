"""Shared plumbing for HTTP-based LLM provider adapters."""
from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Any

from squadhq import http
from squadhq.http import Transport
from squadhq.interfaces import AdapterError, Capability, FatalError, Message, Usage
from squadhq.providers import prices

log = logging.getLogger(__name__)

STOP_REASONS = ("end", "max_tokens", "tool_use", "refusal", "other")


def env(*names: str, default: str | None = None) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


class HTTPProvider:
    """Base class: price/capabilities from the catalogue, JSON POST via :mod:`squadhq.http`.

    Provider HTTP calls use ``retries=0`` — retry and model fallback are the
    job of :func:`squadhq.providers.complete_with_fallback`.
    """

    name = "base"
    #: catalogue section used for prices/capabilities (defaults to ``name``)
    catalogue_name: str | None = None

    def __init__(self, *, transport: Transport | None = None) -> None:
        self.transport = transport

    # -- catalogue -------------------------------------------------------
    @property
    def _cat(self) -> str:
        return self.catalogue_name or self.name

    def capabilities(self, model: str) -> set[Capability]:
        return prices.capabilities(self._cat, model)

    def price(self, model: str, usage: Usage) -> Decimal | None:
        return prices.price(self._cat, model, usage)

    # -- http ------------------------------------------------------------
    def _post(self, url: str, body: dict[str, Any], headers: dict[str, str], timeout: float) -> Any:
        try:
            return http.request("POST", url, headers=headers, json_body=body, timeout=timeout,
                                retries=0, transport=self.transport).json() or {}
        except AdapterError as e:
            # Prefix the provider so a traceback/job log says who failed.
            e.args = (f"{self.name}: {e.args[0] if e.args else ''}",)
            raise

    def _require(self, value: str | None, what: str) -> str:
        if not value:
            raise FatalError(f"{self.name}: {what} is not set", provider_wide=True)
        return value


def warn_unsupported(provider: str, feature: str, model: str) -> None:
    log.warning("%s: %s not supported for %s; ignoring", provider, feature, model)


def to_role_messages(messages: list[Message]) -> list[dict[str, str]]:
    return [{"role": m.role, "content": m.content} for m in messages]


def usage_of(input_tokens: Any = 0, output_tokens: Any = 0, cache_read: Any = 0, cache_write: Any = 0) -> Usage:
    def n(v: Any) -> int:
        return int(v) if isinstance(v, (int, float)) else 0

    return Usage(input_tokens=max(0, n(input_tokens)), output_tokens=n(output_tokens),
                 cache_read_tokens=n(cache_read), cache_write_tokens=n(cache_write))
