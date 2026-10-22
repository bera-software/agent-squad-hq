"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { formatTimestamp, type Approval, type ApiClient } from "@squadhq/shared-api";
import { createBrowserApi } from "@/lib/api";
import { describeError } from "./ErrorState";

type Decision = "approve" | "reject";
type Pending = { approval: Approval; decision: Decision } | null;
type Notice = { tone: "good" | "bad"; text: string } | null;

const KIND_LABEL: Record<string, string> = {
  merge: "Merge",
  proposal: "Proposal",
  deploy: "Deploy",
  escalation: "Escalation",
};

export interface ApprovalsInboxProps {
  initial: Approval[];
  initialKind?: string;
  canDecide?: boolean;
  /** Injected in tests; defaults to the same-origin proxy client. */
  api?: Pick<ApiClient, "decide">;
}

export function ApprovalsInbox({ initial, initialKind = "", canDecide = true, api }: ApprovalsInboxProps) {
  const client = useMemo(() => api ?? createBrowserApi(), [api]);
  const [items, setItems] = useState<Approval[]>(initial);
  const [kind, setKind] = useState(initialKind);
  const [pending, setPending] = useState<Pending>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());

  const kinds = useMemo(() => [...new Set(initial.map((a) => a.kind))].sort(), [initial]);
  const visible = kind ? items.filter((a) => a.kind === kind) : items;

  const submit = useCallback(
    async (approval: Approval, decision: Decision, comment: string) => {
      const index = items.findIndex((a) => a.id === approval.id);
      // Optimistic: remove the item now, put it back if the server refuses.
      setItems((prev) => prev.filter((a) => a.id !== approval.id));
      setBusy((prev) => new Set(prev).add(approval.id));
      setNotice(null);
      try {
        await client.decide(approval.id, { decision, comment });
        setNotice({
          tone: "good",
          text: `${decision === "approve" ? "Approved" : "Rejected"}: ${approval.title || approval.subject}`,
        });
      } catch (e) {
        setItems((prev) => {
          if (prev.some((a) => a.id === approval.id)) return prev;
          const next = [...prev];
          next.splice(Math.min(Math.max(index, 0), next.length), 0, approval);
          return next;
        });
        const d = describeError(e);
        setNotice({ tone: "bad", text: `Could not ${decision} "${approval.title || approval.subject}": ${d.title}. ${d.detail}` });
      } finally {
        setBusy((prev) => {
          const next = new Set(prev);
          next.delete(approval.id);
          return next;
        });
      }
    },
    [client, items],
  );

  return (
    <>
      <div aria-live="polite" role="status">
        {notice ? <div className={`alert ${notice.tone}`}>{notice.text}</div> : null}
      </div>

      {kinds.length > 1 ? (
        <div className="filters" role="group" aria-label="Filter by kind">
          <button type="button" aria-pressed={kind === ""} onClick={() => setKind("")}>
            All ({items.length})
          </button>
          {kinds.map((k) => (
            <button key={k} type="button" aria-pressed={kind === k} onClick={() => setKind(k)}>
              {KIND_LABEL[k] ?? k} ({items.filter((a) => a.kind === k).length})
            </button>
          ))}
        </div>
      ) : null}

      {visible.length === 0 ? (
        <p className="subtle">Nothing is waiting for you.</p>
      ) : (
        <ul className="approval-list" aria-label="Pending approvals">
          {visible.map((a) => (
            <li key={a.id} className="card approval">
              <article aria-labelledby={`apv-${a.id}`}>
                <header>
                  <span className={`badge${a.kind === "escalation" ? " bad" : a.kind === "deploy" ? " warn" : ""}`}>
                    {KIND_LABEL[a.kind] ?? a.kind}
                  </span>
                  <h2 id={`apv-${a.id}`}>{a.title || a.subject}</h2>
                </header>
                <dl>
                  <dt>Project</dt>
                  <dd>{a.project || "–"}</dd>
                  <dt>Subject</dt>
                  <dd className="mono">{a.subject}</dd>
                  <dt>Requested</dt>
                  <dd>
                    <time dateTime={a.created_at}>{formatTimestamp(a.created_at)}</time>
                  </dd>
                  {Object.entries(a.data)
                    .filter(([, v]) => typeof v === "string" || typeof v === "number" || typeof v === "boolean")
                    .slice(0, 6)
                    .map(([k, v]) => (
                      <DataRow key={k} k={k} v={String(v)} />
                    ))}
                </dl>
                <div className="actions">
                  {a.url ? (
                    <a
                      className="button"
                      href={a.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      aria-label={`View ${a.subject} (opens in a new tab)`}
                    >
                      View
                    </a>
                  ) : null}
                  {canDecide ? (
                    <>
                      <button
                        type="button"
                        className="primary"
                        disabled={busy.has(a.id)}
                        onClick={() => setPending({ approval: a, decision: "approve" })}
                        aria-label={`${a.kind === "escalation" ? "Acknowledge" : "Approve"} ${a.title || a.subject}`}
                      >
                        {a.kind === "escalation" ? "Acknowledge" : "Approve"}
                      </button>
                      <button
                        type="button"
                        className="danger"
                        disabled={busy.has(a.id)}
                        onClick={() => setPending({ approval: a, decision: "reject" })}
                        aria-label={`Reject ${a.title || a.subject}`}
                      >
                        Reject
                      </button>
                    </>
                  ) : null}
                </div>
              </article>
            </li>
          ))}
        </ul>
      )}

      <ConfirmDialog
        pending={pending}
        onCancel={() => setPending(null)}
        onConfirm={(comment) => {
          const p = pending;
          setPending(null);
          if (p) void submit(p.approval, p.decision, comment);
        }}
      />
    </>
  );
}

