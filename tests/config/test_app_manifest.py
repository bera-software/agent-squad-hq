import json
import os
import stat
import threading
import urllib.request

import pytest

from squadhq.config import app_manifest as AM

from .hubfix import REPO_ROOT

TEMPLATE = os.path.join(REPO_ROOT, "deploy", "github-app", "app-manifest.json")


def test_template_least_privilege_and_documented():
    with open(TEMPLATE) as f:
        raw = json.load(f)
    perms = raw["default_permissions"]
    assert perms["contents"] == "write" and perms["metadata"] == "read" and perms["checks"] == "read"
    assert "administration" not in perms and "secrets" not in perms
    why = raw["_permissions_rationale"]
    for p, level in perms.items():
        assert f"{p}: {level}" in why, f"undocumented permission {p}"
    assert raw["hook_attributes"]["active"] is False and raw["public"] is False


def test_build_manifest_strips_docs_and_substitutes():
    m = AM.build_manifest("acme-squad", "acme/hq", "http://127.0.0.1:9/callback", template_path=TEMPLATE)
    assert not any(k.startswith("_") for k in m)
    assert m["name"] == "acme-squad" and m["url"] == "https://github.com/acme/hq"
    assert m["redirect_url"] == "http://127.0.0.1:9/callback"
    assert m["hook_attributes"] == {"url": "https://github.com/acme/hq", "active": False}
    m2 = AM.build_manifest("a", "acme/hq", "r", webhook_url="https://hooks.example/gh", template_path=TEMPLATE)
    assert m2["hook_attributes"] == {"url": "https://hooks.example/gh", "active": True}
    assert "__" not in json.dumps(m2)


def test_default_template_path_uses_hub_root(monkeypatch):
    monkeypatch.setenv("SQUADHQ_HOME", REPO_ROOT)
    assert AM.manifest_template_path() == TEMPLATE


def test_urls_and_form():
    assert AM.new_app_url("acme", True, "s1") == "https://github.com/organizations/acme/settings/apps/new?state=s1"
    assert AM.new_app_url("me", False, "s") == "https://github.com/settings/apps/new?state=s"
    page = AM.form_page("https://x/?state=a&b", {"name": 'a"b'})
    assert "&amp;b" in page and "&quot;" in page and "submit()" in page


def _conv_body():
    return json.dumps({"id": 7, "slug": "acme-squad", "client_id": "Iv1.x", "pem": "-----BEGIN-----\n",
                       "html_url": "https://github.com/apps/acme-squad", "webhook_secret": None}).encode()


def test_exchange_code():
    seen = {}

    def opener(req, timeout):
        seen["url"], seen["method"], seen["timeout"] = req.full_url, req.get_method(), timeout
        return _conv_body()

    c = AM.exchange_code("abc", opener)
    assert seen == {"url": "https://api.github.com/app-manifests/abc/conversions", "method": "POST", "timeout": 30.0}
    assert c.slug == "acme-squad" and c.id == 7 and c.webhook_secret == ""


def test_save_pem_mode(tmp_path):
    p = AM.save_pem(str(tmp_path), "acme-squad", "PEM")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert open(p).read() == "PEM"


def _browser_that_follows(state_holder, *, bad_state=False, extra=None):
    def open_browser(url):
        def go():
            page = urllib.request.urlopen(url, timeout=5).read().decode()
            state_holder["page"] = page
            nonce = page.split("state=")[1].split('"')[0]
            base = url.rstrip("/")
            if extra:
                try:
                    urllib.request.urlopen(base + extra, timeout=5)
                except Exception as e:
                    state_holder["extra"] = str(e)
            st = "wrong" if bad_state else nonce
            try:
                urllib.request.urlopen(f"{base}/callback?code=CODE1&state={st}", timeout=5).read()
            except Exception as e:
                state_holder["err"] = str(e)
        threading.Thread(target=go, daemon=True).start()
        return True
    return open_browser


def test_run_flow_end_to_end_on_localhost():
    holder = {}
    calls = []

    def opener(req, timeout):
        calls.append(req.full_url)
        return _conv_body()

    c = AM.run_flow("acme", True, "acme-squad", "acme/hq", open_browser=_browser_that_follows(holder, extra="/nope"),
                    timeout=10, template_path=TEMPLATE, opener=opener)
    assert c.slug == "acme-squad"
    assert calls == ["https://api.github.com/app-manifests/CODE1/conversions"]
    assert "organizations/acme/settings/apps/new" in holder["page"]
    assert "404" in holder["extra"]


def test_run_flow_state_mismatch():
    holder = {}
    with pytest.raises(RuntimeError, match="state mismatch"):
        AM.run_flow("acme", True, "a", "acme/hq", open_browser=_browser_that_follows(holder, bad_state=True),
                    timeout=10, template_path=TEMPLATE, opener=lambda r, t: _conv_body())


def test_run_flow_timeout():
    with pytest.raises(TimeoutError):
        AM.run_flow("acme", True, "a", "acme/hq", open_browser=lambda u: True, timeout=0.2,
                    template_path=TEMPLATE)
