import type { Metadata } from "next";
import { BOARD_COLUMNS, OTHER_COLUMN, flowModifiers, groupByColumn, type Ticket } from "@squadhq/shared-api";
import { ErrorState, NotConfigured, PageHeader } from "@/components/ErrorState";
import { ProjectFilter } from "@/components/ProjectFilter";
import { getConnection, settle } from "@/lib/server-api";

export const metadata: Metadata = { title: "Pipeline" };
export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ project?: string }> };

export default async function PipelinePage({ searchParams }: Props) {
  const { project } = await searchParams;
  const conn = await getConnection();
  if (conn.mode === "unconfigured") {
    return (
      <>
        <PageHeader title="Pipeline" />
        <NotConfigured reason={conn.reason} />
      </>
    );
  }
  const [projects, tickets] = await Promise.all([
    settle(conn.api.projects()),
    settle(conn.api.tickets(project ? { project } : {})),
  ]);
  const grouped = tickets.ok ? groupByColumn(tickets.value) : new Map<string, Ticket[]>();
  const columns = [...BOARD_COLUMNS, ...((grouped.get("other")?.length ?? 0) > 0 ? [OTHER_COLUMN] : [])];

  return (
    <>
      <PageHeader title="Pipeline">
        <ProjectFilter projects={projects.ok ? projects.value.map((p) => p.name) : []} selected={project ?? ""} />
      </PageHeader>
      {!tickets.ok ? (
        <ErrorState error={tickets.error} what="tickets" />
      ) : (
        <div className="board" role="region" aria-label="Tickets by flow state" tabIndex={0}>
          {columns.map((col) => {
            const items = grouped.get(col.id) ?? [];
            return (
              <section key={col.id} className="column" aria-labelledby={`col-${col.id}`}>
                <h2 id={`col-${col.id}`}>
                  <span>{col.label}</span>
                  <span className="badge" aria-label={`${items.length} tickets`}>
                    {items.length}
                  </span>
                </h2>
                {items.length === 0 ? (
                  <p className="subtle">Empty</p>
                ) : (
                  <ul>
                    {items.map((t) => (
                      <li key={`${t.project}:${t.key}`} className="ticket">
                        <div className="subtle mono">
                          {t.project} {t.key}
                        </div>
                        <p>
                          {t.url ? (
                            <a href={t.url} rel="noopener noreferrer" target="_blank">
                              {t.title || t.key}
                              <span className="sr-only"> (opens in a new tab)</span>
                            </a>
                          ) : (
                            t.title || t.key
                          )}
                        </p>
                        <div className="labels">
                          {flowModifiers(t.flow_state).map((m) => (
                            <span key={m} className="badge warn">
                              {m}
                            </span>
                          ))}
                          {t.labels.slice(0, 4).map((l) => (
                            <span key={l} className="badge">
                              {l}
                            </span>
                          ))}
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            );
          })}
        </div>
      )}
    </>
  );
}
