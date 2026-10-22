import type { Metadata } from "next";
import Link from "next/link";
import { ApiError, formatTimestamp, shortHash, type AuditEvent, type AuditVerify } from "@squadhq/shared-api";
import { ErrorState, NotConfigured, PageHeader } from "@/components/ErrorState";
import { pageLinks } from "@/lib/audit";
import { getConnection, settle } from "@/lib/server-api";

export const metadata: Metadata = { title: "Audit log" };
export const dynamic = "force-dynamic";

const PAGE_SIZE = 25;

type Props = { searchParams: Promise<{ since?: string }> };

function VerifyBanner({ result }: { result: { ok: true; value: AuditVerify } | { ok: false; error: unknown } }) {
  if (!result.ok) {
    const forbidden = result.error instanceof ApiError && result.error.status === 403;
    return (
      <div className="alert warn" role="status">
        <strong>Chain verification unavailable.</strong>{" "}
        {forbidden ? "Verifying the full chain needs an admin token." : "The verify endpoint did not respond."}
      </div>
    );
  }
  const v = result.value;
  return v.ok ? (
    <div className="alert good" role="status">
      <strong>Hash chain verified.</strong> {v.checked.toLocaleString("en-US")} events checked; head{" "}
      <span className="mono">{shortHash(v.head_hash, 16)}</span>.
    </div>
  ) : (
    <div className="alert bad" role="alert">
      <strong>Hash chain broken</strong> at event #{v.first_bad_seq ?? "?"}: {v.reason || "mismatch"}. Treat the log
      after this point as untrusted and investigate.
    </div>
  );
}

export default async function AuditPage({ searchParams }: Props) {
  const { since: rawSince } = await searchParams;
  const since = rawSince && /^\d{1,12}$/.test(rawSince) ? Number(rawSince) : 0;
  const conn = await getConnection();
  if (conn.mode === "unconfigured") {
    return (
      <>
        <PageHeader title="Audit log" />
        <NotConfigured reason={conn.reason} />
      </>
    );
  }
  const [page, verify] = await Promise.all([
    settle(conn.api.audit({ since, limit: PAGE_SIZE })),
    settle(conn.api.auditVerify()),
  ]);

  return (
    <>
      <PageHeader title="Audit log">
        <p className="subtle">Append-only, hash-chained record of every decision and action.</p>
      </PageHeader>
      <VerifyBanner result={verify} />
      {!page.ok ? (
        <ErrorState error={page.error} what="the audit log" />
      ) : (
        <AuditTable events={page.value.events} since={since} nextSince={page.value.next_since} />
      )}
    </>
  );
}

function AuditTable({ events, since, nextSince }: { events: AuditEvent[]; since: number; nextSince: number }) {
  const links = pageLinks(events);
  const prevSince = Math.max(0, since - PAGE_SIZE);
  return (
    <>
      <div className="table-wrap">
        <table>
          <caption className="sr-only">Audit events after sequence {since}</caption>
          <thead>
            <tr>
              <th scope="col" className="num">
                #
              </th>
              <th scope="col">Time</th>
              <th scope="col">Actor</th>
              <th scope="col">Action</th>
              <th scope="col">Subject</th>
              <th scope="col">Hash</th>
              <th scope="col">Link</th>
            </tr>
          </thead>
          <tbody>
            {events.length === 0 ? (
              <tr>
                <td colSpan={7}>No events.</td>
              </tr>
            ) : (
              events.map((e, i) => (
                <tr key={e.id}>
                  <td className="num">{e.seq}</td>
                  <td>
                    <time dateTime={e.ts}>{formatTimestamp(e.ts)}</time>
                  </td>
                  <td>{e.actor}</td>
                  <td className="mono">{e.action}</td>
                  <td className="mono">{e.subject}</td>
                  <td className="mono" title={e.hash}>
                    {shortHash(e.hash)}
                  </td>
                  <td>
                    {links[i] ? (
                      <span className="badge good">ok</span>
                    ) : (
                      <span className="badge bad">broken</span>
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      <nav className="pagination" aria-label="Audit log pages">
        {since > 0 ? (
          <Link className="button" href={prevSince ? `/audit?since=${prevSince}` : "/audit"}>
            Previous
          </Link>
        ) : null}
        {events.length === PAGE_SIZE ? (
          <Link className="button" href={`/audit?since=${nextSince}`}>
            Next
          </Link>
        ) : null}
        <span className="subtle">
          {events.length ? `Events ${events[0]?.seq}–${events[events.length - 1]?.seq}` : ""}
        </span>
      </nav>
    </>
  );
}
