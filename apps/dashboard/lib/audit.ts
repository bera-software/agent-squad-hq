import type { AuditEvent } from "@squadhq/shared-api";

/**
 * Local link check for one page of events: each event's prev_hash must equal
 * the previous event's hash and sequence numbers must be contiguous. The full
 * cryptographic check is the server's /audit/verify.
 */
export function pageLinks(events: readonly AuditEvent[]): boolean[] {
  return events.map((e, i) => {
    const prev = events[i - 1];
    return prev ? e.prev_hash === prev.hash && e.seq === prev.seq + 1 : true;
  });
}
