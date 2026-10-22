import Link from "next/link";
import { currentMonth, formatUsd, isSameUtcDay, wipByProject } from "@squadhq/shared-api";
import { ErrorState, NotConfigured, PageHeader } from "@/components/ErrorState";
import { getConnection, settle } from "@/lib/server-api";

export const dynamic = "force-dynamic";

export default async function OverviewPage() {
  const conn = await getConnection();
  if (conn.mode === "unconfigured") {
    return (
      <>
        <PageHeader title="Overview" />
        <NotConfigured reason={conn.reason} />
      </>
    );
  }
  const { api } = conn;
  const month = currentMonth();
  const [projects, tickets, runs, costs, approvals] = await Promise.all([
    settle(api.projects()),
    settle(api.tickets({ state: "open" })),
    settle(api.runs({ limit: 500 })),
    settle(api.costs(month)),
    settle(api.approvals({ status: "pending" })),
  ]);

  const wip = tickets.ok ? wipByProject(tickets.value) : {};
  const projectNames = projects.ok
    ? projects.value.map((p) => p.name)
    : Object.keys(wip);
  const today = runs.ok ? runs.value.filter((r) => isSameUtcDay(r.started_at)) : [];
  const failedToday = today.filter((r) => r.outcome === "failure").length;
  const escalations = approvals.ok ? approvals.value.filter((a) => a.kind === "escalation") : [];
  const budget = costs.ok ? (costs.value.budget_usd ?? null) : null;
  const spent = costs.ok ? costs.value.total_usd : 0;
  const pct = budget ? Math.min(100, Math.round((spent / budget) * 100)) : null;

  return (
    <>
      <PageHeader title="Overview">
        <p className="subtle">{conn.mode === "mock" ? "Sample data" : conn.apiUrl}</p>
      </PageHeader>

      <section className="grid" aria-label="Key figures">
        <div className="card">
          <p className="stat-label">Runs today (UTC)</p>
          {runs.ok ? (
            <>
              <p className="stat-value">{today.length}</p>
              <p className="stat-note">
                {failedToday} failed · {formatUsd(today.reduce((s, r) => s + r.cost_usd, 0))} spent
              </p>
            </>
          ) : (
            <ErrorState error={runs.error} what="runs" />
          )}
        </div>
        <div className="card">
          <p className="stat-label">Spend this month ({month})</p>
          {costs.ok ? (
            <>
              <p className="stat-value">{formatUsd(spent)}</p>
              <p className="stat-note">
                {budget ? `of ${formatUsd(budget)} budget (${pct}%)` : "No budget reported by the server"}
              </p>
              {pct !== null ? (
                <div
                  className={`meter${spent > (budget ?? Infinity) ? " over" : ""}`}
                  role="meter"
                  aria-label="Budget used"
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={pct}
                >
                  <span style={{ width: `${pct}%` }} />
                </div>
              ) : null}
            </>
          ) : (
            <ErrorState error={costs.error} what="costs" />
          )}
        </div>
        <div className="card">
          <p className="stat-label">Awaiting approval</p>
          {approvals.ok ? (
            <>
              <p className="stat-value">{approvals.value.length}</p>
              <p className="stat-note">
                <Link href="/approvals">Open the inbox</Link>
              </p>
            </>
          ) : (
            <ErrorState error={approvals.error} what="approvals" />
          )}
        </div>
        <div className="card">
          <p className="stat-label">Open escalations</p>
          {approvals.ok ? (
            <>
              <p className="stat-value">{escalations.length}</p>
              <p className="stat-note">{escalations.length ? "Needs a human" : "All clear"}</p>
            </>
          ) : (
            <p className="stat-note">Unavailable</p>
          )}
        </div>
      </section>

      <div className="grid-2">
        <section className="card" aria-labelledby="wip-h">
          <h2 id="wip-h">Work in progress by project</h2>
          {tickets.ok ? (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Project</th>
                    <th scope="col" className="num">
                      WIP
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {projectNames.length === 0 ? (
                    <tr>
                      <td colSpan={2}>No projects registered.</td>
                    </tr>
                  ) : (
                    projectNames.map((name) => (
                      <tr key={name}>
                        <td>
                          <Link href={`/pipeline?project=${encodeURIComponent(name)}`}>{name}</Link>
                        </td>
                        <td className="num">{wip[name] ?? 0}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          ) : (
            <ErrorState error={tickets.error} what="tickets" />
          )}
        </section>
        <section className="card" aria-labelledby="esc-h">
          <h2 id="esc-h">Escalations</h2>
          {escalations.length === 0 ? (
            <p className="subtle">No open escalations.</p>
          ) : (
            <ul>
              {escalations.map((e) => (
                <li key={e.id}>
                  <Link href="/approvals?kind=escalation">{e.title || e.subject}</Link>{" "}
                  <span className="subtle">({e.project})</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </>
  );
}
