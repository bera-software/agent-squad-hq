import type { Metadata } from "next";
import { ErrorState, NotConfigured, PageHeader } from "@/components/ErrorState";
import { ProjectFilter } from "@/components/ProjectFilter";
import { RunsTable } from "@/components/RunsTable";
import { getConnection, settle } from "@/lib/server-api";

export const metadata: Metadata = { title: "Runs" };
export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ project?: string }> };

export default async function RunsPage({ searchParams }: Props) {
  const { project } = await searchParams;
  const conn = await getConnection();
  if (conn.mode === "unconfigured") {
    return (
      <>
        <PageHeader title="Runs" />
        <NotConfigured reason={conn.reason} />
      </>
    );
  }
  const [projects, runs] = await Promise.all([
    settle(conn.api.projects()),
    settle(conn.api.runs({ limit: 200, ...(project ? { project } : {}) })),
  ]);
  return (
    <>
      <PageHeader title="Runs">
        <ProjectFilter projects={projects.ok ? projects.value.map((p) => p.name) : []} selected={project ?? ""} />
      </PageHeader>
      {runs.ok ? <RunsTable runs={runs.value} /> : <ErrorState error={runs.error} what="runs" />}
    </>
  );
}
