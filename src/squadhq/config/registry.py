"""Static knowledge the config layer validates against.

Everything here mirrors what the runtime consumes today (``config/models.json``
job names, workflow schedule markers, CONVENTIONS repository variables). When
a new job, agent or schedule is added to the runtime, add it here too; the
validator turns unknown names into warnings (not errors) so a newer runtime
never hard-fails an older CLI.
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# Jobs -> default tier. Covers every job name in config/models.json today.
# ``CODE_ACTION_JOBS`` are claude-code-action JOB env values (tiers.*.jobs,
# read by squadhq.resolve_model). ``HUB_LOOP_JOBS`` are agent_runner modes
# (hub_loops.*.jobs, read by squadhq.agent_runner).
# --------------------------------------------------------------------------
CODE_ACTION_JOBS: dict[str, str] = {
    "review": "heavy",
    "implement": "heavy",
    "design": "heavy",
    "review-fix": "heavy",
    "mention": "heavy",
    "po-shape": "heavy",
    "po-continue": "heavy",
    "concierge": "heavy",
    "ecosystem-watch": "heavy",
    "lessons-harvest": "heavy",
    "quarterly-review": "heavy",  # architect-quarterly.yml job id
    "scan": "light",
    "team-thread": "light",
    "okr-track": "light",
}
HUB_LOOP_JOBS: dict[str, str] = {
    "brief": "heavy",
    "legal": "heavy",
    "rollup": "heavy",
    "cos": "light",
    "growth": "light",
    "seo": "light",
    "finance": "light",
}
DEFAULT_JOB_TIERS: dict[str, str] = {**CODE_ACTION_JOBS, **HUB_LOOP_JOBS}

# --------------------------------------------------------------------------
# Schedules: key -> (workflow file, default local-time spec).
# Each workflow carries `# squadhq:schedule:<key>` on its cron line.
# --------------------------------------------------------------------------
SKILL_SCHEDULER = "skill-scheduler.yml"
SCHEDULES: dict[str, tuple[str, str]] = {
    "chief_of_staff": ("chief-of-staff.yml", "every 30m during working_hours"),
    "stuck_sweeper": ("stuck-sweeper.yml", "hourly :15 during working_hours"),
    "daily_brief": ("daily-brief.yml", "weekdays 07:30"),
    "debt_scan": ("tech-debt-weekly.yml", "sun,wed 04:00"),
    "seo": ("seo-weekly.yml", "mon,thu 05:00"),
    "lessons_harvester": ("lessons-harvester.yml", "mon 06:30"),
    "weekly_rollup": ("weekly-rollup.yml", "mon 07:00"),
    "growth": ("growth-nightly.yml", "daily 03:30"),
    "okr_tracker": ("okr-tracker.yml", "fri 05:00"),
    "legal": ("legal-weekly.yml", "wed 06:00"),
    "architect_quarterly": ("architect-quarterly.yml", "quarterly 1st 14:00"),
    "hub_janitor": ("hub-janitor.yml", "daily 06:45"),
    "finance": ("finance-weekly.yml", "fri 06:00"),
    "ecosystem_watch": ("ecosystem-watch.yml", "sat 06:00"),
    # Skill-driven jobs: all live in the generic skill-scheduler.yml, one
    # marked cron line per key. Monthly/quarterly defaults avoid the 1st
    # before 14:00 so the UTC conversion never falls into the previous month.
    "uptime_watch": (SKILL_SCHEDULER, "every 15m"),
    "weekly_dependencies": (SKILL_SCHEDULER, "mon 05:30"),
    "weekly_dast": (SKILL_SCHEDULER, "sat 03:00"),
    "weekly_test_health": (SKILL_SCHEDULER, "mon 06:00"),
    "release_train": (SKILL_SCHEDULER, "tue,thu 10:00"),
    "finops_weekly": (SKILL_SCHEDULER, "mon 08:00"),
    "quarterly_okr_draft": (SKILL_SCHEDULER, "quarterly 1st 14:00"),
    "weekly_feedback": (SKILL_SCHEDULER, "fri 09:00"),
    "monthly_competitive": (SKILL_SCHEDULER, "monthly 2nd 09:00"),
    "weekly_prompt_eval": (SKILL_SCHEDULER, "sun 05:00"),
    "monthly_model_routing": (SKILL_SCHEDULER, "monthly 3rd 09:00"),
    "compliance_monthly": (SKILL_SCHEDULER, "monthly 2nd 10:00"),
    "analytics_weekly": (SKILL_SCHEDULER, "mon 07:30"),
    "docs_gardener": (SKILL_SCHEDULER, "wed 05:00"),
    "weekly_localisation": (SKILL_SCHEDULER, "thu 05:00"),
}
CORE_SCHEDULE_KEYS: tuple[str, ...] = tuple(k for k, (wf, _s) in SCHEDULES.items() if wf != SKILL_SCHEDULER)
SKILL_SCHEDULE_KEYS: tuple[str, ...] = tuple(k for k, (wf, _s) in SCHEDULES.items() if wf == SKILL_SCHEDULER)

# Deprecated schedule keys (pre-CONVENTIONS) -> canonical key. Accepted with a warning.
SCHEDULE_ALIASES: dict[str, str] = {
    "seo_weekly": "seo",
    "lessons_harvest": "lessons_harvester",
    "growth_nightly": "growth",
    "okr_track": "okr_tracker",
    "legal_weekly": "legal",
    "finance_weekly": "finance",
}

# --------------------------------------------------------------------------
# Agents that can be switched on/off. Names are snake_case in squadhq.yml and
# rendered verbatim into SQUADHQ_DISABLED_AGENTS.
# --------------------------------------------------------------------------
KNOWN_AGENTS: tuple[str, ...] = (
    "product_owner",
    "architect",
    "designer",
    "engineer",
    "reviewer",
    "threat_modeler",
    "security",
    "concierge",
    "chief_of_staff",
    "daily_brief",
    "weekly_rollup",
    "growth",
    "seo",
    "legal",
    "finance",
    "ecosystem_watch",
    "lessons_harvester",
    "okr_tracker",
    "debt_scanner",
    "stuck_sweeper",
    "hub_janitor",
)

# provider -> allowed auth modes (first is the default). Mirrors the adapter
# registry in squadhq.providers; a provider without an adapter is rejected.
PROVIDER_AUTH: dict[str, tuple[str, ...]] = {
    "anthropic": ("api", "subscription"),
    "openai": ("api",),
    "gemini": ("api",),
    "openrouter": ("api",),
    "azure_openai": ("api",),
    "bedrock": ("iam", "api"),
    "together": ("api",),
    "openai_compatible": ("api", "none"),
    "ollama": ("none",),
    "vllm": ("none", "api"),
    "lmstudio": ("none",),
}
# provider -> auth mode -> secret holding the credential ("" = none)
PROVIDER_SECRETS: dict[str, dict[str, str]] = {
    "anthropic": {"api": "ANTHROPIC_API_KEY", "subscription": "CLAUDE_CODE_OAUTH_TOKEN"},
    "openai": {"api": "OPENAI_API_KEY"},
    "gemini": {"api": "GEMINI_API_KEY"},
    "openrouter": {"api": "OPENROUTER_API_KEY"},
    "azure_openai": {"api": "AZURE_OPENAI_API_KEY"},
    "bedrock": {"api": "AWS_BEARER_TOKEN_BEDROCK"},
    "together": {"api": "TOGETHER_API_KEY"},
    "openai_compatible": {"api": "OPENAI_COMPATIBLE_API_KEY"},
    "vllm": {"api": "VLLM_API_KEY"},
}
# Providers that exist elsewhere but have no adapter here: rejected with a hint.
UNSUPPORTED_PROVIDERS: dict[str, str] = {
    "vertex": "no Vertex AI adapter yet: use gemini (Gemini API key) or anthropic directly",
}
PROVIDERS: tuple[str, ...] = tuple(PROVIDER_AUTH)

# Model ids `squadhq doctor` recognises per provider. Unknown ids are a
# warning (providers ship models faster than we ship releases), never an error.
KNOWN_MODELS: dict[str, tuple[str, ...]] = {
    "anthropic": (
        "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
        "claude-opus-5", "claude-sonnet-5", "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001", "claude-haiku-4-5",
    ),
    "openai": ("gpt-5", "gpt-5-mini", "gpt-5-codex", "gpt-4.1", "o4-mini"),
    "gemini": ("gemini-2.5-pro", "gemini-2.5-flash"),
}

# squadhq.yml agent name -> every key the runtime checks against
# SQUADHQ_DISABLED_AGENTS: router/claude-code-action JOB values, agent_runner
# modes, schedule keys and workflow ids. The agent name itself is always added.
AGENT_JOB_KEYS: dict[str, tuple[str, ...]] = {
    "product_owner": ("po-shape", "po-continue", "team-thread", "product-owner"),
    "architect": ("architect", "architect_quarterly", "architect-quarterly", "quarterly-review"),
    "designer": ("design", "designer"),
    "engineer": ("implement", "review-fix", "engineer"),
    "reviewer": ("review", "reviewer"),
    "threat_modeler": ("threat-modeler",),
    "security": ("security-reviewer", "sast-triage", "secrets-response"),
    "concierge": ("concierge", "mention"),
    "chief_of_staff": ("chief_of_staff", "chief-of-staff", "cos"),
    "daily_brief": ("daily_brief", "daily-brief", "brief"),
    "weekly_rollup": ("weekly_rollup", "weekly-rollup", "rollup"),
    "growth": ("growth",),
    "seo": ("seo",),
    "legal": ("legal",),
    "finance": ("finance",),
    "ecosystem_watch": ("ecosystem_watch", "ecosystem-watch"),
    "lessons_harvester": ("lessons_harvester", "lessons-harvester", "lessons-harvest"),
    "okr_tracker": ("okr_tracker", "okr-tracker", "okr-track"),
    "debt_scanner": ("debt_scan", "scan", "backend-debt", "frontend-debt"),
    "stuck_sweeper": ("stuck_sweeper", "stuck-sweeper"),
    "hub_janitor": ("hub_janitor", "hub-janitor"),
}

EXECUTORS: tuple[str, ...] = ("claude-code", "codex", "aider", "gemini-cli")
# Only claude-code runs coding jobs in Actions mode today; the others are
# accepted for server/worker mode and reported as reserved by render/doctor.
ACTIONS_EXECUTORS: tuple[str, ...] = ("claude-code",)
EDITIONS: tuple[str, ...] = ("core", "pro", "enterprise")
TRACKERS: tuple[str, ...] = ("github", "jira", "linear", "trello")
NOTIFY_KINDS: tuple[str, ...] = ("github_issue", "slack", "email", "teams", "webhook", "discord", "pagerduty",
                                 "expo_push")
# Channels that cannot work without a target (the rest fall back to env vars).
NOTIFY_NEEDS_TARGET: tuple[str, ...] = ("email",)
GATES: tuple[str, ...] = ("sast", "sca", "secrets", "iac", "license", "sbom", "dast", "container")
# The one default for SQUADHQ_GATES (CONVENTIONS, router.yml and squadhq.yml agree).
DEFAULT_GATES: tuple[str, ...] = ("secrets", "sast", "sca", "license", "iac", "sbom")
ASSOCIATIONS: tuple[str, ...] = (
    "OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR",
    "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "MANNEQUIN", "NONE",
)
MERGE_MODES: tuple[str, ...] = ("approvers", "risk_based", "auto")
RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high")
RELEASE_STRATEGIES: tuple[str, ...] = ("merge_is_prod", "release_tag", "promote_branch")
DEPLOY_TARGETS: tuple[str, ...] = (
    "vercel", "railway", "fly", "netlify", "render", "heroku", "k8s", "helm",
    "github-pages", "cloudflare", "aws", "gcp", "azure", "custom", "none",
)
ON_EXCEED: tuple[str, ...] = ("pause", "downgrade", "notify")
OPERATING_MODELS: tuple[str, ...] = ("solo", "team")
DST_POLICIES: tuple[str, ...] = ("daylight", "standard")
ALWAYS_HUMAN_AREAS: tuple[str, ...] = (
    "auth", "payments", "migrations", "workflows", "infra", "deps-major", "secrets", "pii",
)

# The single required check every project ruleset needs (stub job id `hub`).
REQUIRED_CHECK = "hub / ci-gate"