function DataRow({ k, v }: { k: string; v: string }) {
  return (
    <>
      <dt>{k.replace(/_/g, " ")}</dt>
      <dd>{v}</dd>
    </>
  );
}

function ConfirmDialog({
  pending,
  onCancel,
  onConfirm,
}: {
  pending: Pending;
  onCancel: () => void;
  onConfirm: (comment: string) => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [comment, setComment] = useState("");
  const [error, setError] = useState("");
  const titleId = useId();
  const commentId = useId();
  const errorId = useId();

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (pending && !d.open) {
      setComment("");
      setError("");
      d.showModal();
    } else if (!pending && d.open) {
      d.close();
    }
  }, [pending]);

  const isReject = pending?.decision === "reject";
  const verb = isReject ? "Reject" : pending?.approval.kind === "escalation" ? "Acknowledge" : "Approve";

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      onClose={() => {
        if (pending) onCancel();
      }}
      onCancel={(e) => {
        e.preventDefault();
        onCancel();
      }}
    >
      {pending ? (
        <form
          method="dialog"
          onSubmit={(e) => {
            e.preventDefault();
            if (isReject && !comment.trim()) {
              setError("Please say why you are rejecting this.");
              return;
            }
            onConfirm(comment.trim());
          }}
        >
          <h2 id={titleId}>
            {verb} “{pending.approval.title || pending.approval.subject}”?
          </h2>
          <p className="subtle">
            This is recorded in the audit log as your decision
            {pending.approval.kind === "merge" && !isReject ? " and the pull request will be merged" : ""}
            {pending.approval.kind === "deploy" && !isReject ? " and the deployment will start" : ""}.
          </p>
          <div className="field">
            <label htmlFor={commentId}>Comment{isReject ? " (required)" : " (optional)"}</label>
            <textarea
              id={commentId}
              rows={3}
              maxLength={4000}
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? errorId : undefined}
              autoFocus
            />
            {error ? (
              <p id={errorId} className="hint" role="alert" style={{ color: "var(--bad)" }}>
                {error}
              </p>
            ) : null}
          </div>
          <div className="dialog-actions">
            <button type="button" onClick={onCancel}>
              Cancel
            </button>
            <button type="submit" className={isReject ? "danger" : "primary"}>
              {verb}
            </button>
          </div>
        </form>
      ) : null}
    </dialog>
  );
}
