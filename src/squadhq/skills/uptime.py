"""Uptime watcher: deterministic health checks with incident open/close rules.

No model is involved. Config (``skills/uptime-watcher/skill.yml`` ``config:``,
or a ``squadhq.yml`` ``uptime:`` block, or a bare mapping). In ``squadhq.yml``
without ``uptime.checks``, every project environment URL is checked
(:func:`derive_checks`)::

    defaults: {interval: "5m", timeout_s: 10, failure_threshold: 3,
               recovery_threshold: 2, severity: P0, auto_close: true,
               incident_label: incident}
    checks:
      - {name: example-home, url: "https://example.com/healthz", method: GET,
         expect_status: [200], expect_body_regex: "ok", timeout_s: 10}

Rules: ``failure_threshold`` consecutive failures open one incident issue
(labels ``severity`` + ``incident_label``); ``recovery_threshold`` consecutive
successes close it when ``auto_close`` is true, otherwise comment that it
recovered. State is a JSON file so runs are idempotent.

CLI::

    python -m squadhq.skills.uptime validate --config skills/uptime-watcher/skill.yml
    python -m squadhq.skills.uptime run --config squadhq.yml --state state/uptime.json

``run`` prints the actions as JSON (``open_incident`` / ``close_incident`` /
``comment_recovered``); the workflow step applies them with the bot token.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "interval": "5m", "timeout_s": 10, "failure_threshold": 3, "recovery_threshold": 2,
    "severity": "P0", "auto_close": True, "incident_label": "incident",
}
SEVERITIES = ("P0", "P1", "P2", "P3")
METHODS = ("GET", "HEAD")
_INTERVAL_RE = re.compile(r"^(\d+)([smh])$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MAX_TIMEOUT_S = 60


def parse_interval(value: str) -> int:
    m = _INTERVAL_RE.match(str(value))
    if not m:
        raise ValueError(f"interval must look like 30s, 5m or 1h (got {value!r})")
    n, unit = int(m.group(1)), m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600}[unit]


def _validate_settings(settings: dict[str, Any], where: str, errors: list[str]) -> None:
    if "interval" in settings:
        try:
            if parse_interval(settings["interval"]) < 60:
                errors.append(f"{where}.interval must be at least 1m (GitHub schedules)")
        except ValueError as exc:
            errors.append(f"{where}: {exc}")
    for key in ("failure_threshold", "recovery_threshold"):
        if key in settings and (not isinstance(settings[key], int) or isinstance(settings[key], bool)
                                or settings[key] < 1):
            errors.append(f"{where}.{key} must be an integer >= 1")
    if "timeout_s" in settings:
        t = settings["timeout_s"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t <= MAX_TIMEOUT_S:
            errors.append(f"{where}.timeout_s must be between 0 and {MAX_TIMEOUT_S}")
    if "severity" in settings and settings["severity"] not in SEVERITIES:
        errors.append(f"{where}.severity must be one of {', '.join(SEVERITIES)}")
    if "auto_close" in settings and not isinstance(settings["auto_close"], bool):
        errors.append(f"{where}.auto_close must be true or false")
    if "incident_label" in settings and not (isinstance(settings["incident_label"], str)
                                             and settings["incident_label"].strip()):
        errors.append(f"{where}.incident_label must be a non-empty string")


def validate_config(cfg: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(cfg, dict):
        return ["config must be a mapping"]
    defaults = cfg.get("defaults", {})
    if not isinstance(defaults, dict):
        errors.append("defaults must be a mapping")
        defaults = {}
    unknown = set(defaults) - set(DEFAULTS)
    if unknown:
        errors.append(f"defaults: unknown keys {sorted(unknown)}")
    _validate_settings(defaults, "defaults", errors)
    checks = cfg.get("checks")
    if not isinstance(checks, list) or not checks:
        errors.append("checks must be a non-empty list")
        return errors
    names: set[str] = set()
    allowed = {"name", "url", "method", "expect_status", "expect_body_regex", "timeout_s",
               "failure_threshold", "recovery_threshold", "severity", "auto_close", "incident_label", "repo"}
    for i, check in enumerate(checks):
        where = f"checks[{i}]"
        if not isinstance(check, dict):
            errors.append(f"{where} must be a mapping")
            continue
        unknown = set(check) - allowed
        if unknown:
            errors.append(f"{where}: unknown keys {sorted(unknown)}")
        name = check.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            errors.append(f"{where}.name must be lowercase kebab-case")
        elif name in names:
            errors.append(f"{where}.name {name!r} is duplicated")
        else:
            names.add(name)
        url = check.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            errors.append(f"{where}.url must be an https:// URL")
        if check.get("method", "GET") not in METHODS:
            errors.append(f"{where}.method must be one of {', '.join(METHODS)}")
        status = check.get("expect_status", [200])
        if not isinstance(status, list) or not status or not all(
                isinstance(s, int) and not isinstance(s, bool) and 100 <= s <= 599 for s in status):
            errors.append(f"{where}.expect_status must be a list of HTTP status codes")
        if "expect_body_regex" in check:
            try:
                re.compile(str(check["expect_body_regex"]))
            except re.error as exc:
                errors.append(f"{where}.expect_body_regex: {exc}")
        _validate_settings(check, where, errors)
    return errors


def effective(check: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULTS)
    merged.update(cfg.get("defaults") or {})
    merged.update(check)
    merged.setdefault("method", "GET")
    merged.setdefault("expect_status", [200])
    return merged


@dataclass
class ProbeResult:
    name: str
    ok: bool
    status: int | None = None
    latency_ms: int = 0
    error: str = ""


Fetcher = Callable[[str, str, float], tuple[int, str]]


def http_fetch(url: str, method: str, timeout: float) -> tuple[int, str]:
    if not url.startswith("https://"):
        raise ValueError("only https:// URLs are probed")
    req = urllib.request.Request(  # noqa: S310 - https enforced above
        url, method=method, headers={"User-Agent": "squadhq-uptime-watcher/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https enforced above
            return int(resp.status), resp.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), ""


def probe(check: dict[str, Any], fetch: Fetcher = http_fetch) -> ProbeResult:
    started = time.monotonic()
    try:
        status, body = fetch(check["url"], check.get("method", "GET"), float(check.get("timeout_s", 10)))
    except Exception as exc:  # network errors are a failed probe, not a crash
        return ProbeResult(name=check["name"], ok=False, error=f"{type(exc).__name__}: {exc}"[:300],
                           latency_ms=int((time.monotonic() - started) * 1000))
    latency = int((time.monotonic() - started) * 1000)
    if status not in check.get("expect_status", [200]):
        return ProbeResult(name=check["name"], ok=False, status=status, latency_ms=latency,
                           error=f"unexpected status {status}")
    pattern = check.get("expect_body_regex")
    if pattern and check.get("method", "GET") != "HEAD" and not re.search(str(pattern), body):
        return ProbeResult(name=check["name"], ok=False, status=status, latency_ms=latency,
                           error="body did not match expect_body_regex")
    return ProbeResult(name=check["name"], ok=True, status=status, latency_ms=latency)


def step(state: dict[str, Any], check: dict[str, Any], result: ProbeResult, *, now: str) -> list[dict[str, Any]]:
    """Advance ``state[check name]`` with one probe result; return actions."""
    entry = state.setdefault(check["name"], {"failures": 0, "successes": 0, "incident_open": False})
    actions: list[dict[str, Any]] = []
    if result.ok:
        entry["failures"] = 0
        entry["successes"] = int(entry.get("successes", 0)) + 1
        if entry.get("incident_open") and entry["successes"] >= int(check["recovery_threshold"]):
            entry["incident_open"] = False
            actions.append({
                "type": "close_incident" if check["auto_close"] else "comment_recovered",
                "check": check["name"], "issue": entry.get("issue"), "at": now,
                "body": f"[Uptime] {check['name']} recovered: {entry['successes']} consecutive successful checks.",
            })
    else:
        entry["successes"] = 0
        entry["failures"] = int(entry.get("failures", 0)) + 1
        entry["last_error"] = result.error
        if not entry.get("incident_open") and entry["failures"] >= int(check["failure_threshold"]):
            entry["incident_open"] = True
            entry["opened_at"] = now
            actions.append({
                "type": "open_incident", "check": check["name"], "at": now,
                "repo": check.get("repo"),
                "title": f"Incident: {check['name']} down",
                "labels": [check["severity"], check["incident_label"]],
                "body": (f"[Uptime] {check['url']} failed {entry['failures']} consecutive checks.\n\n"
                         f"Last error: {result.error or 'n/a'} (status {result.status}).\n"
                         f"Opened automatically at {now}; it closes after "
                         f"{check['recovery_threshold']} consecutive successes."
                         + ("" if check["auto_close"] else " Auto-close is off: a human closes it.")),
            })
    return actions


def run_checks(cfg: dict[str, Any], state: dict[str, Any], *, fetch: Fetcher = http_fetch,
               now: str | None = None) -> tuple[list[ProbeResult], list[dict[str, Any]]]:
    now = now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    results: list[ProbeResult] = []
    actions: list[dict[str, Any]] = []
    for raw in cfg["checks"]:
        check = effective(raw, cfg)
        result = probe(check, fetch)
        results.append(result)
        actions.extend(step(state, check, result, now=now))
    return results, actions


# A derived check is "up" while the host answers below 500: an API root that
# asks for credentials (401) or redirects is alive; 5xx or no answer is not.
REACHABLE = list(range(200, 500))
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(*parts: str) -> str:
    return _SLUG_RE.sub("-", "-".join(parts).lower()).strip("-")


def derive_checks(projects: Any) -> list[dict[str, Any]]:
    """Checks from ``projects[].environments`` (url, api_url, urls.*), so every
    onboarded project is watched without an ``uptime:`` block. Incidents go to
    the project's repository; non-production environments are P2."""
    checks: list[dict[str, Any]] = []
    for proj in projects if isinstance(projects, list) else []:
        if not isinstance(proj, dict) or not isinstance(proj.get("environments"), dict):
            continue
        name = str(proj.get("name") or proj.get("repo") or "project")
        for env, spec in proj["environments"].items():
            if not isinstance(spec, dict):
                continue
            urls: dict[str, Any] = {"site": spec.get("url"), "api": spec.get("api_url")}
            extra = spec.get("urls")
            if isinstance(extra, dict):
                urls.update(extra)
            for kind, url in urls.items():
                if not isinstance(url, str) or not url.startswith("https://"):
                    continue
                check = {"name": _slug(name, str(env), str(kind)), "url": url, "expect_status": REACHABLE}
                if proj.get("repo"):
                    check["repo"] = proj["repo"]
                if str(env) != "production":
                    check["severity"] = "P2"
                checks.append(check)
    return checks


