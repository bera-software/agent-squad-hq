"use client";

import { useEffect, useState } from "react";
import { formatTimestamp } from "@squadhq/shared-api";

function localLabel(d: Date, now: Date): string {
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === now.toDateString()
    ? time
    : `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${time}`;
}

/**
 * A timestamp in the viewer's local time zone. The server render (and the
 * no-JS fallback) shows UTC; the browser swaps in local time after hydration,
 * so server and client markup match.
 */
export function LocalTime({ ts }: { ts: string }) {
  const [label, setLabel] = useState(() => formatTimestamp(ts));
  useEffect(() => {
    const d = new Date(ts);
    if (!Number.isNaN(d.getTime())) setLabel(localLabel(d, new Date()));
  }, [ts]);
  return (
    <time dateTime={ts} title={formatTimestamp(ts)}>
      {label}
    </time>
  );
}
