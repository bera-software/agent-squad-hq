# LLM providers and executors

Agent Squad HQ calls models in two different ways:

- **Executors** run a coding agent in a checkout with tools (implement, review, design, mention, most skills).
- **Providers** answer a single prompt for **hub loops** (Chief of Staff, Daily Brief, Weekly Rollup, Growth, SEO, Legal, Finance) and for live eval runs.

## Executors

| Executor | `providers.executor` | Actions mode | Notes |
|---|---|---|---|
| Claude Code | `claude-code` (default) | **Supported.** `run-agent` runs `anthropics/claude-code-action`, pinned by SHA | Auth from `ANTHROPIC_API_KEY` (`auth: api`) or `CLAUDE_CODE_OAUTH_TOKEN` (`auth: subscription`) |
| OpenAI Codex CLI | `codex` | Not yet | Output normaliser and tool-profile translation exist |
| Gemini CLI | `gemini-cli` | Not yet | Output normaliser and tool-profile translation exist |
| aider | `aider` | Not yet | Best-effort output normaliser exists |

`run-agent` fails the step with "executor ... is not available in Actions mode yet" for anything but `claude-code`. The workflows read the executor from `SQUADHQ_EXECUTOR` (default `claude-code`), which `squadhq render` does not set.

Every executor's native output is normalised to one `execution-result.json` (outcome, error class, cost, tokens, turns, model) by `python -m squadhq.executors.normalize`. The cost ledger, failure handler and capacity breaker read only that file ([ADR-0004](../adr/0004-neutral-execution-result.md)).

### Auth modes for Claude Code

