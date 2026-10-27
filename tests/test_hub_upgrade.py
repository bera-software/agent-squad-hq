"""squadhq.hub_upgrade: the managed files of a hub and the change set of an upgrade."""
import json
import os

import pytest

from squadhq import __version__, hub_upgrade
from squadhq.hub_upgrade import MANIFEST_PATH, Change, HubFiles, git_blob_sha
from squadhq.paths import _SOURCE_ROOT as ROOT


@pytest.fixture(scope="module")
def config_text():
    with open(os.path.join(ROOT, "squadhq.example.yml"), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def hub(config_text):
    return hub_upgrade.desired(config_text)


def blobs(files):
    return {p: git_blob_sha(b) for p, b in files.items()}


def test_git_blob_sha_matches_git():
    assert git_blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"  # git hash-object


def test_desired_holds_engine_and_rendered_files_only(hub):
    assert hub.version == __version__
    assert "src/squadhq/__init__.py" in hub.files and ".github/workflows/router.yml" in hub.files
    assert {"config/models.json", "config/projects.json", "config/auth.json"} <= hub.rendered
    for path in ("squadhq.yml", "README.md", ".gitignore", ".github/workflows/hub-ci.yml",
                 ".github/workflows/release.yml"):
        assert path not in hub.files
    assert not any(p.endswith((".pyc", ".DS_Store")) or "/_hub/" in p for p in hub.files)


def test_invalid_config_is_an_upgrade_error():
    with pytest.raises(hub_upgrade.UpgradeError):
        hub_upgrade.desired("version: 1\norg: [not, a, mapping]\n")


def test_up_to_date_hub_needs_no_change(hub):
    current = blobs(hub.files)
    current[MANIFEST_PATH] = git_blob_sha(hub_upgrade.manifest_bytes(hub))
    ch = hub_upgrade.plan(current, hub, hub_upgrade.parse_manifest(hub_upgrade.manifest_bytes(hub)))
    assert ch.empty and ch.writes == {} and not ch.first_managed


def test_first_managed_upgrade_replaces_engine_files_and_writes_the_manifest():
    new = HubFiles("9.9.9", {"prompts/a.md": b"new", "config/models.json": b"{}"}, frozenset({"config/models.json"}))
    current = {"prompts/a.md": git_blob_sha(b"edited locally"), "squadhq.yml": "x", "docs/mine.md": "y"}
    ch = hub_upgrade.plan(current, new, None)
    assert ch.first_managed and set(ch.writes) == {"prompts/a.md", "config/models.json", MANIFEST_PATH}
    assert ch.deletes == [] and ch.kept == []
    manifest = json.loads(ch.writes[MANIFEST_PATH])
    assert manifest == {"version": "9.9.9", "files": {"config/models.json": git_blob_sha(b"{}"),
                                                       "prompts/a.md": git_blob_sha(b"new")}}


def test_local_changes_are_kept_except_in_rendered_files():
    old = {"prompts/a.md": git_blob_sha(b"v1"), "config/models.json": git_blob_sha(b"{1}")}
    current = {"prompts/a.md": git_blob_sha(b"mine"), "config/models.json": git_blob_sha(b"{mine}")}
    new = HubFiles("2", {"prompts/a.md": b"v2", "config/models.json": b"{2}"}, frozenset({"config/models.json"}))
    ch = hub_upgrade.plan(current, new, old)
    assert ch.kept == ["prompts/a.md"]
    assert set(ch.writes) == {"config/models.json", MANIFEST_PATH}


def test_files_removed_from_the_template_are_deleted_unless_changed_locally():
    old = {"prompts/gone.md": git_blob_sha(b"v1"), "prompts/mine.md": git_blob_sha(b"v1"),
           "squadhq.yml": git_blob_sha(b"cfg")}
    current = {"prompts/gone.md": git_blob_sha(b"v1"), "prompts/mine.md": git_blob_sha(b"edited"),
               "squadhq.yml": git_blob_sha(b"cfg")}
    ch = hub_upgrade.plan(current, HubFiles("2", {}, frozenset()), old)
    assert ch.deletes == ["prompts/gone.md"] and ch.kept == ["prompts/mine.md"]


def test_manifest_only_change_counts_as_empty():
    assert Change(writes={MANIFEST_PATH: b"{}"}).empty
    assert hub_upgrade.parse_manifest("not json") is None and hub_upgrade.parse_manifest(None) is None


def test_project_only_workflows_are_removed_from_template_hubs(hub):
    current = blobs(hub.files)
    current[".github/workflows/hub-ci.yml"] = git_blob_sha(b"project ci")
    current[".github/workflows/release.yml"] = git_blob_sha(b"project release")
    ch = hub_upgrade.plan(current, hub, None)
    assert ch.deletes == [".github/workflows/hub-ci.yml", ".github/workflows/release.yml"] and not ch.empty

