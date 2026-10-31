"""squadhq-token broker client (ADR-0011): exchange this job's GitHub Actions
OIDC token for a GitHub App installation token at SQUADHQ_TOKEN_BROKER.

    python3 -I broker.py token   # TOKEN_IDENTITY, TOKEN_REPOSITORIES, TOKEN_PERMISSIONS,
                                 # TOKEN_PURPOSE, TOKEN_SKIP_REVOKE
    python3 -I broker.py usage   # USAGE_JOB, USAGE_OUTCOME, USAGE_RESULT (best effort)

Standard library only: it runs on the runner's system python, before
setup-python. The OIDC JWT and the minted token are masked the moment they are
read and never printed; broker errors are reported by code and detail only.
TOKEN_PERMISSIONS ("contents=read,pull-requests=write", empty levels skipped)
narrows the token exactly like create-github-app-token's permission-* inputs.
Unless TOKEN_SKIP_REVOKE is "true", the token is listed in
$RUNNER_TEMP/squadhq-broker-tokens for the job's final revoke step
(DELETE /installation/token, as create-github-app-token does at job end).
router.yml and squadhq-agent.yml carry a verbatim copy (they mint before the
hub is checked out); tests/test_token_broker.py keeps the copies identical.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

URL_RE = re.compile(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%/-]*)?")
NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]{20,2048}")  # also GitHub's stateless ghs_APPID_JWT tokens (~520)
LOGIN_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,33}\[bot\]")
RETRY = (429, 500, 502, 503, 504)
PERM_RE = re.compile(r"[a-z][a-z_-]{0,49}")
MAX_AGENT_REPOS = 25
LEVEL = "error"  # "warning" in usage mode: reporting never fails a run


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect would carry the bearer JWT to another URL: refuse it.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


def esc(text):
    return str(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")[:500]


def mask(secret):
    for line in str(secret).splitlines() or [""]:
        if line.strip():
            print(f"::add-mask::{line.strip()}", flush=True)


def fail(msg):
    print(f"::{LEVEL} title=squadhq-token::{esc(msg)}", flush=True)
    sys.exit(1 if LEVEL == "error" else 0)


def _json(raw):
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def call(req, tries=3):
    """(status, JSON object) — status 0 when the host was unreachable."""
    for attempt in range(tries):
        try:
            with OPENER.open(req, timeout=30) as resp:
                return resp.status, _json(resp.read())
        except urllib.error.HTTPError as e:
            status, data = e.code, _json(e.read())
            if status not in RETRY or attempt == tries - 1:
                return status, data
        except (urllib.error.URLError, OSError) as e:
            if attempt == tries - 1:
                return 0, {"error": "unreachable", "detail": esc(getattr(e, "reason", e))}
        time.sleep(2 * (attempt + 1) ** 2)
    return 0, {}


def broker_url():
    url = os.environ.get("SQUADHQ_TOKEN_BROKER", "").strip().rstrip("/")
    if not URL_RE.fullmatch(url):
        fail("SQUADHQ_TOKEN_BROKER must be an https URL (https://host[:port][/path])")
    return url


def oidc(broker):
    req_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    req_tok = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not req_url or not req_tok:
        fail("no GitHub OIDC token in this job: token-broker mode needs `permissions: id-token: write` "
             "on the job (and on the caller job of a reusable workflow)")
    mask(req_tok)
    audience = os.environ.get("SQUADHQ_TOKEN_BROKER_AUDIENCE", "").strip() or broker
    sep = "&" if "?" in req_url else "?"
    req = urllib.request.Request(  # noqa: S310 - the runner's https OIDC endpoint
        f"{req_url}{sep}audience={urllib.parse.quote(audience, safe='')}",
        headers={"Authorization": f"bearer {req_tok}", "Accept": "application/json"})
    status, data = call(req)
    jwt = data.get("value")
    if isinstance(jwt, str) and jwt:
        mask(jwt)
    if status != 200 or not isinstance(jwt, str) or not jwt:
        fail(f"GitHub did not issue an OIDC token (HTTP {status})")
    return jwt


def post(url, jwt, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={  # noqa: S310 - https checked
        "Authorization": f"Bearer {jwt}", "Content-Type": "application/json",
        "Accept": "application/json", "User-Agent": "squadhq-token"})
    return call(req)


def refusal(status, data):
    code = data.get("error") if isinstance(data.get("error"), str) else "unknown"
    detail = data.get("detail") if isinstance(data.get("detail"), str) else ""
    return f"HTTP {status} {code}" + (f": {detail}" if detail else "")


def repositories(raw, owner):
    names = []
    for entry in re.split(r"[,\s]+", raw):
        if not entry:
            continue
        own, _, name = entry.rpartition("/")
        if own and own.lower() != owner.lower():
            fail(f"repository {entry} is outside {owner}: broker tokens cover the installation owner only")
        if not NAME_RE.fullmatch(name):
            fail(f"invalid repository name {entry!r}")
        if name not in names:
            names.append(name)
    return names


def permissions(raw):
    perms = {}
    for entry in re.split(r"[,\s]+", raw):
        name, _, level = entry.partition("=")
        if not name or not level:
            continue
        if not PERM_RE.fullmatch(name) or level not in ("read", "write", "admin"):
            fail(f"invalid permission {entry!r} (name=read|write|admin)")
        perms[name.replace("-", "_")] = level
    return perms


def remember(tok):
    if os.environ.get("TOKEN_SKIP_REVOKE", "").strip().lower() == "true":
        return
    temp = os.environ.get("RUNNER_TEMP", "")
    if not temp:
        print("::warning title=squadhq-token::RUNNER_TEMP unset: the token is not revoked at job end")
        return
    path = os.path.join(temp, "squadhq-broker-tokens")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(tok + "\n")


def output(**values):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
        for k, v in values.items():
            f.write(f"{k}={v}\n")


def token():
    broker = broker_url()
    identity = os.environ.get("TOKEN_IDENTITY", "ops").strip() or "ops"
    if identity not in ("ops", "agent"):
        fail(f"identity must be ops or agent, not {identity!r}")
    repos = repositories(os.environ.get("TOKEN_REPOSITORIES", ""), os.environ.get("GITHUB_REPOSITORY_OWNER", ""))
    if identity == "agent" and len(repos) > MAX_AGENT_REPOS:
        fail(f"an agent token covers at most {MAX_AGENT_REPOS} repositories, {len(repos)} requested")
    perms = permissions(os.environ.get("TOKEN_PERMISSIONS", ""))
    purpose = os.environ.get("TOKEN_PURPOSE", "").strip() or os.environ.get("GITHUB_JOB", "")
    body = {"identity": identity, "purpose": re.sub(r"[^A-Za-z0-9_.:-]", "-", purpose)[:100]}
    if repos:
        body["repositories"] = repos
    if perms:
        body["permissions"] = perms
    status, data = post(f"{broker}/v1/broker/token", oidc(broker), body)
    tok = data.get("token")
    if isinstance(tok, str) and tok:
        mask(tok)
    if status != 200:
        # For a notice on the issue or PR (agent-prep): the code, and where to fix it.
        code = data.get("error") if isinstance(data.get("error"), str) else ""
        help_url = data.get("help_url") if isinstance(data.get("help_url"), str) else ""
        if "GITHUB_OUTPUT" in os.environ:
            output(refused=re.sub(r"[^a-z_]", "", code)[:40] or "unknown",
                   **{"help-url": help_url if URL_RE.fullmatch(help_url) else ""})
        fail(f"token broker refused the {identity} token ({refusal(status, data)})")
    if not isinstance(tok, str) or not TOKEN_RE.fullmatch(tok):
        fail("token broker answered 200 without a usable token")
    remember(tok)
    login = data.get("login") if isinstance(data.get("login"), str) else ""
    login = login if LOGIN_RE.fullmatch(login) else ""
    expires = data.get("expires_at") if isinstance(data.get("expires_at"), str) else ""
    output(token=tok, login=login, **{"app-slug": login[: -len("[bot]")] if login else "",
                                      "expires-at": re.sub(r"[^0-9A-Za-z:.+-]", "", expires)[:40]})
    print(f"squadhq-token: {identity} token from the token broker"
          + (f" for {', '.join(repos)}" if repos else "") + (f" as {login}" if login else ""))


OUTCOMES = {"success": "success", "cancelled": "cancelled", "skipped": "cancelled"}


def usage():
    global LEVEL
    LEVEL = "warning"
    broker = broker_url()
    body = {"run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "job": re.sub(r"[^A-Za-z0-9_.:-]", "-", os.environ.get("USAGE_JOB", "") or os.environ.get("GITHUB_JOB", ""))[:100],
            "outcome": OUTCOMES.get(os.environ.get("USAGE_OUTCOME", "").strip().lower(), "failure")}
    try:
        with open(os.environ.get("USAGE_RESULT", "") or os.devnull, encoding="utf-8") as f:
            result = _json(f.read().encode())
    except OSError:
        result = {}
    for key, kind in (("cost_usd", float), ("input_tokens", int), ("output_tokens", int)):
        v = result.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            body[key] = kind(v)
    pr = result.get("pr_url") or (result.get("extra") or {}).get("pr_url")
    if isinstance(pr, str) and re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+", pr):
        body["pr_url"] = pr
    status, data = post(f"{broker}/v1/broker/usage", oidc(broker), body)
    if 200 <= status < 300:
        print(f"squadhq-token: usage reported ({body['job']}: {body['outcome']})")
    else:
        fail(f"usage report not accepted ({refusal(status, data)})")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "token"
    if mode == "usage":
        usage()
    elif mode == "token":
        token()
    else:
        fail(f"unknown mode {mode!r} (token | usage)")
