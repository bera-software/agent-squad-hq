"use client";

import { useEffect, useId, useRef, useState } from "react";
import { formatDuration, formatTimestamp, formatUsd, runAccount, type Run } from "@squadhq/shared-api";

function outcomeTone(outcome: string): string {
  if (outcome === "success") return "good";
  if (outcome === "failure") return "bad";
  return "";
}

export function RunsTable({ runs }: { runs: Run[] }) {
  const [selected, setSelected] = useState<Run | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);

  if (runs.length === 0) return <p className="subtle">No runs recorded yet.</p>;
  // The account column appears only when the server attributes runs to pool accounts (data.account).
  const showAccount = runs.some((r) => runAccount(r) !== null);

  return (
    <>
      <div className="table-wrap">
        <table>
          <caption className="sr-only">Agent runs, newest first</caption>
          <thead>
            <tr>
              <th scope="col">Started</th>
              <th scope="col">Role</th>
              <th scope="col">Project</th>
              <th scope="col">Outcome</th>
              {showAccount ? <th scope="col">Account</th> : null}
              <th scope="col" className="num">
                Cost
              </th>
              <th scope="col" className="num">
                Duration
              </th>
              <th scope="col">
                <span className="sr-only">Details</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td>
                  <time dateTime={r.started_at}>{formatTimestamp(r.started_at)}</time>
                </td>
                <td>{r.role}</td>
                <td>{r.project}</td>
                <td>
                  <span className={`badge ${outcomeTone(r.outcome)}`}>{r.outcome}</span>
                </td>
                {showAccount ? <td className="mono">{runAccount(r) ?? "–"}</td> : null}
                <td className="num">{formatUsd(r.cost_usd)}</td>
                <td className="num">{formatDuration(r.duration_s)}</td>
                <td>
                  <button
                    type="button"
                    aria-label={`Details for ${r.role} run on ${r.project}`}
                    onClick={(e) => {
                      triggerRef.current = e.currentTarget;
                      setSelected(r);
                    }}
                  >
                    Details
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <RunDrawer
        run={selected}
        onClose={() => {
          setSelected(null);
          triggerRef.current?.focus();
        }}
      />
    </>
  );
}

export function RunDrawer({ run, onClose }: { run: Run | null; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (run && !d.open) d.showModal();
    if (!run && d.open) d.close();
  }, [run]);

  return (
    <dialog
      ref={ref}
      className="drawer"
      aria-labelledby={titleId}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClose={() => {
        if (run) onClose();
      }}
    >
      {run ? (
        <>
          <div className="page-header">
            <h2 id={titleId}>
              {run.role} · {run.project}
            </h2>
            <button type="button" onClick={onClose} autoFocus>
              Close
            </button>
          </div>
          <dl className="kv">
            <dt>Run id</dt>
            <dd className="mono">{run.id}</dd>
            <dt>Outcome</dt>
            <dd>
              <span className={`badge ${outcomeTone(run.outcome)}`}>{run.outcome}</span>
            </dd>
            <dt>Error class</dt>
            <dd>{run.error_class}</dd>
            <dt>Started</dt>
            <dd>{formatTimestamp(run.started_at)}</dd>
            <dt>Duration</dt>
            <dd>{formatDuration(run.duration_s)}</dd>
            <dt>Cost</dt>
            <dd>{formatUsd(run.cost_usd)}</dd>
            <dt>Model</dt>
            <dd>{run.model || "–"}</dd>
            <dt>Executor</dt>
            <dd>{run.executor || "–"}</dd>
            {runAccount(run) ? (
              <>
                <dt>Account</dt>
                <dd className="mono">{runAccount(run)}</dd>
              </>
            ) : null}
            <dt>Tokens in / out</dt>
            <dd>
              {run.input_tokens.toLocaleString("en-US")} / {run.output_tokens.toLocaleString("en-US")}
            </dd>
            <dt>Job</dt>
            <dd className="mono">{run.job_id ?? "–"}</dd>
          </dl>
          {Object.keys(run.data).length > 0 ? (
            <>
              <h3>Details</h3>
              <pre>{JSON.stringify(run.data, null, 2)}</pre>
            </>
          ) : null}
        </>
      ) : null}
    </dialog>
  );
}
