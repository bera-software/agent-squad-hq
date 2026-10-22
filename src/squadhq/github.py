"""The one place that shells out to the ``gh`` CLI.

Every control-plane module that talks to GitHub goes through
:class:`GitHubClient` (usually via :func:`client`), so timeouts, retries on
transient failures, pagination and "find or create by title" behave the same
everywhere. Modules keep thin seam functions of their own (``sh``, ``gh``,
``gh_json``...) that delegate here, so their tests can keep mocking at the
module boundary.

Design rules (docs/architecture/CONVENTIONS.md):

* every call has a timeout (``SQUADHQ_GH_TIMEOUT``, default 60s);
* transient failures (HTTP 5xx/429, secondary rate limits, network resets,
  timeouts) are retried with exponential backoff; everything else fails fast;
* list calls paginate (``gh api --paginate --jq '.[] | tojson'``, one JSON
  value per line — never ``json.loads`` over concatenated pages) or warn when
  they hit their cap.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from squadhq import settings

DEFAULT_TIMEOUT = 60.0
DEFAULT_RETRIES = 2

# stderr fragments that mean "try again later", not "this request is wrong".
_TRANSIENT_RE = re.compile(
    r"HTTP 5\d\d|HTTP 429|HTTP 408|secondary rate limit|rate limit exceeded|"
    r"timed? ?out|timeout|connection reset|connection refused|EOF|"
    r"temporarily unavailable|bad gateway|service unavailable|gateway time-?out|"
    r"tls handshake|i/o timeout|could not resolve host",
    re.IGNORECASE,
)

_RAISE = object()

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class GitHubError(RuntimeError):
    """A ``gh`` invocation failed (after retries, where retrying applied)."""

    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        self.cmd = list(args)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"gh {' '.join(self.cmd)} failed ({returncode}): {stderr.strip()[:500]}")


def is_transient(stderr: str) -> bool:
    return bool(_TRANSIENT_RE.search(stderr or ""))


def _warn(msg: str, warnings: list[str] | None) -> None:
    if warnings is not None:
        warnings.append(msg)
    print(f"warning: {msg}", file=sys.stderr)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a") as f:
                f.write(f"\n> ⚠ {msg}\n")
        except OSError:
            pass


class GitHubClient:
    def __init__(
        self,
        gh: str = "gh",
        *,
        timeout: float | None = None,
        retries: int = DEFAULT_RETRIES,
        backoff: float = 1.0,
        runner: Runner | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.gh = gh
        if timeout is None:
            try:
                timeout = float(os.environ.get("SQUADHQ_GH_TIMEOUT") or DEFAULT_TIMEOUT)
            except ValueError:
                timeout = DEFAULT_TIMEOUT
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = backoff
        self._runner: Runner = runner or subprocess.run
        self._sleep = sleep

    # ------------------------------------------------------------------ core

    def run(
        self,
        args: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = False,
        timeout: float | None = None,
        retry: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``gh <args>``. Never raises unless ``check=True``; a timeout
        comes back as returncode 124 with ``timeout`` in stderr."""
        cmd = [self.gh, *[str(a) for a in args]]
        attempts = (self.retries + 1) if retry else 1
        result: subprocess.CompletedProcess[str] | None = None
        for attempt in range(attempts):
            try:
                result = self._runner(cmd, capture_output=True, text=True, input=input_text,
                                      timeout=timeout or self.timeout)
            except subprocess.TimeoutExpired:
                result = subprocess.CompletedProcess(cmd, 124, "", f"timeout after {timeout or self.timeout}s")
            except FileNotFoundError as e:
                result = subprocess.CompletedProcess(cmd, 127, "", f"{self.gh} not found: {e}")
                break
            if result.returncode == 0 or not is_transient(result.stderr):
                break
            if attempt + 1 < attempts:
                self._sleep(self.backoff * (2 ** attempt))
        assert result is not None
        if check and result.returncode != 0:
            raise GitHubError(args, result.returncode, result.stderr)
        return result

    def json(self, args: Sequence[str], default: Any = _RAISE, **kw: Any) -> Any:
        """``gh <args>`` parsed as ONE JSON document. On failure returns
        ``default`` when given, else raises :class:`GitHubError`."""
        r = self.run(args, **kw)
        if r.returncode != 0:
            if default is _RAISE:
                raise GitHubError(args, r.returncode, r.stderr)
            return default
        try:
            return json.loads(r.stdout) if r.stdout.strip() else None
        except ValueError as e:
            if default is _RAISE:
                raise GitHubError(args, 0, f"unparseable JSON: {e}") from None
            return default

    def lines(self, args: Sequence[str], default: Any = _RAISE) -> Any:
        r = self.run(args)
        if r.returncode != 0:
            if default is _RAISE:
                raise GitHubError(args, r.returncode, r.stderr)
            return default
        return [ln for ln in r.stdout.splitlines() if ln.strip()]

    # ------------------------------------------------------------ list calls

    def paginate(self, path: str, *, key: str | None = None, default: Any = _RAISE,
                 extra: Sequence[str] = ()) -> Any:
        """Every element of a paginated REST list. ``key`` selects a nested
        list (``jobs`` for ``/actions/runs/{id}/jobs``). One JSON value per
        output line, so pages never concatenate into ``][``."""
        jq = f".{key}[] | tojson" if key else ".[] | tojson"
        args = ["api", "--paginate", *extra, path, "--jq", jq]
        r = self.run(args)
        if r.returncode != 0:
            if default is _RAISE:
                raise GitHubError(args, r.returncode, r.stderr)
            return default
        out = []
        for line in r.stdout.splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def list_capped(self, args: Sequence[str], limit: int, *, what: str = "",
                    warnings: list[str] | None = None, default: Any = _RAISE) -> Any:
        """``gh issue|pr list ... --limit N --json ...`` that WARNS when the
        result fills the cap (the rest is silently missing otherwise)."""
        items = self.json([*args, "--limit", str(limit)], default=default)
        if isinstance(items, list) and len(items) >= limit:
            _warn(f"{what or ' '.join(args[:3])}: hit the {limit}-item cap — older items not seen", warnings)
        return items

    # ------------------------------------------------------------------ api

    def api(
        self,
        path: str,
        *,
        method: str = "GET",
        fields: Mapping[str, Any] | None = None,
        raw_fields: Mapping[str, Any] | None = None,
        body: Any = None,
        jq: str | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """REST call. ``fields`` are typed (``-F``), ``raw_fields`` strings
        (``-f``); ``body`` is sent as a JSON document on stdin."""
        args: list[str] = ["api", "-X", method.upper(), path]
        for k, v in (raw_fields or {}).items():
            args += ["-f", f"{k}={v}"]
        for k, v in (fields or {}).items():
            args += ["-F", f"{k}={v}"]
        if jq:
            args += ["--jq", jq]
        text = None
        if body is not None:
            args += ["--input", "-"]
            text = json.dumps(body)
        # Non-idempotent writes are not retried: a POST that timed out may have
        # landed, and re-sending it creates a duplicate.
        retry = method.upper() in ("GET", "HEAD", "PUT", "DELETE", "PATCH")
        return self.run(args, input_text=text, check=check, retry=retry)

    def api_json(self, path: str, default: Any = _RAISE, **kw: Any) -> Any:
        r = self.api(path, **kw)
        if r.returncode != 0:
            if default is _RAISE:
                raise GitHubError([path], r.returncode, r.stderr)
            return default
        try:
            return json.loads(r.stdout) if r.stdout.strip() else None
        except ValueError:
            if default is _RAISE:
                raise GitHubError([path], 0, "unparseable JSON") from None
            return default

    def graphql(self, query: str, variables: Mapping[str, Any] | None = None,
                raw_variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """GraphQL call. String variables go as ``-f``, typed (Int/Boolean)
        as ``-F``. Raises on transport errors AND on a populated ``errors``
        array (GitHub returns HTTP 200 with null data for permission gaps)."""
        args = ["api", "graphql", "-f", f"query={query}"]
        for k, v in (variables or {}).items():
            args += ["-f", f"{k}={v}"]
        for k, v in (raw_variables or {}).items():
            args += ["-F", f"{k}={v}"]
        r = self.run(args)
        if r.returncode != 0:
            raise GitHubError(["api", "graphql"], r.returncode, r.stderr)
        data: dict[str, Any] = json.loads(r.stdout or "{}")
        if data.get("errors"):
            raise GitHubError(["api", "graphql"], 0, "GraphQL errors: " + json.dumps(data["errors"])[:500])
        return data

    # ------------------------------------------------------------- issues

    def find_issue_by_title(self, repo: str, title: str, *, state: str = "open",
                            label: str | None = None, bot_only: bool = True) -> int | None:
        """Exact-title match (search is fuzzy; the title check is not).

        ``bot_only`` (default): only an issue OUR bot opened counts — anyone
        can open an issue with an escalation's title to swallow the real one."""
        from squadhq import untrusted

        args = ["issue", "list", "-R", repo, "--state", state, "--search", f'"{title}" in:title',
                "--json", "number,title,author", "--limit", "50"]
        if label:
            args += ["--label", label]
        items = self.json(args, default=None) or []
        for it in items:
            if it.get("title") == title and (not bot_only or untrusted.by_bot(it)):
                return int(it["number"])
        return None

    def create_issue(self, repo: str, title: str, body: str, labels: Iterable[str] = ()) -> int | None:
        args = ["issue", "create", "-R", repo, "-t", title, "-b", body]
        lbls = normalize_labels(labels)
        if lbls:
            args += ["-l", ",".join(lbls)]
        r = self.run(args, retry=False)
        if r.returncode != 0 and lbls and "not found" in (r.stderr or "").lower():
            # A missing label must never block the issue itself.
            r = self.run(["issue", "create", "-R", repo, "-t", title, "-b", body], retry=False)
        if r.returncode != 0:
            return None
        m = re.search(r"/issues/(\d+)", r.stdout + r.stderr)
        return int(m.group(1)) if m else None

    def comment(self, repo: str, number: int | str, body: str) -> bool:
        return self.run(["issue", "comment", str(number), "-R", repo, "-b", body], retry=False).returncode == 0

    def find_or_create_issue_by_title(
        self,
        repo: str,
        title: str,
        body: str,
        *,
        labels: Iterable[str] = (),
        comment_body: str | None = None,
        state: str = "open",
        bot_only: bool = True,
    ) -> tuple[int | None, bool]:
        """-> (issue number, created). An existing issue (in ``state``,
        opened by our bot unless ``bot_only=False``) gets ``comment_body`` as a
        comment when given (``None`` = leave it be)."""
        number = self.find_issue_by_title(repo, title, state=state, bot_only=bot_only)
        if number is not None:
            if comment_body:
                self.comment(repo, number, comment_body)
            return number, False
        return self.create_issue(repo, title, body, labels), True

    # ------------------------------------------------------------- contents

    def contents_get(self, repo: str, path: str, ref: str | None = None) -> tuple[str | None, str | None]:
        """-> (decoded text, blob sha), or (None, None) when absent/unreadable."""
        p = f"repos/{repo}/contents/{path}" + (f"?ref={ref}" if ref else "")
        meta = self.json(["api", p], default=None)
        if not isinstance(meta, dict) or "content" not in meta:
            return None, None
        try:
            return base64.b64decode(meta["content"]).decode(), meta.get("sha")
        except (ValueError, TypeError):
            return None, None

    def contents_put(self, repo: str, path: str, text: str, message: str, *, sha: str | None = None,
                     branch: str | None = None) -> bool:
        """Create (``sha=None``) or compare-and-set update of one file. GitHub
        rejects the PUT with 409/422 when ``sha`` is stale, which is what
        makes read-modify-write safe under concurrency."""
        name, email = bot_identity(self)
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(text.encode()).decode(),
            "committer": {"name": name, "email": email},
            "author": {"name": name, "email": email},
        }
        if sha:
            payload["sha"] = sha
        if branch:
            payload["branch"] = branch
        return self.api(f"repos/{repo}/contents/{path}", method="PUT", body=payload).returncode == 0

    def update_json_file(self, repo: str, path: str, mutate: Callable[[Any], Any], message: str, *,
                         default: Any = None, attempts: int = 4) -> bool:
        """Compare-and-set loop over a JSON file in a repo: read (text, sha),
        apply ``mutate`` to the parsed value, PUT with that sha; on a lost race
        re-read and re-apply. ``mutate`` returning ``None`` means "no write
        needed" (counts as success)."""
        for attempt in range(attempts):
            text, sha = self.contents_get(repo, path)
            try:
                current = json.loads(text) if text else default
            except ValueError:
                current = default
            new = mutate(current)
            if new is None:
                return True
            if self.contents_put(repo, path, json.dumps(new, indent=2) + "\n", message, sha=sha):
                return True
            if attempt + 1 < attempts:
                self._sleep(self.backoff * (attempt + 1))
        return False


def normalize_labels(labels: Any) -> list[str]:
    """Labels as a clean list. A bare string is ONE label (or a comma list),
    never iterated character by character (``"bug"`` -> ``["bug"]``)."""
    if labels is None:
        return []
    if isinstance(labels, str):
        items: Iterable[Any] = labels.split(",")
    elif isinstance(labels, (list, tuple, set, frozenset)):
        items = labels
    else:
        return []
    out: list[str] = []
    for x in items:
        if isinstance(x, dict):
            x = x.get("name")
        if not isinstance(x, str):
            continue
        for part in x.split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


_IDENTITY: tuple[str, str] | None = None
_FALLBACK_IDENTITY = ("github-actions[bot]", "41898282+github-actions[bot]@users.noreply.github.com")


def bot_identity(gh: GitHubClient | None = None) -> tuple[str, str]:
    """``(name, email)`` for commits made by the bot. Uses
    ``SQUADHQ_BOT_USER_ID`` when set; otherwise looks the id up once per
    process. Falls back to the github-actions identity, never to a guessed
    login (a placeholder name gets attributed to whoever owns it)."""
    global _IDENTITY
    if _IDENTITY is not None:
        return _IDENTITY
    login = settings.bot_login()
    if os.environ.get("SQUADHQ_BOT_USER_ID"):
        _IDENTITY = settings.bot_git_identity()
        return _IDENTITY
    r = (gh or client()).run(["api", f"/users/{login}", "--jq", ".id"])
    uid = r.stdout.strip() if r.returncode == 0 else ""
    _IDENTITY = (login, f"{uid}+{login}@users.noreply.github.com") if uid.isdigit() else _FALLBACK_IDENTITY
    return _IDENTITY


def reset_identity_cache() -> None:
    global _IDENTITY
    _IDENTITY = None


_DEFAULT: GitHubClient | None = None


def client() -> GitHubClient:
    """The process-wide client. Tests swap it with :func:`set_client`."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = GitHubClient()
    return _DEFAULT


def set_client(c: GitHubClient | None) -> None:
    global _DEFAULT
    _DEFAULT = c
