# Upgrading

Agent Squad HQ follows [semantic versioning](https://semver.org/). Before 1.0.0, a minor release may contain breaking changes; the [CHANGELOG](https://github.com/berasoftware/agent-squad-hq/blob/main/CHANGELOG.md) lists them with upgrade steps.

## How versions are pinned (Actions mode)

There are two things to keep in step:

1. **Your hub repository.** It is your copy of this repository plus your `squadhq.yml`. The hub's own scheduled workflows run whatever is on its default branch.
2. **Each project's `agents.yml` stub.** It calls `router.yml` from the hub **at a full commit SHA**, with the release tag in a comment, and passes the same repository and SHA as inputs. The router then checks out the hub at that SHA, so the router code, prompts, config and the `squadhq` package always come from the same version.

The SHA comes from `SQUADHQ_STUB_REF` when onboarding and stub sync render the stub. The onboarding workflow sets it from `vars.SQUADHQ_HUB_REF` (default `main`).

## Agent Squad HQ Cloud

On Cloud you do not merge anything by hand. When a new version ships, Cloud opens a pull request
in your hub, **chore: upgrade Agent Squad HQ to X.Y.Z** (you can also open it from the Overview
page). It is one commit made by `squadhq.hub_upgrade`:

- engine files (workflows, actions, prompts, packs, skills, `src/squadhq`, ...) are replaced with
  the new version's;
- `config/*.json` and the workflow schedules are rendered again from your `squadhq.yml`;
- `squadhq.yml`, `README.md`, `.gitignore` and files outside the template are left alone;
- `.github/squadhq-manifest.json` records what the upgrade wrote. From the second upgrade on, an
  engine file you changed is kept and listed in the PR description instead of being replaced.

Review and merge it. The manifest change starts **Stub Sync**, which opens a PR in each project
(step 4 below).

## Upgrade steps

1. **Read the changelog** for every version between yours and the target.
2. **Merge the new release into your hub.** For example, add this repository as an `upstream` remote in your hub checkout, fetch the release tag and merge it. Resolve conflicts in files you changed (usually `squadhq.yml`, sometimes prompts or packs).
3. **Re-render and validate:**

    ```bash
    squadhq validate
    squadhq render
    squadhq render --check     # must report "up to date"
    git add -A && git commit -m "chore: upgrade to vX.Y.Z" && git push
    ```

4. **Update the project stubs.** The **Stub Sync** workflow in the hub runs on every published release of the hub, on pushes that change `onboarding/agents.yml.stub`, and on demand. For each project whose stub differs it pushes branch `squadhq-stub-sync` and opens (or reuses) a PR. It never pushes to a default branch. Merge those PRs.

    To pin a specific ref, run **Stub Sync** manually with the `ref` input, or set `SQUADHQ_HUB_REF` to a tag (for example `v0.1.0`).

5. **Check:** `squadhq doctor`, then `squadhq drill --repo <a project> --apply`.

!!! tip "Tag your hub"
    Stub Sync renders the stub with the release tag when it runs on a `release` event. If you publish releases in your hub (for example `v0.1.0-acme.1`), projects pin to those, and you control exactly when they move.

## Server mode

Back up Postgres, pin the new image by digest, and restart. The `migrate` step runs first, and migrations are additive within a major version. The full procedure, including Helm and rollback, is in [upgrades and backups](../deploy/upgrade-and-backup.md).

## Configuration changes

`squadhq validate` treats unknown keys as errors and deprecated schedule keys (for example `seo_weekly`, `okr_track`) as warnings with the new name. Fix the warnings when you upgrade; deprecated aliases may be removed in a later minor release.
