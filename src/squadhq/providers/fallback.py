"""Retry + model-fallback policy for hub-loop LLM calls.

Policy (one bounded budget of ``max_attempts`` calls in total):

1. A *transient* failure (timeout, 429, 5xx) retries the **same** model with
   exponential backoff (``backoff * 2**attempt`` s, or ``Retry-After``).
2. A failure that is **model-specific** (529 overloaded, model not found /
   deprecated / unavailable) switches to the fallback immediately.
3. A **provider-wide** failure (auth, billing, quota/capacity) switches only
   when the fallback is on a *different* provider; otherwise it is raised.
4. Any other fatal error (400 bad request ...) is raised at once.
5. The final attempt always goes to the fallback if it has not been tried —
   two failed tries on the primary are evidence enough to try another model.

At most one switch happens; the fallback is never "fallen back" from.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from squadhq.interfaces import AdapterError, CapacityError, LLMProvider, LLMReply, Message

log = logging.getLogger(__name__)


@dataclass
class FallbackResult:
    reply: LLMReply
    used_ref: str  # the model ref that answered (as configured: "provider/model" or bare)
    provider: str
    model: str
    used_fallback: bool
    attempts: int
    errors: list[str] = field(default_factory=list)


ProviderFactory = Callable[[str], LLMProvider]


def complete_with_fallback(
    primary_ref: str,
    fallback_ref: str | None = None,
    *,
    system: str,
    messages: list[Message],
    max_tokens: int,
    web_search: bool = False,
    json_mode: bool = False,
    timeout: float = 120.0,
    max_attempts: int = 3,
    backoff: float = 1.0,
    provider_factory: ProviderFactory | None = None,
    on_event: Callable[[str], None] | None = None,
) -> FallbackResult:
    """Complete with the primary model, falling back per the module policy.

    Raises the last :class:`~squadhq.interfaces.AdapterError` when every
    allowed attempt failed. ``provider_factory(name)`` defaults to
    :func:`squadhq.providers.get_provider`.
    """
    from squadhq.providers import get_provider, resolve

    factory = provider_factory or get_provider
    say = on_event or (lambda m: log.info("%s", m))
    if fallback_ref == primary_ref:
        fallback_ref = None
    current = primary_ref
    switched = False
    errors: list[str] = []
    last: AdapterError | None = None

    def switch(reason: str) -> None:
        nonlocal current, switched
        say(f"llm: {current!r} failed ({reason}) — switching to fallback {fallback_ref!r}")
        assert fallback_ref is not None
        current = fallback_ref
        switched = True

    attempts = max(1, max_attempts)
    for attempt in range(attempts):
        can_switch = bool(fallback_ref) and not switched
        if attempt == attempts - 1 and attempt > 0 and can_switch:
            switch(f"{attempt} failed attempt(s)")
            can_switch = False
        prov_name, model = resolve(current)
        try:
            provider = factory(prov_name)
            reply = provider.complete(model=model, system=system, messages=messages, max_tokens=max_tokens,
                                      web_search=web_search, json_mode=json_mode, timeout=timeout)
            return FallbackResult(reply=reply, used_ref=current, provider=prov_name, model=model,
                                  used_fallback=switched, attempts=attempt + 1, errors=errors)
        except AdapterError as e:
            last = e
            errors.append(f"{current}: {e}")
            other_provider = bool(fallback_ref) and resolve(fallback_ref or "")[0] != prov_name
            if e.model_specific and can_switch:
                switch("model-specific")
                continue
            if (e.provider_wide or isinstance(e, CapacityError)) and can_switch and other_provider:
                switch("provider-wide")
                continue
            if not e.transient or isinstance(e, CapacityError):
                raise
            if attempt == attempts - 1:
                raise
            delay = e.retry_after if e.retry_after is not None else backoff * (2 ** attempt)
            say(f"llm: transient failure on {current!r} (attempt {attempt + 1}/{attempts}) — "
                f"retrying in {delay:g}s: {e}")
            time.sleep(min(delay, 60.0))
    assert last is not None  # pragma: no cover - loop always returns or raises
    raise last  # pragma: no cover
