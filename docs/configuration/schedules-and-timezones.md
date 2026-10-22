# Schedules and time zones

Schedules in `squadhq.yml` are written in **local time** in `org.timezone`. In Actions mode, `squadhq render` turns them into the UTC cron lines GitHub needs. In server mode the scheduler evaluates them in the time zone directly.

```yaml
org:
  timezone: Europe/London
  working_hours: "07:00-20:00"     # quote times: YAML 1.1 reads 07:30 as a number
  working_days: mon-fri
  dst: daylight

schedules:
  chief_of_staff: "every 30m during working_hours"
  daily_brief: "weekdays 07:30"
  debt_scan: "sun,wed 04:00"
```

## Grammar

| Form | Example | Meaning |
|---|---|---|
| `DAYS HH:MM[,HH:MM...]` | `weekdays 07:30`, `sun,wed 04:00`, `mon-fri 09:00,15:00`, `daily 03:30` | At these local times on these days |
| `hourly [:MM] [during WINDOW]` | `hourly`, `hourly :15 during working_hours` | Every hour at minute `MM` (default `:00`) |
| `every N(m\|h) [during WINDOW]` | `every 30m during working_hours`, `every 2h` | Minute steps must divide an hour (5, 10, 15, 20, 30); hour steps must divide a day (1, 2, 3, 4, 6, 8, 12) |
| `DAYS hourly ...` / `DAYS every ...` | `mon-fri every 15m` | Restrict to some days |
| `monthly <Nth> HH:MM` | `monthly 1st 05:00` | Once a month |
| `quarterly <Nth> HH:MM` | `quarterly 1st 07:00` | January, April, July, October |
| five cron fields | `30 6 * * 1-5` | Used **verbatim as UTC**, no conversion |

`DAYS` is `daily`, `weekdays`, `weekends`, a list (`sun,wed`) or a range (`mon-fri`) of `sun mon tue wed thu fri sat`. `WINDOW` is `working_hours` (that is, `org.working_hours` on `org.working_days`) or an explicit `HH:MM-HH:MM`; the end is exclusive.

The validator rejects malformed phrases with a hint, a window that contains no run time, and a monthly or quarterly schedule whose UTC conversion would fall into the previous month (for example the 1st before 10:00 local time in a zone east of UTC+10). The defaults avoid this by using 14:00 for quarterly jobs on the 1st.

## Schedule keys and defaults

Each key matches a `# squadhq:schedule:<key>` marker on a cron line in `.github/workflows/`. `squadhq doctor --check schedule-markers` confirms every key has a marker and every marker has a key.

| Key | Default (local time) | Workflow |
|---|---|---|
| `chief_of_staff` | `every 30m during working_hours` | `chief-of-staff.yml` |
| `stuck_sweeper` | `hourly :15 during working_hours` | `stuck-sweeper.yml` |
| `daily_brief` | `weekdays 07:30` | `daily-brief.yml` |
| `weekly_rollup` | `mon 07:00` | `weekly-rollup.yml` |
| `debt_scan` | `sun,wed 04:00` | `tech-debt-weekly.yml` |
| `seo` | `mon,thu 05:00` | `seo-weekly.yml` |
| `growth` | `daily 03:30` | `growth-nightly.yml` |
| `legal` | `wed 06:00` | `legal-weekly.yml` |
| `finance` | `fri 06:00` | `finance-weekly.yml` |
| `okr_tracker` | `fri 05:00` | `okr-tracker.yml` |
| `lessons_harvester` | `mon 06:30` | `lessons-harvester.yml` |
| `ecosystem_watch` | `sat 06:00` | `ecosystem-watch.yml` |
| `hub_janitor` | `daily 06:45` | `hub-janitor.yml` |
| `architect_quarterly` | `quarterly 1st 14:00` | `architect-quarterly.yml` |
| `uptime_watch` | `every 15m` | `skill-scheduler.yml` |
| `weekly_dependencies` | `mon 05:30` | `skill-scheduler.yml` |
| `weekly_dast` | `sat 03:00` | `skill-scheduler.yml` |
| `weekly_test_health` | `mon 06:00` | `skill-scheduler.yml` |
| `release_train` | `tue,thu 10:00` | `skill-scheduler.yml` |
| `finops_weekly` | `mon 08:00` | `skill-scheduler.yml` |
| `quarterly_okr_draft` | `quarterly 1st 14:00` | `skill-scheduler.yml` |
| `weekly_feedback` | `fri 09:00` | `skill-scheduler.yml` |
| `monthly_competitive` | `monthly 2nd 09:00` | `skill-scheduler.yml` |
| `weekly_prompt_eval` | `sun 05:00` | `skill-scheduler.yml` |
| `monthly_model_routing` | `monthly 3rd 09:00` | `skill-scheduler.yml` |
| `compliance_monthly` | `monthly 2nd 10:00` | `skill-scheduler.yml` |
| `analytics_weekly` | `mon 07:30` | `skill-scheduler.yml` |
| `docs_gardener` | `wed 05:00` | `skill-scheduler.yml` |
| `weekly_localisation` | `thu 05:00` | `skill-scheduler.yml` |

Skill-driven keys run only if the skill that uses them is enabled. Deprecated keys (`seo_weekly`, `lessons_harvest`, `growth_nightly`, `okr_track`, `legal_weekly`, `finance_weekly`) are accepted with a warning.

See what you will get with:

```bash
squadhq validate --explain
```

```text
schedules (UTC)
  chief_of_staff         0,30 6-18 * * 1-5   # every 30m during working_hours Europe/London -> UTC+01:00; run 1h earlier local time outside DST
  daily_brief            30 6 * * 1-5        # weekdays 07:30 Europe/London -> UTC+01:00; run 1h earlier local time outside DST
```

## Daylight saving time

GitHub Actions cron has no time zone: every expression is UTC. A zone with daylight saving has two UTC offsets a year, so one rendered cron line cannot be right all year.

**Decision.** `render` picks **one** offset per render, chosen by `org.dst`, and never emits two crons for the same schedule. Two crons would make every job run twice for half the year.

| `org.dst` | Offset used | Summer | Winter |
|---|---|---|---|
| `daylight` (default) | The zone's summer offset | Runs at the configured local time | Runs **one hour earlier** |
| `standard` | The zone's winter offset | Runs **one hour later** | Runs at the configured local time |

The default favours "never late": a 07:30 brief arrives at 06:30 in winter rather than 08:30 in summer. The chosen offset and the caveat are written into each rendered cron line's comment, `squadhq render` prints a DST warning, and `squadhq doctor` (checks `dst` and `schedules`) reports which offset the crons were rendered for.

You do not need to re-render when the clocks change. Re-render only when you change `squadhq.yml`.

**Server mode** does not have this limitation. Its scheduler evaluates the local wall-clock times in `org.timezone` with real DST handling, so jobs run at the configured local time all year.

## Reliability of GitHub cron

GitHub runs scheduled workflows on a best-effort basis: runs can start late or be skipped under load, and schedules on inactive repositories can be disabled. Agent Squad HQ is designed for that. The stuck sweeper and ticket transitions recover any work that was waiting on a missed run, and every scheduled workflow also has `workflow_dispatch` so you can run it by hand. If you need punctual schedules, use [server mode](../getting-started/quickstart-server-mode.md).
