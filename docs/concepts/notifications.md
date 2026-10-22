# Notifications

## What you get in Actions mode

Everything surfaces as GitHub issues and comments, with no extra setup:

| What | Where |
|---|---|
| Escalations (job failures, budget cap, capacity trips, stalls) | An `escalation` issue in the hub. A repeat of the same failure comments on the open issue instead of opening another. The hub janitor merges duplicates |
| Daily Brief, Weekly Rollup | A report issue in the hub labelled `daily-brief` / `weekly-rollup` |
| Department reports | Hub issues labelled `report` and `project:<name>` |
| Cost ledger | A pinned monthly hub issue, `Cost ledger — <YYYY-MM>` |
| Work that needs a person | `needs-human` or `needs-approver-merge` on the ticket or PR, with a comment saying why |
| Onboarding audits | A hub issue per audit |

Watch the hub repository (or subscribe to the `escalation` label) to receive these as GitHub notifications.

## Notifier adapters

`squadhq.notify` provides adapters behind one `Notifier` interface, plus a command line that fans one message out to several targets:

```bash
python -m squadhq.notify send \
  --targets "github_issue,slack:#eng-alerts,email:lead@acme.example" \
  --title "Deploy blocked" --body-file body.md --severity warning --kind escalation \
  --dedupe-key deploy-blocked
```

It exits 0 if at least one target succeeded, 1 if all failed, 2 on usage errors.

| Target | Sends | Environment |
|---|---|---|
| `github_issue[:owner/repo]` | Opens an issue in the hub (or the given repo), or comments on the open one with the same dedupe key | `GH_TOKEN`; optional `SQUADHQ_NOTIFY_REPO`, `SQUADHQ_NOTIFY_LABEL` (default `notification`) |
| `slack[:#channel]` | Slack incoming webhook, Block Kit layout. The channel override only works with legacy webhooks | `SLACK_WEBHOOK_URL` |
| `teams` | Microsoft Teams Workflows (or legacy connector) webhook, Adaptive Card | `TEAMS_WEBHOOK_URL` |
| `email[:a@b.example,c@d.example]` | SMTP, STARTTLS by default | `SQUADHQ_SMTP_HOST`, `SQUADHQ_SMTP_PORT` (587, or 465 with `ssl`), `SQUADHQ_SMTP_USER`, `SQUADHQ_SMTP_PASSWORD`, `SQUADHQ_SMTP_FROM`, `SQUADHQ_SMTP_TLS` (`starttls` \| `ssl` \| `none`), `SQUADHQ_NOTIFY_EMAIL_TO` |
| `webhook[:https://...]` | JSON POST. With a secret, adds `X-SquadHQ-Signature-256: sha256=<HMAC-SHA256 of the body>`, the same scheme as GitHub's | `SQUADHQ_WEBHOOK_URL`, optional `SQUADHQ_WEBHOOK_SECRET` |
| `pagerduty` | PagerDuty Events API v2. The dedupe key becomes PagerDuty's `dedup_key` | `PAGERDUTY_ROUTING_KEY` |
| `expo_push` | Push to mobile devices registered with the server, by topic: approvals, escalations, brief | Server mode only |

## The `notifications:` section of `squadhq.yml`

```yaml
notifications:
  escalations: [github_issue, "slack:#eng-alerts"]
  digest: [github_issue, "email:leads@acme.example"]
```

!!! note "Validated, not wired yet"
    `squadhq validate` checks these channel lists (known kinds, a target where one is needed, valid email addresses), but the Actions-mode workflows do not read them yet: escalations and digests go to GitHub issues as described above. To send elsewhere today, call `python -m squadhq.notify send` from your own workflow step, or run server mode.

    The schema also accepts `discord:<url>`, but there is no Discord notifier. `pagerduty` and `expo_push` exist as notifiers but are not accepted in this section yet.
