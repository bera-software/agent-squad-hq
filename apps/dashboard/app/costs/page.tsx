import type { Metadata } from "next";
import { currentMonth, formatUsd } from "@squadhq/shared-api";
import { BarChart, type Bar } from "@/components/BarChart";
import { ErrorState, NotConfigured, PageHeader } from "@/components/ErrorState";
import { getConnection, settle } from "@/lib/server-api";

export const metadata: Metadata = { title: "Costs" };
export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ month?: string }> };

function toBars(map: Record<string, number>, targets?: Record<string, number>): Bar[] {
  return Object.entries(map).map(([label, value]) => ({ label, value, target: targets?.[label] }));
}

export default async function CostsPage({ searchParams }: Props) {
  const params = await searchParams;
  const month = params.month && /^\d{4}-(0[1-9]|1[0-2])$/.test(params.month) ? params.month : currentMonth();
  const conn = await getConnection();
  if (conn.mode === "unconfigured") {
    return (
      <>
        <PageHeader title="Costs" />
        <NotConfigured reason={conn.reason} />
      </>
    );
  }
  const costs = await settle(conn.api.costs(month));
  return (
    <>
      <PageHeader title="Costs">
        <form className="toolbar" method="get" aria-label="Choose month">
          <div className="field">
            <label htmlFor="month">Month</label>
            <input id="month" name="month" type="month" defaultValue={month} max={currentMonth()} required />
          </div>
          <button type="submit">Show</button>
        </form>
      </PageHeader>
      {!params.month || params.month === month ? null : (
        <div className="alert warn" role="status">
          Ignored invalid month “{params.month}”; showing {month}.
        </div>
      )}
      {costs.ok ? (
        <>
          <section className="grid" aria-label="Totals">
            <div className="card">
              <p className="stat-label">Total for {costs.value.month}</p>
              <p className="stat-value">{formatUsd(costs.value.total_usd)}</p>
              <p className="stat-note">{costs.value.runs} runs</p>
            </div>
            <div className="card">
              <p className="stat-label">Monthly budget</p>
              <p className="stat-value">
                {costs.value.budget_usd != null ? formatUsd(costs.value.budget_usd) : "–"}
              </p>
              <p className="stat-note">
                {costs.value.budget_usd != null
                  ? `${Math.round((costs.value.total_usd / costs.value.budget_usd) * 100)}% used`
                  : "The server does not report a budget yet"}
              </p>
            </div>
          </section>
          <div className="grid-2">
            <BarChart title="By project" bars={toBars(costs.value.by_project, costs.value.budget_by_project)} format={formatUsd} />
            <BarChart title="By role" bars={toBars(costs.value.by_role)} format={formatUsd} />
            <BarChart title="By model" bars={toBars(costs.value.by_model)} format={formatUsd} />
          </div>
        </>
      ) : (
        <ErrorState error={costs.error} what="costs" />
      )}
    </>
  );
}
