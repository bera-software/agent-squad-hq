import { NextResponse, type NextRequest } from "next/server";
import { cookies } from "next/headers";
import { API_TIMEOUT_MS, fallbackToken, isMockMode, resolveApiUrl } from "@/lib/config";
import { mockFetch } from "@/lib/mock";
import { isProxyAllowed, MAX_PROXY_BODY_BYTES } from "@/lib/proxy-policy";
import { API_URL_COOKIE, isSameOrigin, TOKEN_COOKIE, unsealToken } from "@/lib/session";

export const dynamic = "force-dynamic";

type Ctx = { params: Promise<{ path: string[] }> };

function problem(status: number, detail: string): NextResponse {
  return NextResponse.json({ detail }, { status, headers: { "Cache-Control": "no-store" } });
}

async function forward(req: NextRequest, ctx: Ctx): Promise<NextResponse> {
  const { path: parts } = await ctx.params;
  const path = parts.map((p) => encodeURIComponent(p)).join("/");
  const method = req.method.toUpperCase();
  if (!isProxyAllowed(method, path)) return problem(404, "not available through the dashboard proxy");
  if (method !== "GET" && !isSameOrigin(req)) return problem(403, "cross-origin request refused");

  let body: string | undefined;
  if (method === "POST") {
    body = await req.text();
    if (new TextEncoder().encode(body).length > MAX_PROXY_BODY_BYTES) return problem(413, "request body too large");
  }
  const search = req.nextUrl.search;
  const init: RequestInit = {
    method,
    headers: { Accept: "application/json", ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
    body,
    cache: "no-store",
  };

  let upstream: Response;
  try {
    if (isMockMode()) {
      upstream = await mockFetch()(`/api/v1/${path}${search}`, init);
    } else {
      const jar = await cookies();
      const apiUrl = resolveApiUrl(jar.get(API_URL_COOKIE)?.value);
      const token = unsealToken(jar.get(TOKEN_COOKIE)?.value) ?? fallbackToken();
      if (!apiUrl) return problem(503, "SQUADHQ_API_URL is not configured");
      if (!token) return problem(401, "not signed in");
      upstream = await fetch(`${apiUrl}/api/v1/${path}${search}`, {
        ...init,
        headers: { ...(init.headers as Record<string, string>), Authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(API_TIMEOUT_MS),
        redirect: "manual",
      });
    }
  } catch {
    return problem(502, "the Agent Squad HQ server is unreachable");
  }

  const text = await upstream.text();
  const contentType = upstream.headers.get("content-type") ?? "application/json";
  if (!contentType.includes("json")) return problem(502, "unexpected upstream response");
  return new NextResponse(text, {
    status: upstream.status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

export const GET = forward;
export const POST = forward;
