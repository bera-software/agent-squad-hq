import { afterEach, describe, expect, it, vi } from "vitest";
import { createBrowserApi } from "@/lib/api";
import { pageLinks } from "@/lib/audit";
import { allowedApiUrls, anonymousAccessEnabled, configWarnings, fallbackToken, isAllowedApiUrl, resolveApiUrl } from "@/lib/config";
import { isProxyAllowed } from "@/lib/proxy-policy";
import { isSameOrigin, sealToken, unsealToken } from "@/lib/session";
import { buildCsp } from "@/middleware";
import { buildFixtures } from "./helpers";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("session sealing", () => {
  it("round-trips with a secret and rejects tampering", () => {
    vi.stubEnv("SQUADHQ_SESSION_SECRET", "x".repeat(32));
    const sealed = sealToken("secret-token");
    expect(sealed).not.toContain("secret-token");
    expect(unsealToken(sealed)).toBe("secret-token");
    const tampered = sealed.slice(0, -2) + (sealed.endsWith("A") ? "BB" : "AA");
    expect(unsealToken(tampered)).toBeNull();
  });

  it("refuses plaintext cookies once a secret is configured", () => {
    vi.stubEnv("SQUADHQ_SESSION_SECRET", "");
    const plain = sealToken("tok");
    expect(unsealToken(plain)).toBe("tok");
    vi.stubEnv("SQUADHQ_SESSION_SECRET", "y".repeat(32));
    expect(unsealToken(plain)).toBeNull();
    expect(unsealToken("garbage")).toBeNull();
  });
});

describe("API URL allowlist (SSRF guard)", () => {
  it("only accepts configured origins", () => {
    vi.stubEnv("SQUADHQ_API_URL", "https://hq.example.com/");
    vi.stubEnv("SQUADHQ_API_URL_ALLOWLIST", "https://staging.example.com, not a url");
    expect(allowedApiUrls()).toEqual(["https://hq.example.com", "https://staging.example.com"]);
    expect(isAllowedApiUrl("https://staging.example.com/api/v1")).toBe(true);
    expect(isAllowedApiUrl("http://169.254.169.254")).toBe(false);
    expect(resolveApiUrl("http://169.254.169.254")).toBe("https://hq.example.com");
  });
});

describe("proxy policy", () => {
  it("allows only the dashboard's endpoints", () => {
    expect(isProxyAllowed("GET", "approvals")).toBe(true);
    expect(isProxyAllowed("POST", "approvals/apv-1/decision")).toBe(true);
    expect(isProxyAllowed("GET", "tokens")).toBe(false);
    expect(isProxyAllowed("POST", "tokens")).toBe(false);
    expect(isProxyAllowed("POST", "jobs")).toBe(false);
    expect(isProxyAllowed("DELETE", "tokens/x")).toBe(false);
    expect(isProxyAllowed("GET", "approvals/../tokens")).toBe(false);
  });
});

describe("same-origin check", () => {
  it("accepts same-origin and rejects cross-site", () => {
    const ok = new Request("https://dash.example.com/api/session", {
      method: "POST",
      headers: { origin: "https://dash.example.com", host: "dash.example.com", "sec-fetch-site": "same-origin" },
    });
    const bad = new Request("https://dash.example.com/api/session", {
      method: "POST",
      headers: { origin: "https://evil.example", host: "dash.example.com" },
    });
    expect(isSameOrigin(ok)).toBe(true);
    expect(isSameOrigin(bad)).toBe(false);
  });
});

describe("CSP", () => {
  it("uses a nonce and forbids framing", () => {
    const csp = buildCsp("abc", false);
    expect(csp).toContain("'nonce-abc'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).not.toContain("unsafe-eval");
    expect(buildCsp("abc", true)).toContain("unsafe-eval");
  });
});

describe("browser API client", () => {
  it("goes through the same-origin proxy without a token", async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ approvals: [] }), { status: 200 }));
    const api = createBrowserApi(fetchImpl as unknown as typeof fetch);
    await api.approvals();
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/proxy/approvals");
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
    expect(init.credentials).toBe("same-origin");
  });
});

describe("audit page links", () => {
  it("flags a broken link", () => {
    const events = buildFixtures().audit.slice(0, 4);
    expect(pageLinks(events)).toEqual([true, true, true, true]);
    const broken = events.map((e, i) => (i === 2 ? { ...e, prev_hash: "f".repeat(64) } : e));
    expect(pageLinks(broken)).toEqual([true, true, false, true]);
  });
});

describe("anonymous fallback token (S7)", () => {
  it("is ignored without the explicit opt-in", () => {
    vi.stubEnv("SQUADHQ_API_TOKEN", "fallback-token-123");
    vi.stubEnv("SQUADHQ_DASHBOARD_ANONYMOUS", "");
    expect(anonymousAccessEnabled()).toBe(false);
    expect(fallbackToken()).toBeNull();
    expect(configWarnings().join(" ")).toContain("is set but ignored");
  });

  it("is used only with SQUADHQ_DASHBOARD_ANONYMOUS=true, with a warning", () => {
    vi.stubEnv("SQUADHQ_API_TOKEN", "fallback-token-123");
    vi.stubEnv("SQUADHQ_DASHBOARD_ANONYMOUS", "true");
    expect(fallbackToken()).toBe("fallback-token-123");
    expect(configWarnings().join(" ")).toContain("authenticating reverse proxy");
  });

  it("needs a token as well as the opt-in", () => {
    vi.stubEnv("SQUADHQ_API_TOKEN", "");
    vi.stubEnv("SQUADHQ_DASHBOARD_ANONYMOUS", "true");
    expect(fallbackToken()).toBeNull();
  });
});