def load_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping")
    if isinstance(data.get("config"), dict) and data.get("id") == "uptime-watcher":
        cfg: dict[str, Any] = data["config"]
        return cfg
    if isinstance(data.get("uptime"), dict) or "projects" in data:
        # squadhq.yml: the uptime: block, with checks derived from the projects'
        # environments when it lists none.
        cfg = dict(data.get("uptime") or {})
        if not cfg.get("checks"):
            cfg["checks"] = derive_checks(data.get("projects"))
        return cfg
    return data


def _load_state(path: str | None) -> dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def main(argv: list[str] | None = None, *, fetch: Fetcher = http_fetch) -> int:
    p = argparse.ArgumentParser(prog="python -m squadhq.skills.uptime")
    sub = p.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate")
    v.add_argument("--config", required=True)
    r = sub.add_parser("run")
    r.add_argument("--config", required=True)
    r.add_argument("--state", help="JSON state file (read and rewritten)")
    args = p.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    errors = validate_config(cfg)
    if errors:
        for e in errors:
            print(f"ERROR {e}", file=sys.stderr)
        return 1
    if args.command == "validate":
        print(f"OK: {len(cfg['checks'])} checks")
        return 0
    state = _load_state(args.state)
    results, actions = run_checks(cfg, state, fetch=fetch)
    if args.state:
        os.makedirs(os.path.dirname(os.path.abspath(args.state)), exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
    print(json.dumps({"results": [asdict(r) for r in results], "actions": actions}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
