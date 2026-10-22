"""squadhq.policy.implementer — exactly one implementer per dispatch."""
import io

import pytest

from squadhq import stuck_sweeper as sw
from squadhq.policy import implementer as im

ALL = {r.skill for r in im.RULES}


@pytest.mark.parametrize("labels,body,expected", [
    (["agent:implement"], "", "engineer"),
    (["agent:implement", "dept:frontend"], "", "frontend-engineer"),
    (["dept:ml"], None, "ml-engineer"),
    (["dept:integrations"], "", "integration-engineer"),
    (["dept:data"], "", "data-engineer"),
    ([], "Update `dbt/models/orders.sql` please", "database-migration-engineer"),  # .sql is a migration path
    ([], "Add a DAG in airflow/dags/sync.py", "data-engineer"),
    ([], "Change infra/main.tf and charts/api/values.yaml", "iac-devops-engineer"),
    ([], "Add db/migrations/V3__add_col.sql", "database-migration-engineer"),
    # several hints -> fixed priority, never two
    (["dept:frontend", "dept:data"], "", "data-engineer"),
    (["dept:frontend"], "touches db/migrations/V1.sql", "database-migration-engineer"),
    (["dept:frontend", "dept:infra"], "", "iac-devops-engineer"),
])
def test_choose_priority(labels, body, expected):
    assert im.choose(labels, body, ALL) == expected


def test_disabled_specialist_falls_through():
    assert im.choose(["dept:frontend", "dept:data"], "", {"frontend-engineer"}) == "frontend-engineer"
    assert im.choose(["dept:frontend"], "", set()) == "engineer"


def test_paths_in():
    assert "db/migrations/V1.sql" in im.paths_in("see (db/migrations/V1.sql).")
    assert "main.tf" in im.paths_in("edit main.tf now")
    assert im.paths_in(None) == []


def test_enabled_specialists_reads_specialist_routes():
    cat = {
        "frontend-engineer": {"enabled": True, "trigger": [
            {"label": "agent:implement", "filter": "specialist route: dept:frontend"}]},
        "ml-engineer": {"enabled": False, "trigger": [
            {"label": "agent:implement", "filter": "specialist route: dept:ml"}]},
        "engineer": {"enabled": True, "trigger": [{"label": "agent:implement"}, "bogus"]},
        "weird": "not a dict",
        "nothing": {"enabled": True},
    }
    assert im.enabled_specialists(lambda: cat) == {"frontend-engineer"}


def test_enabled_specialists_fails_safe():
    def boom():
        raise OSError("no catalogue")
    assert im.enabled_specialists(boom) == set()


def test_enabled_specialists_real_catalogue_is_a_set():
    assert im.enabled_specialists() <= ALL


def test_cli(capsys, tmp_path, monkeypatch):
    assert im.main(["--labels", "agent:implement,dept:frontend", "--enabled", "frontend-engineer"]) == 0
    assert capsys.readouterr().out.strip() == "frontend-engineer"
    f = tmp_path / "body.md"
    f.write_text("db/migrations/V2.sql")
    assert im.main(["--body-file", str(f), "--enabled", "database-migration-engineer, "]) == 0
    assert capsys.readouterr().out.strip() == "database-migration-engineer"
    monkeypatch.setattr("sys.stdin", io.StringIO("nothing special"))
    monkeypatch.setattr(im, "enabled_specialists", lambda: ALL)
    assert im.main(["--body-file", "-"]) == 0
    assert capsys.readouterr().out.strip() == "engineer"


def test_sweeper_records_one_implementer():
    issue = {"labels": [{"name": "triaged:ready"}, {"name": "dept:frontend"}], "body": ""}
    assert sw.implementer_for(issue, {"frontend-engineer"}) == "frontend-engineer"
    assert sw.implementer_for(issue, set()) == "engineer"
