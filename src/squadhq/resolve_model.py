#!/usr/bin/env python3
"""
Resolve model: prints the model ID a claude-code-action job should run with,
via $GITHUB_OUTPUT `resolved_model` (+ `used_fallback` true|false). Single
source of truth is config/models.json — never hardcode a model ID in a
workflow file; a job's tier is decided by which tier's "jobs" list contains
its JOB env value.

One-shot override: if a MODEL_OVERRIDE_<JOB> repo variable is set (written by
squadhq.failure_handler right before it triggers a rerun, after a
model-specific TRANSIENT failure — see its MODEL_SPECIFIC_PATTERNS), this run
uses that model instead of the tier's primary, then immediately deletes the
variable — the override applies to exactly the retry it was set for, never to
every future run of this job.

Never raises past main(): a bug here must not block the job entirely — falls
back to the tier's primary, or a hardcoded last-resort model ID, and logs
loudly either way so the drift is visible in the run log.

Required env: JOB, GH_TOKEN. Optional: RUN_REPO (repo to read/clear the
MODEL_OVERRIDE_<JOB> variable from; falls back to REPO), CONFIG_PATH (default:
the hub checkout's config/models.json — .squadhq/config/models.json in
per-project jobs, via SQUADHQ_HOME).
"""
import json
import os
import re
import sys

from squadhq import github
from squadhq.paths import hub_path

# Only a last-resort fallback when config/models.json itself can't be read, so
# accuracy here matters less than always resolving to SOMETHING.
_LAST_RESORT = "claude-sonnet-5-5"


def sh(args):
    """Seam: ``args`` is a full ``gh ...`` argv (kept for test mocking)."""
    if args and args[0] == "gh":
        args = args[1:]
    return github.client().run(args)


def default_config_path():
    return hub_path("config", "models.json")


def var_name(job):
    return "MODEL_OVERRIDE_" + re.sub(r"[^A-Za-z0-9]", "_", job).upper()


def find_tier(config, job):
    for tier in config.get("tiers", {}).values():
        if job in tier.get("jobs", []):
            return tier
    return None


def gh_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    line = f"{key}={value}\n"
    if not path:
        print(line, end="")
        return
    with open(path, "a") as f:
        f.write(line)


def read_override(run_repo, name):
    if not run_repo:
        return None
    r = sh(["gh", "variable", "list", "-R", run_repo, "--json", "name,value"])
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        for v in json.loads(r.stdout):
            if v.get("name") == name:
                return v.get("value") or None
    except Exception:
        return None
    return None


def main():
    job = os.environ.get("JOB", "")
    run_repo = os.environ.get("RUN_REPO") or os.environ.get("REPO", "")
    config_path = os.environ.get("CONFIG_PATH") or default_config_path()

    try:
        with open(config_path) as f:
            config = json.load(f)
    except Exception as e:
        print(f"resolve_model: could not read {config_path}: {e!r} — using "
              f"last-resort {_LAST_RESORT}")
        gh_output("resolved_model", _LAST_RESORT)
        gh_output("used_fallback", "false")
        return

    tier = find_tier(config, job)
    if not tier:
        print(f"resolve_model: job {job!r} not found in any tier's jobs "
              f"list in {config_path} — using last-resort {_LAST_RESORT}")
        gh_output("resolved_model", _LAST_RESORT)
        gh_output("used_fallback", "false")
        return

    name = var_name(job)
    override = read_override(run_repo, name)
    if override:
        # Consumed once: clear it now so the NEXT run of this job — whether
        # this retry succeeds or not — goes back to the tier's primary by
        # default instead of staying stuck on the fallback.
        r = sh(["gh", "variable", "delete", name, "-R", run_repo])
        if r.returncode != 0:
            print(f"resolve_model: WARNING could not delete {name} on {run_repo}: "
                  f"{(r.stderr or '').strip()[:200]} — later runs may keep using {override!r}")

    resolved = override or tier["primary"]
    used_fallback = bool(override)
    print(f"resolve_model: job={job!r} tier_primary={tier['primary']!r} "
          f"override={override!r} -> resolved={resolved!r}")
    gh_output("resolved_model", resolved)
    gh_output("used_fallback", "true" if used_fallback else "false")


def run():
    """Entry point: never raises — a bug here must not block the job."""
    try:
        main()
    except Exception as e:
        print(f"resolve_model: swallowed error: {e!r} — using last-resort {_LAST_RESORT}")
        gh_output("resolved_model", _LAST_RESORT)
        gh_output("used_fallback", "false")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
