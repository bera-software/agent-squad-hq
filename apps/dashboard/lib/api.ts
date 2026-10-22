/**
 * Typed API client for the dashboard.
 *
 * Schemas and the client implementation live in @squadhq/shared-api (zod
 * runtime validation of every response), shared with the mobile app. Browser
 * code talks only to the same-origin proxy at /api/proxy, which attaches the
 * bearer token server-side; the browser never sees the token.
 */
import { createApiClient, type ApiClient } from "@squadhq/shared-api";

export * from "@squadhq/shared-api";

export const PROXY_PREFIX = "/api/proxy";

export function createBrowserApi(fetchImpl?: typeof fetch): ApiClient {
  return createApiClient({
    baseUrl: "",
    apiPrefix: PROXY_PREFIX,
    ...(fetchImpl ? { fetch: (input: string, init?: RequestInit) => fetchImpl(input, init) } : {}),
  });
}
