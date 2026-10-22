import { formatRelative, formatTimestamp, isTripped, type Capacity, type Run } from "@squadhq/shared-api";
import { LocalTime } from "./LocalTime";

export const POOL_DOCS_URL = "https://bera-software.github.io/agent-squad-hq/configuration/squadhq-yml/#account-pool";

const STRATEGY_TEXT: Record<string, string> = {
  ordered: "Ordered: each job uses the first available account, in the order below.",
  round_robin: "Round robin: jobs rotate across the available accounts.",
};

const SOURCE_TEXT: Record<string, string> = {
  "auth.json": "providers.anthropic.pool in squadhq.yml",
  env: "discovered from the worker environment",
};

function authLabel(auth: string): string {
  if (auth === "api") return "API key";
  if (auth === "subscription") return "Subscription";
  return auth || "–";
}

export function NoPoolConfigured() {
  return (
    <section className="card" aria-labelledby="no-pool-title">
      <h2 id="no-pool-title">No account pool configured</h2>
      <p>
        Coding-agent jobs use the single <code>providers.anthropic.secret</code> credential. To spread jobs over
        several accounts, so that a usage limit on one pauses only that account, add{" "}
        <code>providers.anthropic.pool</code> to <code>squadhq.yml</code>:
      </p>
      <pre>
        {`providers:
  anthropic:
    pool:
      - { id: team-a, auth: subscription }   # CLAUDE_CODE_OAUTH_TOKEN_1
      - { id: org-key, auth: api, secret: ANTHROPIC_API_KEY_2 }
    pool_strategy: ordered                   # or round_robin`}
      </pre>
      <p className="hint">
        In server mode the worker also discovers <code>ANTHROPIC_API_KEY_1..5</code> and{" "}
        <code>CLAUDE_CODE_OAUTH_TOKEN_1..5</code> from its environment. See{" "}
        <a href={POOL_DOCS_URL} rel="noopener noreferrer">
          Account pool
        </a>{" "}
        (<code>docs/configuration/squadhq-yml.md#account-pool</code>).
      </p>
    </section>
  );
}

/** Read-only view of GET /api/v1/capacity. `lastRuns` (optional) maps account id to its latest run. */
export function AccountsPanel({
  capacity,
  lastRuns,
  now = new Date(),
}: {
  capacity: Capacity;
  lastRuns?: ReadonlyMap<string, Run> | undefined;
  now?: Date;
}) {
  if (!capacity.configured || capacity.accounts.length === 0) return <NoPoolConfigured />;
  const { accounts } = capacity;
  const ordered = capacity.strategy === "ordered";
  const nextUp = ordered ? accounts.find((a) => !isTripped(a)) : undefined;

  return (
    <>
      <section className="grid" aria-label="Pool summary">
        <div className="card">
          <p className="stat-label">Available accounts</p>
          <p className="stat-value">
            {capacity.available} of {accounts.length}
          </p>
          <p className="stat-note">
            {capacity.all_tripped_until ? (
              <>
                All limited until <LocalTime ts={capacity.all_tripped_until} /> (
                {formatRelative(capacity.all_tripped_until, now)})
              </>
            ) : nextUp ? (
              <>Next job: {nextUp.id}</>
            ) : (
              "At least one account can take a job"
            )}
          </p>
        </div>
        <div className="card">
          <p className="stat-label">Strategy</p>
          <p className="stat-value">{capacity.strategy === "round_robin" ? "Round robin" : capacity.strategy === "ordered" ? "Ordered" : capacity.strategy}</p>
          <p className="stat-note">{STRATEGY_TEXT[capacity.strategy] ?? ""}</p>
        </div>
        <div className="card">
          <p className="stat-label">When every account is limited</p>
          <p className="stat-value">{capacity.fallback_on_capacity_trip ? "Fallback" : "Pause"}</p>
          <p className="stat-note">
            {capacity.fallback_on_capacity_trip
              ? "Jobs fall back to the metered ANTHROPIC_API_KEY."
              : "Jobs pause until an account recovers."}
          </p>
        </div>
      </section>

      {capacity.all_tripped_until ? (
        <div className="alert warn" role="status">
          <strong>Every account is limited.</strong>{" "}
          {capacity.fallback_on_capacity_trip ? "Jobs use the metered fallback key" : "Jobs are paused"} until{" "}
          <LocalTime ts={capacity.all_tripped_until} /> ({formatRelative(capacity.all_tripped_until, now)}).
        </div>
      ) : null}

      <div className="table-wrap">
        <table className="cell-th">
          <caption className="sr-only">
            Coding-agent accounts{ordered ? ", in the order jobs try them" : ""}. Source:{" "}
            {SOURCE_TEXT[capacity.source] ?? capacity.source}.
          </caption>
          <thead>
            <tr>
              <th scope="col" className="num">
                {ordered ? "Order" : "#"}
              </th>
              <th scope="col">Account</th>
              <th scope="col">Auth</th>
              <th scope="col">Credential name</th>
              <th scope="col">Status</th>
              {lastRuns ? <th scope="col">Last run</th> : null}
            </tr>
          </thead>
          <tbody>
            {accounts.map((a, i) => {
              const last = lastRuns?.get(a.id);
              return (
                <tr key={a.id}>
                  <td className="num">{i + 1}</td>
                  <th scope="row" className="mono">
                    {a.id}
                  </th>
                  <td>{authLabel(a.auth)}</td>
                  <td className="mono">{a.secret || "–"}</td>
                  <td>
                    {isTripped(a) ? (
                      <>
                        <span className="badge warn">Limited</span>{" "}
                        {a.tripped_until ? (
                          <span className="subtle">
                            until <LocalTime ts={a.tripped_until} /> ({formatRelative(a.tripped_until, now)})
                          </span>
                        ) : null}
                      </>
                    ) : (
                      <span className="badge good">Available</span>
                    )}
                  </td>
                  {lastRuns ? (
                    <td>
                      {last ? (
                        <>
                          <time dateTime={last.started_at}>{formatTimestamp(last.started_at)}</time>
                          <span className="subtle">
                            {" "}
                            · {last.role} on {last.project}
                            {last.job_id ? <span className="mono"> ({last.job_id})</span> : null}
                          </span>
                        </>
                      ) : (
                        "–"
                      )}
                    </td>
                  ) : null}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="hint">
        Read-only. A usage-limit failure pauses only the account that ran it, for 60 minutes. Credential values are
        never sent to the dashboard.
      </p>
    </>
  );
}
