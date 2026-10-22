"""GitHub App creation through the manifest flow.

1. Serve a tiny page on ``127.0.0.1:<port>`` that auto-POSTs the manifest to
   ``https://github.com/organizations/<org>/settings/apps/new?state=<nonce>``.
2. GitHub redirects back to ``http://127.0.0.1:<port>/callback?code=..&state=..``.
3. Exchange the one-hour code: ``POST /app-manifests/{code}/conversions``
   (no auth needed; the code itself is the credential).
4. Save the PEM with mode 0600 and hand back slug / client id.
"""
from __future__ import annotations

import html
import json
import os
import secrets
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from squadhq.paths import hub_root

API = "https://api.github.com"
Opener = Callable[[urllib.request.Request, float], bytes]


def _default_opener(req: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
        data: bytes = resp.read()
        return data


MANIFESTS = {"ops": "app-manifest.json", "agent": "app-manifest-agent.json"}


def manifest_template_path(kind: str = "ops", root: str | None = None) -> str:
    """``deploy/github-app/app-manifest[-agent].json`` under ``root`` (default
    the hub root)."""
    return os.path.join(root or hub_root(), "deploy", "github-app", MANIFESTS[kind])


def build_manifest(app_name: str, hub_repo: str, redirect_url: str, webhook_url: str = "",
                   template_path: str | None = None) -> dict[str, Any]:
    with open(template_path or manifest_template_path(), encoding="utf-8") as f:
        raw = f.read()
    subs = {
        "__APP_NAME__": app_name,
        "__HUB_URL__": f"https://github.com/{hub_repo}",
        "__REDIRECT_URL__": redirect_url,
        "__WEBHOOK_URL__": webhook_url or f"https://github.com/{hub_repo}",
    }
    for k, v in subs.items():
        raw = raw.replace(k, v)
    data: dict[str, Any] = {k: v for k, v in json.loads(raw).items() if not k.startswith("_")}
    if webhook_url:
        data["hook_attributes"] = {"url": webhook_url, "active": True}
    return data


def new_app_url(owner: str, is_org: bool, state: str) -> str:
    base = (f"https://github.com/organizations/{urllib.parse.quote(owner)}/settings/apps/new" if is_org
            else "https://github.com/settings/apps/new")
    return f"{base}?state={urllib.parse.quote(state)}"


def form_page(action: str, manifest: dict[str, Any]) -> str:
    return (
        "<!doctype html><meta charset=utf-8><title>Create GitHub App</title>"
        f'<form id=f method=post action="{html.escape(action)}">'
        f'<input type=hidden name=manifest value="{html.escape(json.dumps(manifest))}">'
        "<button>Create the Agent Squad HQ GitHub App</button></form>"
        "<script>document.getElementById('f').submit()</script>"
    )


@dataclass
class Conversion:
    id: int
    slug: str
    client_id: str
    pem: str
    html_url: str
    webhook_secret: str = ""


def exchange_code(code: str, opener: Opener = _default_opener, timeout: float = 30.0) -> Conversion:
    req = urllib.request.Request(  # noqa: S310 - fixed https URL
        f"{API}/app-manifests/{urllib.parse.quote(code)}/conversions", method="POST", data=b"",
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    body = json.loads(opener(req, timeout).decode("utf-8"))
    return Conversion(id=int(body["id"]), slug=str(body["slug"]), client_id=str(body.get("client_id", "")),
                      pem=str(body["pem"]), html_url=str(body.get("html_url", "")),
                      webhook_secret=str(body.get("webhook_secret") or ""))


def save_pem(directory: str, slug: str, pem: str) -> str:
    path = os.path.join(directory, f"{slug}.private-key.pem")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(pem)
    os.chmod(path, 0o600)
    return path


class _State:
    def __init__(self, page: str, nonce: str) -> None:
        self.page = page
        self.nonce = nonce
        self.code: str | None = None
        self.error: str | None = None
        self.done = threading.Event()


def _handler(state: _State) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            return

        def _send(self, status: int, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            u = urllib.parse.urlparse(self.path)
            if u.path == "/":
                self._send(200, state.page)
                return
            if u.path == "/callback":
                q = urllib.parse.parse_qs(u.query)
                if q.get("state", [""])[0] != state.nonce:
                    state.error = "state mismatch (possible CSRF); start `squadhq init --create-app` again"
                    self._send(400, "<p>State mismatch. Return to the terminal.</p>")
                else:
                    state.code = q.get("code", [""])[0] or None
                    self._send(200, "<p>App created. You can close this tab and return to the terminal.</p>")
                state.done.set()
                return
            self._send(404, "not found")

    return H


def run_flow(owner: str, is_org: bool, app_name: str, hub_repo: str, *,
             open_browser: Callable[[str], bool], timeout: float = 600.0, port: int = 0,
             template_path: str | None = None, opener: Opener = _default_opener,
             server_factory: Callable[..., HTTPServer] = HTTPServer) -> Conversion:
    nonce = secrets.token_urlsafe(16)
    state = _State("", nonce)
    server = server_factory(("127.0.0.1", port), _handler(state))
    host, bound = server.server_address[0], server.server_address[1]
    redirect = f"http://{host!s}:{bound}/callback"
    manifest = build_manifest(app_name, hub_repo, redirect, template_path=template_path)
    state.page = form_page(new_app_url(owner, is_org, nonce), manifest)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        open_browser(f"http://{host!s}:{bound}/")
        if not state.done.wait(timeout):
            raise TimeoutError(f"no callback from GitHub within {int(timeout)}s")
    finally:
        server.shutdown()
        server.server_close()
    if state.error or not state.code:
        raise RuntimeError(state.error or "GitHub returned no code")
    return exchange_code(state.code, opener)
