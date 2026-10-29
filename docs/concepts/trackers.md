# Trackers

A tracker is where tickets live. The flow engine works in terms of the state labels in `config/flows.yml`; each tracker adapter maps those onto the tracker's native statuses, lists or labels.

## Support by runtime

| Tracker | Adapter | Actions mode | Server mode |
|---|---|---|---|
| GitHub Issues | `squadhq.trackers.github` | **Yes** (the router uses `gh` and labels directly) | Yes, webhook `/api/v1/webhooks/github` |
| Jira Cloud | `squadhq.trackers.jira` (REST v3) | No | Yes, `/api/v1/webhooks/jira` |
| Linear | `squadhq.trackers.linear` (GraphQL) | No | Yes, `/api/v1/webhooks/linear` |
| Trello | `squadhq.trackers.trello` (REST v1) | No | Yes, `/api/v1/webhooks/trello` |
| GitLab Issues | `squadhq.trackers.gitlab` (REST v4) | No | Yes, `/api/v1/webhooks/gitlab` |

Jira, Linear, Trello and GitLab need server mode because they cannot start a GitHub Actions run. The adapters are tested against recorded API responses; they have not had sustained production use.

In `squadhq.yml`, `trackers.default` accepts `github`, `jira`, `linear` or `trello`, each non-GitHub value with its settings block. GitLab is available to the server through its adapter and webhook but is not yet selectable in `squadhq.yml`.

```yaml
trackers:
  default: jira
  jira: { site: acme.atlassian.net, project: ENG, status_map: default }
  # linear: { team: ENG }
  # trello: { board: <board id> }
```

## Credentials

| Tracker | Environment variables |
|---|---|
| GitHub | `GH_TOKEN` or `GITHUB_TOKEN`; optional `GITHUB_API_URL` (GitHub Enterprise Server) |
| Jira Cloud | `JIRA_BASE_URL` (`https://<site>.atlassian.net`), `JIRA_EMAIL`, `JIRA_API_TOKEN`; optional `JIRA_ISSUE_TYPE` (default `Task`) |
| Linear | `LINEAR_API_KEY` (a personal API key, or `Bearer <oauth token>`); optional `LINEAR_API_URL` |
| Trello | `TRELLO_API_KEY`, `TRELLO_TOKEN`; optional `TRELLO_API_URL` |
| GitLab | `GITLAB_TOKEN`; optional `GITLAB_URL` (default `https://gitlab.com`) |

Webhook secrets are separate: `SQUADHQ_WEBHOOK_SECRET_<SOURCE>`. See [Connect your sources](../deploy/docker-compose.md#connect-your-sources).

## Status maps

`src/squadhq/trackers/status_maps.yml` maps native states to flow labels and back. GitHub and GitLab carry flow labels natively, so their maps are empty. The defaults:

| Flow label | Jira status | Linear state | Trello list |
|---|---|---|---|
| `proposal:pending` | | Triage | |
| `triaged:ready` | Selected for Development | Todo | Ready |
| `status:in-progress` | In Progress | In Progress | Doing |
| `status:in-review` | In Review | In Review | Review |
| `needs-human` | Blocked | Blocked | Blocked |
| closed (completed) | Done | Done | Done |
| closed (not planned) | Won't Do | Canceled | Done |

Native names are matched case-insensitively, and several native names can map to one label (for example Jira "Code Review" and "In Review"). Labels that are not state labels stay ordinary labels in Jira, Linear and Trello (created on the board or team when missing). Jira descriptions and comments are converted between Markdown and the Atlassian Document Format. A Trello card that is closed moves to the done list when the board has one, then is archived.

To use your own workflow names, change the map for your deployment (adapters accept an `overrides=` mapping of the same shape).

## Trust model

Whether a ticket event may start agent work is decided by code from the author's association, never by a model.

| Tracker | How association is derived | Consequence |
|---|---|---|
| GitHub | GitHub's `author_association`; bot accounts are `BOT` | `SQUADHQ_TRUSTED_ASSOCIATIONS` in both Actions and server mode (default `OWNER,MEMBER,COLLABORATOR`; `SQUADHQ_ALLOWED_ASSOCIATIONS` is a deprecated server-mode alias) |
| GitLab | Project membership: access level 50 → `OWNER`, 30/40 → `MEMBER`, 10/20 → `CONTRIBUTOR`, not a member → `NONE` | Reading tickets resolves membership. **Webhook events arrive as `NONE`** |
| Linear | A human creator is `MEMBER` (a workspace has no outside contributors); integrations are `BOT` | Anyone in the workspace counts as trusted |
| Jira Cloud | Licensed site users (`accountType: atlassian`) are `MEMBER`, apps are `BOT`, everyone else (for example service-desk customers) is `NONE` | Best effort. **Webhook events arrive as `NONE`** |
| Trello | Card creators are `MEMBER` (creating needs board membership); commenters are `MEMBER` only if on the board's member list | Public boards can allow outside comments. **Webhook events arrive as `NONE`** |

For webhook events marked `NONE` (all Jira, Trello and GitLab events), the server only starts agent work when the actor is the bot or appears in `SQUADHQ_TRUSTED_ACTORS` (a comma-separated list of tracker account ids or usernames of your approvers). Everything else **fails closed**. Configure that list deliberately; it is the only thing standing between a Jira service-desk customer or a public Trello commenter and a paid agent run.

Ticket text from any tracker is untrusted and is [fenced](../security/security-model.md#untrusted-input) before it reaches a model, whoever wrote it.

## Adding a tracker

See [CONTRIBUTING.md](https://github.com/bera-software/agent-squad-hq/blob/main/CONTRIBUTING.md#adding-a-tracker).
