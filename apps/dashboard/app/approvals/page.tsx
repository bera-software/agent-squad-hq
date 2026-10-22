import type { Metadata } from "next";
import { ApprovalsInbox } from "@/components/ApprovalsInbox";
import { ErrorState, NotConfigured, PageHeader } from "@/components/ErrorState";
import { getConnection, settle } from "@/lib/server-api";

export const metadata: Metadata = { title: "Approvals" };
export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ kind?: string }> };

export default async function ApprovalsPage({ searchParams }: Props) {
  const { kind } = await searchParams;
  const conn = await getConnection();
  if (conn.mode === "unconfigured") {
    return (
      <>
        <PageHeader title="Approvals" />
        <NotConfigured reason={conn.reason} />
      </>
    );
  }
  const [approvals, who] = await Promise.all([settle(conn.api.approvals({ status: "pending" })), settle(conn.api.whoami())]);
  const canDecide = who.ok ? who.value.role === "approver" || who.value.role === "admin" : true;
  return (
    <>
      <PageHeader title="Approvals">
        <p className="subtle">Merges, proposals, deploys and escalations waiting for a human.</p>
      </PageHeader>
      {!canDecide ? (
        <div className="alert warn" role="status">
          Your token has the viewer role; you can see approvals but not decide them.
        </div>
      ) : null}
      {approvals.ok ? (
        <ApprovalsInbox initial={approvals.value} initialKind={kind ?? ""} canDecide={canDecide} />
      ) : (
        <ErrorState error={approvals.error} what="approvals" />
      )}
    </>
  );
}