- `providers.anthropic.auth: api` (default): metered API key. Coding agents and hub loops use `ANTHROPIC_API_KEY`.
- `providers.anthropic.auth: subscription`: coding agents use `CLAUDE_CODE_OAUTH_TOKEN` (created with `claude setup-token`). Whether automated use of a subscription token is permitted, its capacity limits and its one-year lifetime are your responsibility under the provider's terms. Hub loops still need `ANTHROPIC_API_KEY`. `squadhq doctor` warns as the token ages. When the subscription's capacity trips, coding jobs pause for 60 minutes, or fall back to API auth if `providers.fallback_on_capacity_trip: true`.
- `providers.anthropic.pool`: up to five accounts (`{id, auth, secret}`), each with its own capacity breaker. A trip pauses only the account that ran; the next job (and the automatic rerun of the tripped run) uses the next available account. Only when every account is tripped do jobs pause or fall back to metered API. Selection happens in the prep job; the agent job receives only the selected credential. See [account pool](../configuration/squadhq-yml.md#account-pool). Rotating several API keys or organisation (team / enterprise) accounts is fine. Pooling or rotating personal consumer subscriptions for automated use may violate the provider's terms; whether your use is permitted is your responsibility.

## Models and tiers

Each job belongs to a tier, and each tier names a primary and an optional fallback model as `provider/model-id`:

```yaml
providers:
  routing:
    heavy: { primary: anthropic/claude-sonnet-5-5, fallback: anthropic/claude-sonnet-5 }
    light: { primary: anthropic/claude-haiku-5-5, fallback: anthropic/claude-sonnet-5-5 }
  jobs:
    team-thread: light       # override one job's tier
```

`squadhq validate --explain` prints every job's tier and model. `squadhq doctor` warns about model ids it does not recognise for a provider (a warning, not an error, because providers ship models faster than we ship releases).

**Coding-agent fallback.** When a run fails with a model-specific transient error, the failure handler sets a one-shot `MODEL_OVERRIDE_<JOB>` repository variable and re-runs the job; `squadhq.resolve_model` uses the fallback model for that one retry and deletes the variable.

**Hub-loop fallback** (`squadhq.providers.complete_with_fallback`), within at most 3 attempts:

1. A transient failure (timeout, 429, 5xx) retries the same model with exponential backoff, honouring `Retry-After`.
2. A model-specific failure (overloaded, model not found or retired) switches to the fallback at once.
3. A provider-wide failure (auth, billing, quota) switches only if the fallback is on a different provider.
4. Any other error (for example a bad request) is raised immediately.
5. The last attempt always goes to the fallback if it has not been tried. At most one switch happens.

## Provider adapters (hub loops)

| Provider id | API | Environment variables |
|---|---|---|
| `anthropic` | Messages API | `ANTHROPIC_API_KEY`; optional `ANTHROPIC_BASE_URL`, `ANTHROPIC_VERSION` |
| `openai` | Responses API (default) or Chat Completions | `OPENAI_API_KEY`; optional `OPENAI_BASE_URL`, `OPENAI_API_STYLE` (`responses` \| `chat`), `OPENAI_ORG_ID`, `OPENAI_PROJECT_ID` |
| `azure_openai` | Chat Completions on a deployment, or the v1 Responses API | `AZURE_OPENAI_ENDPOINT`; `AZURE_OPENAI_API_KEY` or `AZURE_OPENAI_AD_TOKEN`; optional `AZURE_OPENAI_API_VERSION` (default `2024-10-21`), `AZURE_OPENAI_API_STYLE`, `AZURE_OPENAI_DEPLOYMENT_MODELS` (`deployment=model,...`, for pricing). The model id is the deployment name |
| `gemini` | Generative Language `generateContent` | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`); optional `GEMINI_BASE_URL`. For Vertex AI set `GEMINI_BASE_URL` to the publisher endpoint and `GEMINI_BEARER_TOKEN` to an OAuth access token |
| `bedrock` | Converse API, SigV4 signed without boto3 | `AWS_REGION` or `AWS_DEFAULT_REGION` (default `us-east-1`); `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` (+ `AWS_SESSION_TOKEN`), or `AWS_BEARER_TOKEN_BEDROCK`; optional `BEDROCK_ENDPOINT_URL`. Instance-profile and SSO credential discovery are not implemented: export the variables |
| `ollama` | OpenAI-compatible | `OLLAMA_BASE_URL` (default `http://localhost:11434/v1`), optional `OLLAMA_API_KEY` |
| `vllm` | OpenAI-compatible | `VLLM_BASE_URL` (default `http://localhost:8000/v1`), optional `VLLM_API_KEY` |
| `lmstudio` | OpenAI-compatible | `LMSTUDIO_BASE_URL` (default `http://localhost:1234/v1`), optional `LMSTUDIO_API_KEY` |
| `openrouter` | OpenAI-compatible | `OPENROUTER_API_KEY` (required); optional `OPENROUTER_BASE_URL` |
| `together` | OpenAI-compatible | `TOGETHER_API_KEY` (required); optional `TOGETHER_BASE_URL` |
| `openai_compatible` | Any OpenAI-compatible server | `OPENAI_COMPATIBLE_BASE_URL` (required), optional `OPENAI_COMPATIBLE_API_KEY` |

Aliases: `azure` → `azure_openai`, `google` → `gemini`, `aws` → `bedrock`, `claude` → `anthropic`, `lm_studio` → `lmstudio`. A bare model id with no provider prefix uses `SQUADHQ_DEFAULT_PROVIDER` (default `anthropic`).

Capabilities (web search, JSON mode, thinking controls) and prices per model come from `src/squadhq/providers/prices.yml`; `SQUADHQ_PRICES_FILE` points at a replacement. Unsupported features (for example web search on Bedrock Converse or on OpenAI-compatible servers) are ignored with a warning.

## Current limitations

!!! warning "Read before you route hub loops away from Anthropic"
    - **Non-Anthropic routing through `squadhq.yml` does not reach the adapters yet in Actions mode.** `squadhq render` writes bare model ids into `config/models.json` (with the provider in a separate `primary_provider` field that the runtime does not read), so a bare id resolves to `SQUADHQ_DEFAULT_PROVIDER`, which the workflows do not set. Until this is fixed, keep hub-loop tiers on Anthropic models in Actions mode.
    - The hub workflows pass only `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and `GEMINI_API_KEY` to hub loops. Other providers' variables (`AZURE_OPENAI_*`, `AWS_*`, `OLLAMA_BASE_URL`, …) would need workflow changes. In server mode, set them in the server environment.
    - `providers.<name>.base_url`, `.region` and `.secret` in `squadhq.yml` are rendered into `config/auth.json`, but the adapters read their environment variables, not that file.
    - `vertex` is accepted in `squadhq.yml` but has no adapter of its own; use `gemini` with `GEMINI_BASE_URL` and `GEMINI_BEARER_TOKEN` as described above.

## Adding a provider

See [CONTRIBUTING.md](https://github.com/bera-software/agent-squad-hq/blob/main/CONTRIBUTING.md#adding-an-llm-provider-hub-loops).
