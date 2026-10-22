# ADR-0011: Token-broker mode

- **Status:** Accepted
- **Date:** 2026-10-26

## Context

Every Actions-mode job mints GitHub App installation tokens with `actions/create-github-app-token`, so each organisation stores two App private keys (`SQUADHQ_APP_PRIVATE_KEY`, `SQUADHQ_AGENT_APP_PRIVATE_KEY`) as Actions secrets. Those keys are the most valuable secrets in the installation: anyone who reads one can mint tokens for every repository the App is installed on, for as long as the key is valid.

A hosted service, or an organisation's own platform team, may want to own the Apps centrally: install them once, keep the keys in a vault, and hand out short-lived tokens to the workflows that need them, with rules about which repository and which workflow may ask. GitHub Actions already gives every job a signed identity for this, the OIDC token (`id-token: write`), with claims for the repository, owner, run and the exact workflow file (`job_workflow_ref`).

## Decision

Add an optional **token-broker mode**, generic to any host that implements the contract below.

- It is on when the repository or organisation variable `SQUADHQ_TOKEN_BROKER` holds an https URL (`hub.token_broker` in `squadhq.yml`, rendered by `squadhq render` and set by `squadhq init --token-broker URL`). `SQUADHQ_TOKEN_BROKER_AUDIENCE` overrides the OIDC audience; it defaults to the broker URL.
- Every token mint goes through one client, `.github/actions/squadhq-token` (`broker.py`, standard library only). In broker mode it requests an OIDC token for the audience and calls `POST <broker>/v1/broker/token` with `{"identity": "ops" | "agent", "repositories": [...], "permissions": {...}, "purpose": "<job>"}`. `permissions` is the **same** set the App-key path passes to `create-github-app-token` at that site (the broker may only narrow), so broker mode is never broader than App-key mode; a test compares every broker step with its App-key sibling. It masks the OIDC token and the returned installation token before anything else is printed, refuses redirects, and fails with the broker's error code and detail, never with a token. Without the variable it calls `actions/create-github-app-token` at the same pinned commit as before, with the same inputs.
- Where the hub is not yet on disk (the reusable `router.yml` and `squadhq-agent.yml` mint the token that checks the hub out), the workflows carry a verbatim copy of `broker.py`. A test keeps the copies identical. `setup-hub` calls the client by path; `agent-prep` uses the composite action.
- `id-token: write` is granted only to jobs that mint tokens (prep, post and deterministic jobs) and to the callers of reusable workflows that contain such jobs. It is never granted to a job that runs an agent or untrusted code, or to a caller of the agent-only `agent-exec.yml`. `.github/lint/isolation.py` enforces this, together with "no `squadhq-token` and no broker or OIDC request in an agent job" and "no `id-token: write` on a job that mints nothing".
- The agent job gets exactly what it gets in App-key mode: the agent token and a `contents: read` ops token for the hub checkout, both sealed in the hand-off and revoked by the post job.
- Agent tokens: from a project, the calling repository; from the hub, `agent-repositories: "@projects"` asks for the hub plus every registered project, as in App-key mode (at most 25, checked client-side).
- Broker tokens are ordinary installation tokens and are revoked at job end like App-key tokens: the client lists each token it mints in `$RUNNER_TEMP/squadhq-broker-tokens` (mode 0600), and every minting job ends with an `if: always()` step that calls `DELETE /installation/token` for each (composite actions have no post step; a test checks every minting job). Tokens that must outlive the job (`skip-token-revoke`, the agent's hand-off tokens) are not listed and are revoked by the post job as before.
- Broker mode is never single-app mode: the broker mints agent tokens from its own agent App, so the separate-identity guarantees (markers trusted only from the ops login, unattended merges allowed by policy) hold. Bot logins still come from configuration (`hub.bot`, `hub.agent_bot`).
- Post jobs report a run summary (`run_id`, `job`, `outcome`, and `cost_usd` / token counts when the normalised execution result has them) to `POST <broker>/v1/broker/usage`, best effort: the step is `continue-on-error` and the client always exits 0.

The broker contract (`POST /v1/broker/token`, `POST /v1/broker/usage`): the bearer is the GitHub Actions OIDC JWT; the broker verifies it (RS256, GitHub's JWKS, issuer, audience, expiry), maps `repository_owner_id` to an installation, accepts only calls from the registered hub or enabled projects whose `job_workflow_ref` points at the hub's workflows, and answers `{"token", "expires_at", "identity", "login", "repositories", "permissions"}` or `{"error", "detail"}`.

## Consequences

- An organisation in broker mode stores no App private key in GitHub. Leaking a workflow log or a secret no longer leaks a key; the broker becomes the key holder instead.
- The broker is a high-value target: whoever controls it can mint tokens for every tenant that installed its Apps. The threat model lists the risks and the controls that remain on the GitHub side.
- Token scope and lifetime match App-key mode: the same narrowed permissions per job, revocation at job end, the agent's tokens revoked by the post job.
- If a minting job is killed before its last step (runner lost, cancellation past the grace period), its broker tokens live until they expire, within the hour. App-key mode has the same gap when the action's post step does not run.
- Callers must grant `id-token: write` along the whole reusable-workflow chain. The project stub (`agents.yml`, `squadhq-agent.yml`) carries it in its permission ceiling, so a stub-sync is needed before a project can use broker mode; App-key mode does not use the permission.
- With the variable unset, every token is minted exactly as before; the only differences are the added `id-token: write` permission on minting jobs and two unused environment variables.

## Alternatives considered

- **A third-party "OIDC to App token" action.** Rejected: one more pinned dependency in the most sensitive path, and none implements the per-tenant, per-workflow rules.
- **Referencing the client as a remote action** (`owner/agent-squad-hq/.github/actions/squadhq-token@sha`) from the reusable workflows. Rejected for now: private hubs, and a commit cannot pin itself. The verbatim copy plus a drift test gives the same single source.
- **Shipping the hub tooling to the agent job in the hand-off artifact** (`git archive`) instead of a hub read token. Built first, while the contract could not narrow ops tokens; dropped once `permissions` was added. The narrowed token is the App-key design unchanged (one repository, `contents: read`, revoked by the post job), whereas the archive put a private hub's files into the project's artifacts, readable by anyone who can download them, and added an unpack path in the agent job.
