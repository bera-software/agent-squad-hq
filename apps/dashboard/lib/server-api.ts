import "server-only";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { createApiClient, type ApiClient } from "@squadhq/shared-api";
import { API_TIMEOUT_MS, fallbackToken, isMockMode, resolveApiUrl } from "./config";
import { mockFetch } from "./mock";
import { API_URL_COOKIE, TOKEN_COOKIE, unsealToken } from "./session";

export type Connection =
  | { mode: "mock"; api: ApiClient; apiUrl: null }
  | { mode: "live"; api: ApiClient; apiUrl: string }
  | { mode: "unconfigured"; reason: "no_api_url" | "no_token"; apiUrl: string | null };

/**
 * Server-side API access for Server Components.
 * Without a session (and without the anonymous opt-in) pages are sent to the
 * sign-in page; pass `{ redirectToSignIn: false }` on the sign-in page itself.
 */
export async function getConnection(opts: { redirectToSignIn?: boolean } = {}): Promise<Connection> {
  if (isMockMode()) {
    return { mode: "mock", api: createApiClient({ baseUrl: "", fetch: mockFetch() }), apiUrl: null };
  }
  const jar = await cookies();
  const apiUrl = resolveApiUrl(jar.get(API_URL_COOKIE)?.value);
  if (!apiUrl) return { mode: "unconfigured", reason: "no_api_url", apiUrl: null };
  const token = unsealToken(jar.get(TOKEN_COOKIE)?.value) ?? fallbackToken();
  if (!token) {
    if (opts.redirectToSignIn ?? true) redirect("/settings?signin=required");
    return { mode: "unconfigured", reason: "no_token", apiUrl };
  }
  return { mode: "live", apiUrl, api: createApiClient({ baseUrl: apiUrl, token, timeoutMs: API_TIMEOUT_MS }) };
}

/** Settle a promise into a discriminated result so one failing panel does not blank the page. */
export async function settle<T>(p: Promise<T>): Promise<{ ok: true; value: T } | { ok: false; error: unknown }> {
  try {
    return { ok: true, value: await p };
  } catch (error) {
    return { ok: false, error };
  }
}
