"""Normalise coding-agent native output into :class:`ExecutionResult`.

The ledger, failure handler and capacity gate read only the neutral
``execution-result.json`` this module writes — never a CLI's native format.

Supported executors (aliases in brackets):

* ``claude-code`` [``claude``] — the ``anthropics/claude-code-action``
  ``execution_file``: a JSON array of stream messages whose final
  ``{"type": "result"}`` message carries ``total_cost_usd``, ``num_turns``,
  ``duration_ms``, ``is_error``, ``subtype`` and ``usage``. The model comes
  from the ``{"type": "system", "subtype": "init"}`` message.
* ``codex`` [``codex-cli``] — ``codex exec --json`` JSONL events (current
  ``thread.started``/``turn.completed``/``item.completed`` shape, plus the
  older ``{"msg": {...}}`` envelope, best effort).
* ``gemini-cli`` [``gemini``] — ``gemini -p ... --output-format json``.
* ``aider`` — best-effort scrape of the console log (``Tokens: ... Cost: ...``).

CLI::

    python -m squadhq.executors.normalize --executor claude-code \\
        --input "$EXECUTION_FILE" --output execution-result.json \\
        [--role implement] [--model claude-sonnet-5-5] [--duration-s 312]

A missing or unparseable input still produces a result (``outcome=failure``,
``error_class=infra``) and the CLI exits 0, so a later ledger/failure step
always has something to read.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from typing import Any, cast

from squadhq.interfaces import ErrorClass, ExecutionResult, Usage

try:  # single source of truth for the subscription-limit strings
    from squadhq.capacity_gate import TRIP_SIGNATURES as _GATE_SIGNATURES

    TRIP_SIGNATURES: tuple[str, ...] = tuple(cast(Sequence[str], _GATE_SIGNATURES))
except Exception:  # pragma: no cover - legacy module unavailable
    TRIP_SIGNATURES = ("hit your session limit", "hit your weekly limit", "hit your Opus limit")

# Quota/usage-limit wording used by other executors' backends.
EXTRA_CAPACITY_SIGNATURES: tuple[str, ...] = (
    "hit your usage limit",
    "usage limit reached",
    "insufficient_quota",
    "exceeded your current quota",
    # Anthropic API keys (a pool of api accounts): prepaid credit / org spend limit.
    "credit balance is too low",
    "reached your specified API usage limits",
)

try:
    from squadhq import failure_handler as _fh

    _classify_legacy: Callable[[str, str], tuple[str, str]] | None = cast(
        Callable[[str, str], tuple[str, str]], _fh.classify
    )
except Exception:  # pragma: no cover - legacy module unavailable
    _classify_legacy = None


def detect_capacity(text: str) -> bool:
    """True when ``text`` contains a subscription/quota-exhausted signature."""
    low = text.lower()
    return any(sig.lower() in low for sig in TRIP_SIGNATURES + EXTRA_CAPACITY_SIGNATURES)


def classify_error(text: str) -> ErrorClass:
    """Map failure text to an :class:`ErrorClass` (failure_handler patterns)."""
    if detect_capacity(text):
        return ErrorClass.CAPACITY
    if _classify_legacy is not None:
        tag, _ = _classify_legacy(text, "failure")
        if tag == "INFRA":
            return ErrorClass.INFRA
        if tag == "TRANSIENT":
            return ErrorClass.TRANSIENT
    return ErrorClass.AGENT


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _int(v: Any) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


def _price(model_ref: str, input_tokens: int | None, output_tokens: int | None,
           cache_read: int = 0, cache_write: int = 0) -> float | None:
    """Price via ``squadhq.providers.price_for`` when available; else ``None``."""
    if not model_ref or input_tokens is None or output_tokens is None:
        return None
    try:
        from squadhq.providers import price_for  # type: ignore[attr-defined,unused-ignore]

        cost = price_for(model_ref, Usage(input_tokens=input_tokens, output_tokens=output_tokens,
                                          cache_read_tokens=cache_read, cache_write_tokens=cache_write))
    except Exception:
        return None
    return round(float(cost), 6) if cost is not None else None


def _infra_failure(executor: str, role: str, model: str, message: str,
                   transcript: str | None = None, duration_s: float | None = None) -> ExecutionResult:
    return ExecutionResult(executor=executor, role=role, model=model, outcome="failure",
                           error_class=ErrorClass.INFRA, error_message=message[:2000],
                           duration_s=duration_s, transcript_path=transcript)


# ---------------------------------------------------------------------------
# claude-code
# ---------------------------------------------------------------------------


def normalize_claude_code(data: Any, *, role: str = "", model: str = "",
                          duration_s: float | None = None) -> ExecutionResult:
    """Normalise a claude-code-action execution file.

    ``data`` is a path, raw JSON text, or the already-decoded list/dict.
    """
    if isinstance(data, (str, os.PathLike)) and os.path.exists(data):
        with open(data, encoding="utf-8", errors="replace") as f:
            data = f.read()
    raw_text = data if isinstance(data, str) else json.dumps(data)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            cap = detect_capacity(data)
            return ExecutionResult(
                executor="claude-code", role=role, model=model, outcome="failure",
                error_class=ErrorClass.CAPACITY if cap else ErrorClass.INFRA,
                error_message=("capacity limit hit" if cap else "unparseable execution file: ") + data[:500],
                capacity_tripped=cap, duration_s=duration_s)
    messages: list[dict[str, Any]]
    if isinstance(data, list):
        messages = [m for m in data if isinstance(m, dict)]
    elif isinstance(data, dict):
        messages = [data]
    else:
        return _infra_failure("claude-code", role, model, "execution file is not a JSON array/object")

    init = next((m for m in messages if m.get("type") == "system" and m.get("subtype") == "init"), {})
    results = [m for m in messages if m.get("type") == "result"]
    result = results[-1] if results else {}
    model = model or str(init.get("model") or "")

    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    assert isinstance(usage, dict)
    cache_read = _int(usage.get("cache_read_input_tokens")) or 0
    cache_write = _int(usage.get("cache_creation_input_tokens")) or 0
    extra: dict[str, Any] = {}
    for k in ("session_id", "subtype"):
        if result.get(k) is not None:
            extra[k] = result[k]
    if cache_read or cache_write:
        extra["cache_read_tokens"] = cache_read
        extra["cache_write_tokens"] = cache_write
    denials = result.get("permission_denials")
    if isinstance(denials, list) and denials:
        extra["permission_denials"] = len(denials)
    if isinstance(result.get("result"), str):
        extra["final_message"] = result["result"][:4000]

    dms = _num(result.get("duration_ms"))
    if duration_s is None and dms is not None:
        duration_s = round(dms / 1000.0, 3)

    subtype = str(result.get("subtype") or "")
    is_error = bool(result.get("is_error")) or (bool(subtype) and subtype != "success")
    # Only a failed run can have hit the plan limit; scanning a successful
    # transcript would trip on an agent merely *reading* the signature text.
    capacity = detect_capacity(raw_text) if (is_error or not result) else False
    if result and not is_error and not capacity:
        outcome, error_class, msg = "success", ErrorClass.NONE, ""
    else:
        parts = [str(result.get("result", ""))] if result else []
        parts += [json.dumps(m)[:2000] for m in messages if m.get("is_error") and m is not result]
        msg = "\n".join(p for p in parts if p) or (raw_text[:2000] if not result else subtype)
        outcome = "failure"
        if capacity:
            error_class = ErrorClass.CAPACITY
        elif subtype == "error_max_turns":
            error_class = ErrorClass.AGENT
            msg = msg or "max turns reached"
        elif not result:
            error_class = classify_error(raw_text)
            if error_class is ErrorClass.AGENT:
                error_class = ErrorClass.UNKNOWN
            msg = msg or "no result message in execution file"
        else:
            error_class = classify_error(msg)

    cost = _num(result.get("total_cost_usd"))
    return ExecutionResult(
        executor="claude-code", role=role, model=model, outcome=cast(Any, outcome),
        error_class=error_class, error_message=msg[:2000],
        cost_usd=round(cost, 6) if cost is not None else None,
        input_tokens=_int(usage.get("input_tokens")), output_tokens=_int(usage.get("output_tokens")),
        turns=_int(result.get("num_turns")), duration_s=duration_s,
        capacity_tripped=capacity, extra=extra,
    )


# ---------------------------------------------------------------------------
# codex
# ---------------------------------------------------------------------------


def normalize_codex(jsonl: str, *, role: str = "", model: str = "",
                    duration_s: float | None = None) -> ExecutionResult:
    """Normalise ``codex exec --json`` output (JSONL text)."""
    events: list[dict[str, Any]] = []
    bad_lines = 0
    for line in jsonl.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            bad_lines += 1
            continue
        if isinstance(ev, dict):
            events.append(ev)
    if not events:
        cap = detect_capacity(jsonl)
        return ExecutionResult(
            executor="codex", role=role, model=model, outcome="failure",
            error_class=ErrorClass.CAPACITY if cap else ErrorClass.INFRA,
            error_message=("capacity limit hit: " if cap else "no JSON events in codex output: ") + jsonl[:500],
            capacity_tripped=cap, duration_s=duration_s)

    inp = out = cached = 0
    seen_usage = False
    turns = 0
    errors: list[str] = []
    final_message = ""
    commands = 0
    thread_id: str | None = None
    for ev in events:
        typ = ev.get("type")
        if typ is None and isinstance(ev.get("msg"), dict):
            # Older envelope: {"id": "...", "msg": {"type": ...}}
            msg = ev["msg"]
            mtype = msg.get("type")
            if mtype == "token_count":
                info = msg.get("info") if isinstance(msg.get("info"), dict) else None
                total = info.get("total_token_usage") if info else msg
                if isinstance(total, dict):
                    seen_usage = True
                    inp = _int(total.get("input_tokens")) or 0
                    out = _int(total.get("output_tokens")) or 0
                    cached = _int(total.get("cached_input_tokens")) or 0
            elif mtype == "task_complete":
                turns += 1
                if isinstance(msg.get("last_agent_message"), str):
                    final_message = msg["last_agent_message"]
            elif mtype == "agent_message" and isinstance(msg.get("message"), str):
                final_message = msg["message"]
            elif mtype == "exec_command_begin":
                commands += 1
            elif mtype in ("error", "stream_error"):
                errors.append(str(msg.get("message", "")))
            elif mtype == "session_configured" and not model:
                model = str(msg.get("model") or "")
            continue
        if typ == "thread.started":
            thread_id = ev.get("thread_id")
        elif typ == "turn.completed":
            turns += 1
            u = ev.get("usage")
            if isinstance(u, dict):
                seen_usage = True
                inp += _int(u.get("input_tokens")) or 0
                out += _int(u.get("output_tokens")) or 0
                cached += _int(u.get("cached_input_tokens")) or 0
        elif typ == "turn.failed":
            err = ev.get("error")
            errors.append(str(err.get("message", "")) if isinstance(err, dict) else str(err))
        elif typ == "error":
            errors.append(str(ev.get("message", "")))
        elif typ == "item.completed" and isinstance(ev.get("item"), dict):
            item = ev["item"]
            itype = item.get("type") or item.get("item_type")
            if itype in ("agent_message", "assistant_message") and isinstance(item.get("text"), str):
                final_message = item["text"]
            elif itype == "command_execution":
                commands += 1

    capacity = detect_capacity(jsonl)
    extra: dict[str, Any] = {"commands": commands}
    if thread_id:
        extra["thread_id"] = thread_id
    if cached:
        extra["cache_read_tokens"] = cached
    if final_message:
        extra["final_message"] = final_message[:4000]
    if bad_lines:
        extra["unparsed_lines"] = bad_lines

    if errors or capacity:
        msg = "\n".join(e for e in errors if e)[:2000] or "capacity limit hit"
        outcome = "failure"
        error_class = ErrorClass.CAPACITY if capacity else classify_error(msg)
    elif turns == 0:
        outcome, error_class, msg = "failure", ErrorClass.UNKNOWN, "codex output has no completed turn"
    else:
        outcome, error_class, msg = "success", ErrorClass.NONE, ""

    in_tok = inp if seen_usage else None
    out_tok = out if seen_usage else None
    return ExecutionResult(
        executor="codex", role=role, model=model, outcome=cast(Any, outcome), error_class=error_class,
        error_message=msg, cost_usd=_price(model if "/" in model else f"openai/{model}" if model else "",
                                           in_tok, out_tok, cached),
        input_tokens=in_tok, output_tokens=out_tok, turns=turns, duration_s=duration_s,
        capacity_tripped=capacity, extra=extra,
    )


# ---------------------------------------------------------------------------
# gemini-cli
# ---------------------------------------------------------------------------


def normalize_gemini_cli(data: Any, *, role: str = "", model: str = "",
                         duration_s: float | None = None) -> ExecutionResult:
    """Normalise ``gemini --output-format json`` output (text or decoded dict)."""
    raw_text = data if isinstance(data, str) else json.dumps(data)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            cap = detect_capacity(data)
            return ExecutionResult(
                executor="gemini-cli", role=role, model=model, outcome="failure",
                error_class=ErrorClass.CAPACITY if cap else ErrorClass.INFRA,
                error_message=data[:500] or "empty gemini output", capacity_tripped=cap,
                duration_s=duration_s)
    if not isinstance(data, dict):
        return _infra_failure("gemini-cli", role, model, "gemini output is not a JSON object")

    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    assert isinstance(stats, dict)
    models = stats.get("models") if isinstance(stats.get("models"), dict) else {}
    assert isinstance(models, dict)
    inp = out = cached = requests = api_errors = 0
    seen = False
    for name, m in models.items():
        if not isinstance(m, dict):
            continue
        model = model or str(name)
        tok = m.get("tokens") if isinstance(m.get("tokens"), dict) else {}
        assert isinstance(tok, dict)
        if tok:
            seen = True
        inp += _int(tok.get("prompt")) or 0
        out += (_int(tok.get("candidates")) or 0) + (_int(tok.get("thoughts")) or 0)
        cached += _int(tok.get("cached")) or 0
        api = m.get("api") if isinstance(m.get("api"), dict) else {}
        assert isinstance(api, dict)
        requests += _int(api.get("totalRequests")) or 0
        api_errors += _int(api.get("totalErrors")) or 0
        lat = _num(api.get("totalLatencyMs"))
        if duration_s is None and lat is not None:
            duration_s = round(lat / 1000.0, 3)

    extra: dict[str, Any] = {"api_requests": requests, "api_errors": api_errors}
    tools = stats.get("tools")
    if isinstance(tools, dict) and _int(tools.get("totalCalls")) is not None:
        extra["tool_calls"] = _int(tools.get("totalCalls"))
    if cached:
        extra["cache_read_tokens"] = cached
    if isinstance(data.get("response"), str):
        extra["final_message"] = data["response"][:4000]

    err = data.get("error")
    capacity = detect_capacity(raw_text) if err else False
    if err:
        emsg = str(err.get("message", "")) if isinstance(err, dict) else str(err)
        if isinstance(err, dict) and err.get("type"):
            emsg = f"{err['type']}: {emsg}"
        outcome, msg = "failure", emsg[:2000]
        error_class = ErrorClass.CAPACITY if capacity else classify_error(emsg)
    elif not isinstance(data.get("response"), str):
        outcome, error_class, msg = "failure", ErrorClass.UNKNOWN, "gemini output has no response"
    else:
        outcome, error_class, msg = "success", ErrorClass.NONE, ""

    in_tok = inp if seen else None
    out_tok = out if seen else None
    return ExecutionResult(
        executor="gemini-cli", role=role, model=model, outcome=cast(Any, outcome), error_class=error_class,
        error_message=msg, cost_usd=_price(model if "/" in model else f"gemini/{model}" if model else "",
                                           in_tok, out_tok, cached),
        input_tokens=in_tok, output_tokens=out_tok, turns=requests or None, duration_s=duration_s,
        capacity_tripped=capacity, extra=extra,
    )


# ---------------------------------------------------------------------------
# aider (best effort)
# ---------------------------------------------------------------------------

_AIDER_TOKENS_RE = re.compile(
    r"Tokens:\s*([\d.,]+)\s*([km]?)\s*sent.*?([\d.,]+)\s*([km]?)\s*received\.?"
    r"(?:\s*Cost:\s*\$([\d.]+)\s*message,\s*\$([\d.]+)\s*session)?",
    re.I,
)
_AIDER_MODEL_RE = re.compile(r"^(?:Main model|Model):\s*(\S+)", re.I | re.M)
_AIDER_ERROR_RE = re.compile(
    r"^.*(?:litellm\.\w+Error|APIError|RateLimitError|AuthenticationError|APIConnectionError"
    r"|Traceback \(most recent call last\)|^Error:).*$",
    re.I | re.M,
)


def _scaled(num: str, suffix: str) -> int:
    v = float(num.replace(",", ""))
    return int(round(v * {"k": 1_000, "m": 1_000_000}.get(suffix.lower(), 1)))


def normalize_aider(text: str, *, role: str = "", model: str = "",
                    duration_s: float | None = None) -> ExecutionResult:
    """Best-effort scrape of an aider console log; the last ``Tokens:`` line wins."""
    matches = list(_AIDER_TOKENS_RE.finditer(text))
    if not model:
        mm = _AIDER_MODEL_RE.search(text)
        model = mm.group(1) if mm else ""
    inp = out = None
    cost: float | None = None
    if matches:
        last = matches[-1]
        inp = _scaled(last.group(1), last.group(2))
        out = _scaled(last.group(3), last.group(4))
        if last.group(6):
            cost = round(float(last.group(6)), 6)
    capacity = detect_capacity(text)
    errors = [m.group(0).strip() for m in _AIDER_ERROR_RE.finditer(text)]
    extra: dict[str, Any] = {"best_effort": True}
    commits = re.findall(r"^Commit ([0-9a-f]{7,40})\b", text, re.M)
    if commits:
        extra["commits"] = commits
    if capacity or errors:
        msg = "\n".join(errors)[:2000] or "capacity limit hit"
        outcome = "failure"
        error_class = ErrorClass.CAPACITY if capacity else classify_error(msg)
    elif not text.strip():
        outcome, error_class, msg = "failure", ErrorClass.INFRA, "empty aider log"
    else:
        outcome, error_class, msg = "success", ErrorClass.NONE, ""
    return ExecutionResult(
        executor="aider", role=role, model=model, outcome=cast(Any, outcome), error_class=error_class,
        error_message=msg, cost_usd=cost, input_tokens=inp, output_tokens=out,
        turns=len(matches) or None, duration_s=duration_s, capacity_tripped=capacity, extra=extra,
    )


# ---------------------------------------------------------------------------
# registry + entry point
# ---------------------------------------------------------------------------

Normalizer = Callable[..., ExecutionResult]

NORMALIZERS: dict[str, Normalizer] = {
    "claude-code": normalize_claude_code,
    "codex": normalize_codex,
    "gemini-cli": normalize_gemini_cli,
    "aider": normalize_aider,
}
ALIASES = {"claude": "claude-code", "claude_code": "claude-code", "codex-cli": "codex",
           "gemini": "gemini-cli", "gemini_cli": "gemini-cli"}


def canonical_executor(name: str) -> str:
    key = name.strip().lower()
    key = ALIASES.get(key, key)
    if key not in NORMALIZERS:
        raise ValueError(f"unknown executor {name!r}; expected one of {sorted(NORMALIZERS)}")
    return key


def normalize(executor: str, native_path: str | os.PathLike[str], *, role: str = "", model: str = "",
              duration_s: float | None = None) -> ExecutionResult:
    """Read ``native_path`` and normalise it with the executor's normaliser.

    Never raises for missing/unreadable/unparseable input — returns an
    ``outcome="failure"`` / ``error_class=infra`` result instead.
    """
    name = canonical_executor(executor)
    path = os.fspath(native_path)
    if not path or not os.path.exists(path):
        return _infra_failure(name, role, model, f"executor output not found: {path or '<empty>'}",
                              duration_s=duration_s)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        return _infra_failure(name, role, model, f"cannot read {path}: {e}", path, duration_s)
    if not text.strip():
        return _infra_failure(name, role, model, f"executor output is empty: {path}", path, duration_s)
    try:
        result = NORMALIZERS[name](text, role=role, model=model, duration_s=duration_s)
    except Exception as e:  # a normaliser bug must not hide the run's outcome
        return _infra_failure(name, role, model, f"normaliser error: {e!r}", path, duration_s)
    result.transcript_path = path
    return result


def main(argv: Sequence[str] | None = None) -> int:
    from squadhq.executors.result import write_result

    p = argparse.ArgumentParser(prog="python -m squadhq.executors.normalize",
                                description="Normalise coding-agent output into execution-result.json")
    p.add_argument("--executor", required=True, help="claude-code | codex | gemini-cli | aider")
    p.add_argument("--input", required=True, help="native output file (execution_file, JSONL, JSON, log)")
    p.add_argument("--output", default="execution-result.json")
    p.add_argument("--role", default="")
    p.add_argument("--model", default="")
    p.add_argument("--duration-s", type=float, default=None)
    p.add_argument("--account", default="", help="pool account id the run used (from the prep job; not a secret)")
    args = p.parse_args(argv)
    try:
        name = canonical_executor(args.executor)
    except ValueError as e:
        print(f"normalize: {e}", file=sys.stderr)
        return 2
    result = normalize(name, args.input, role=args.role, model=args.model, duration_s=args.duration_s)
    if args.account:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,33}", args.account):
            print(f"normalize: ignoring malformed --account {args.account[:40]!r}", file=sys.stderr)
        else:
            result.account = args.account
    write_result(result, args.output)
    cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "unpriced"
    print(f"normalize: {result.executor} role={result.role or '-'} model={result.model or '-'} "
          f"outcome={result.outcome} error_class={result.error_class.value} cost={cost} "
          f"turns={result.turns if result.turns is not None else '-'} "
          f"capacity_tripped={str(result.capacity_tripped).lower()} -> {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
