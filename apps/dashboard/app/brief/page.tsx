import type { Metadata } from "next";
import { formatTimestamp } from "@squadhq/shared-api";
import { ErrorState, NotConfigured, PageHeader } from "@/components/ErrorState";
import { Markdown } from "@/components/Markdown";
import { getConnection, settle } from "@/lib/server-api";

export const metadata: Metadata = { title: "Daily brief" };
export const dynamic = "force-dynamic";

export default async function BriefPage() {
  const conn = await getConnection();
  if (conn.mode === "unconfigured") {
    return (
      <>
        <PageHeader title="Daily brief" />
        <NotConfigured reason={conn.reason} />
      </>
    );
  }
  const brief = await settle(conn.api.latestBrief());
  return (
    <>
      <PageHeader title="Daily brief">
        {brief.ok && brief.value ? (
          <p className="subtle">
            Generated <time dateTime={brief.value.created_at}>{formatTimestamp(brief.value.created_at)}</time>
          </p>
        ) : null}
      </PageHeader>
      {!brief.ok ? (
        <ErrorState error={brief.error} what="the brief" />
      ) : brief.value === null ? (
        <p className="subtle">No brief has been published yet.</p>
      ) : (
        <article className="card" aria-label="Latest daily brief">
          <Markdown source={brief.value.markdown} />
        </article>
      )}
    </>
  );
}
