import type { Metadata } from "next";
import { lastRunByAccount } from "@squadhq/shared-api";
import { AccountsPanel } from "@/components/AccountsPanel";
import { ErrorState, NotConfigured, PageHeader } from "@/components/ErrorState";
import { getConnection, settle } from "@/lib/server-api";

export const metadata: Metadata = { title: "Accounts" };
export const dynamic = "force-dynamic";

export default async function AccountsPage() {
  const conn = await getConnection();
  if (conn.mode === "unconfigured") {
    return (
      <>
        <PageHeader title="Accounts" />
        <NotConfigured reason={conn.reason} />
      </>
    );
  }
  const [capacity, runs] = await Promise.all([settle(conn.api.capacity()), settle(conn.api.runs({ limit: 500 }))]);
  // "Last run" only when the server attributes runs to accounts (worker mode sets data.account).
  const lastRuns = runs.ok ? lastRunByAccount(runs.value) : undefined;
  return (
    <>
      <PageHeader title="Accounts">
        <p className="subtle">Coding-agent account pool</p>
      </PageHeader>
      {capacity.ok ? (
        <AccountsPanel capacity={capacity.value} lastRuns={lastRuns && lastRuns.size > 0 ? lastRuns : undefined} />
      ) : (
        <ErrorState error={capacity.error} what="accounts" />
      )}
    </>
  );
}
