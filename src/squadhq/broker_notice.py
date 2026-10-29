"""Tell an issue or PR why its agent did not start: the token broker refused the agent token.

    python -m squadhq.broker_notice   # REPO, ISSUE_OR_PR, ROLE, REFUSED, HELP_URL; GH_TOKEN (ops)

Run by agent-prep after a refused agent token. Posts one comment per refusal code, issue and
month (an identical notice from the bot is not repeated); codes without a notice are ignored.
Best effort: the job has already failed and says why in its log.
"""
from __future__ import annotations

import datetime
import os
import re

from squadhq import capacity_gate

NOTICES = {
    "quota_exceeded": "this month's agent runs on your Agent Squad HQ plan are used up. They reset on the 1st; "
                      "to continue now, upgrade the plan",
    "plan_limit": "this repository is beyond the number of projects your Agent Squad HQ plan covers. Turn "
                  "another project off or upgrade the plan",
    "plan_feature": "this job is not part of your Agent Squad HQ plan. Upgrade the plan to run it",
    "paused": "agents are paused for this repository in Agent Squad HQ. Resume them to continue",
    "repo_not_enabled": "this repository is not turned on in Agent Squad HQ. Turn it on under Repositories",
    "not_installed": "the Agent Squad HQ Agent App cannot reach this repository. Check the App's installation",
}
_URL = re.compile(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%/-]*)?")


def notice(code: str, role: str, help_url: str, today: datetime.date) -> str | None:
    text = NOTICES.get(code)
    if text is None:
        return None
    link = f": {help_url}" if _URL.fullmatch(help_url) else "."
    job = re.sub(r"[^A-Za-z0-9_.-]", "", role)[:40] or "agent"
    return (f"The **{job}** agent did not start: {text}{link}\n\n"
            f"<!-- squadhq:broker-refused {code} {today:%Y-%m} -->")


def main() -> int:
    env = os.environ.get
    repo, number = env("REPO", ""), env("ISSUE_OR_PR", "")
    body = notice(env("REFUSED", ""), env("ROLE", ""), env("HELP_URL", ""), datetime.date.today())
    if not body or not repo or not number.isdigit():
        return 0
    capacity_gate.dedupe_comment(repo, number, body)
    print(f"broker_notice: told {repo}#{number} ({env('REFUSED')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
