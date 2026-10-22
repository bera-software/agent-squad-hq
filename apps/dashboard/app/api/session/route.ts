import { NextResponse, type NextRequest } from "next/server";
import { z } from "zod";
import { ApiError, createApiClient } from "@squadhq/shared-api";
import { API_TIMEOUT_MS, isAllowedApiUrl, isMockMode, resolveApiUrl } from "@/lib/config";
import { API_URL_COOKIE, cookieOptions, isSameOrigin, sealToken, TOKEN_COOKIE } from "@/lib/session";

export const dynamic = "force-dynamic";

const SessionIn = z.object({
  apiUrl: z.string().max(2048).optional(),
  token: z.string().min(8).max(4096),
});

function isSecure(req: NextRequest): boolean {
  return req.nextUrl.protocol === "https:" || req.headers.get("x-forwarded-proto") === "https";
}

/** Sign in: validate the token against the server, then store it in an httpOnly cookie. */
export async function POST(req: NextRequest): Promise<NextResponse> {
  if (!isSameOrigin(req)) return NextResponse.json({ detail: "cross-origin request refused" }, { status: 403 });
  if (isMockMode()) return NextResponse.json({ detail: "mock mode: no sign-in needed" }, { status: 409 });
  let parsed: z.infer<typeof SessionIn>;
  try {
    parsed = SessionIn.parse(await req.json());
  } catch {
    return NextResponse.json({ detail: "token is required (8–4096 characters)" }, { status: 422 });
  }
  if (parsed.apiUrl && !isAllowedApiUrl(parsed.apiUrl)) {
    return NextResponse.json({ detail: "that server URL is not on this dashboard's allowlist" }, { status: 422 });
  }
  const apiUrl = resolveApiUrl(parsed.apiUrl);
  if (!apiUrl) return NextResponse.json({ detail: "SQUADHQ_API_URL is not configured" }, { status: 503 });

  const token = parsed.token.trim();
  try {
    const who = await createApiClient({ baseUrl: apiUrl, token, timeoutMs: API_TIMEOUT_MS }).whoami();
    const res = NextResponse.json({ ok: true, name: who.name, role: who.role, apiUrl });
    const opts = cookieOptions(isSecure(req));
    res.cookies.set(TOKEN_COOKIE, sealToken(token), opts);
    res.cookies.set(API_URL_COOKIE, apiUrl, opts);
    return res;
  } catch (e) {
    const status = e instanceof ApiError && e.status === 401 ? 401 : 502;
    const detail = status === 401 ? "the server rejected this token" : "could not reach the server";
    return NextResponse.json({ detail }, { status });
  }
}

/** Sign out: clear the cookies. */
export async function DELETE(req: NextRequest): Promise<NextResponse> {
  if (!isSameOrigin(req)) return NextResponse.json({ detail: "cross-origin request refused" }, { status: 403 });
  const res = NextResponse.json({ ok: true });
  res.cookies.delete(TOKEN_COOKIE);
  res.cookies.delete(API_URL_COOKIE);
  return res;
}
