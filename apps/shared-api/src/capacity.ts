/** Helpers for the coding-agent account pool (GET /api/v1/capacity) and run attribution. */
import type { AccountStatus, Run } from "./schemas";

/** The pool account a run used (`data.account`, set by the server worker), or null. */
export function runAccount(run: Pick<Run, "data">): string | null {
  const a = run.data?.account;
  return typeof a === "string" && a !== "" ? a : null;
}

/** Most recent run per account id (runs in any order). */
export function lastRunByAccount(runs: readonly Run[]): Map<string, Run> {
  const out = new Map<string, Run>();
  for (const r of runs) {
    const id = runAccount(r);
    if (!id) continue;
    const cur = out.get(id);
    if (!cur || Date.parse(r.started_at) > Date.parse(cur.started_at)) out.set(id, r);
  }
  return out;
}

export function isTripped(a: Pick<AccountStatus, "state">): boolean {
  return a.state === "tripped";
}

/**
 * Relative time from `now` to `ts`: "in 38 min", "in 1 h 5 min", "12 min ago", "now".
 * Returns "" for an unparseable timestamp.
 */
export function formatRelative(ts: string, now: Date = new Date()): string {
  const t = Date.parse(ts);
  if (Number.isNaN(t)) return "";
  const diffMin = Math.round((t - now.getTime()) / 60_000);
  const abs = Math.abs(diffMin);
  if (abs < 1) return "now";
  const h = Math.floor(abs / 60);
  const m = abs % 60;
  const span = h ? (m ? `${h} h ${m} min` : `${h} h`) : `${m} min`;
  return diffMin > 0 ? `in ${span}` : `${span} ago`;
}
