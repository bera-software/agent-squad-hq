import json
import os

from squadhq.config import registry as R
from squadhq.config import schema

from .hubfix import REPO_ROOT


def test_committed_schema_is_current():
    with open(os.path.join(REPO_ROOT, "schema", "squadhq.schema.json")) as f:
        assert f.read() == schema.dumps(), "run: python -m squadhq.config.schema > schema/squadhq.schema.json"


def test_schema_covers_registry():
    s = schema.build()
    props = s["properties"]
    assert set(props) == {"version", "org", "hub", "providers", "budgets", "trackers", "notifications",
                          "schedules", "agents", "skills", "edition", "policies", "security", "projects"}
    assert set(props["schedules"]["properties"]) == set(R.SCHEDULES)
    assert set(props["providers"]["properties"]["jobs"]["properties"]) == set(R.DEFAULT_JOB_TIERS)
    assert props["policies"]["properties"]["merge"]["properties"]["mode"]["enum"] == list(R.MERGE_MODES)
    json.dumps(s)
