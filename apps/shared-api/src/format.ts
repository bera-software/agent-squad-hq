/** Small, locale-stable formatting helpers shared by both apps. */

export function formatUsd(amount: number): string {
  const abs = Math.abs(amount);
  const digits = abs > 0 && abs < 1 ? 3 : 2;
  return `${amount < 0 ? "-" : ""}$${abs.toFixed(digits)}`;
}

export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "–";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

/** `2026-10-21T08:30:00.000000Z` -> `2026-10-21 08:30 UTC`. */
export function formatTimestamp(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

export function currentMonth(now: Date = new Date()): string {
  return now.toISOString().slice(0, 7);
}

export function isSameUtcDay(ts: string, now: Date = new Date()): boolean {
  const d = new Date(ts);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === now.toISOString().slice(0, 10);
}

export function shortHash(hash: string, n = 10): string {
  return hash.length > n ? `${hash.slice(0, n)}…` : hash;
}
