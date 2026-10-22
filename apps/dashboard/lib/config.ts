import "server-only";
import { normalizeBaseUrl } from "@squadhq/shared-api";

/** Server-side configuration. None of these values are ever sent to the browser. */

export function isMockMode(): boolean {
  const v = (process.env.MOCK_API ?? "").toLowerCase();
  return v === "1" || v === "true" || v === "yes";
}

function safeNormalize(raw: string): string | null {
  try {
    const n = normalizeBaseUrl(raw);
    return n === "" ? null : n;
  } catch {
    return null;
  }
}

export function defaultApiUrl(): string | null {
  return safeNormalize(process.env.SQUADHQ_API_URL ?? "");
}

/**
 * Server URLs a user may select in Settings. The dashboard fetches server-side,
 * so accepting arbitrary URLs would be an SSRF vector: only SQUADHQ_API_URL and
 * SQUADHQ_API_URL_ALLOWLIST entries are accepted.
 */
export function allowedApiUrls(): string[] {
  const out = new Set<string>();
  const d = defaultApiUrl();
  if (d) out.add(d);
  for (const raw of (process.env.SQUADHQ_API_URL_ALLOWLIST ?? "").split(",")) {
    const n = safeNormalize(raw);
    if (n) out.add(n);
  }
  return [...out];
}

export function isAllowedApiUrl(url: string): boolean {
  const n = safeNormalize(url);
  return n !== null && allowedApiUrls().includes(n);
}

/** The API origin for a request: the user's (allowlisted) choice, else the default. */
export function resolveApiUrl(selected?: string | null): string | null {
  if (selected && isAllowedApiUrl(selected)) return safeNormalize(selected);
  return defaultApiUrl();
}

function envFlag(name: string): boolean {
  const v = (process.env[name] ?? "").trim().toLowerCase();
  return v === "1" || v === "true" || v === "yes";
}

/**
 * Anonymous access: visitors without a session act with SQUADHQ_API_TOKEN.
 * Only safe behind an authenticating reverse proxy, so it needs BOTH the token
 * and the explicit opt-in SQUADHQ_DASHBOARD_ANONYMOUS=true.
 */
export function anonymousAccessEnabled(): boolean {
  return envFlag("SQUADHQ_DASHBOARD_ANONYMOUS") && Boolean(process.env.SQUADHQ_API_TOKEN?.trim());
}

/** The fallback token for session-less requests, or null unless anonymous access is opted into. */
export function fallbackToken(): string | null {
  if (!anonymousAccessEnabled()) return null;
  return process.env.SQUADHQ_API_TOKEN?.trim() || null;
}

/** Startup warnings about risky or ineffective configuration (called from instrumentation.ts). */
export function configWarnings(): string[] {
  const out: string[] = [];
  const hasToken = Boolean(process.env.SQUADHQ_API_TOKEN?.trim());
  if (anonymousAccessEnabled()) {
    out.push(
      "SQUADHQ_DASHBOARD_ANONYMOUS=true: visitors without a session act with SQUADHQ_API_TOKEN's role. " +
        "Only run this behind an authenticating reverse proxy.",
    );
  } else if (hasToken) {
    out.push("SQUADHQ_API_TOKEN is set but ignored; set SQUADHQ_DASHBOARD_ANONYMOUS=true to enable anonymous access.");
  } else if (envFlag("SQUADHQ_DASHBOARD_ANONYMOUS")) {
    out.push("SQUADHQ_DASHBOARD_ANONYMOUS=true has no effect without SQUADHQ_API_TOKEN.");
  }
  if (!isMockMode() && !(process.env.SQUADHQ_SESSION_SECRET ?? "").trim()) {
    out.push("SQUADHQ_SESSION_SECRET is not set; session cookies are not encrypted.");
  }
  return out;
}

export const API_TIMEOUT_MS = 10_000;
