import { afterEach, describe, expect, it, vi } from "vitest";

const jar = new Map<string, string>();
vi.mock("next/headers", () => ({
  cookies: async () => ({ get: (name: string) => (jar.has(name) ? { name, value: jar.get(name) } : undefined) }),
}));
const redirect = vi.fn((url: string) => {
  throw new Error(`NEXT_REDIRECT ${url}`);
});
vi.mock("next/navigation", () => ({ redirect: (url: string) => redirect(url) }));

const { getConnection } = await import("@/lib/server-api");

afterEach(() => {
  vi.unstubAllEnvs();
  redirect.mockClear();
  jar.clear();
});

describe("getConnection", () => {
  it("sends visitors without a session to sign-in, even if SQUADHQ_API_TOKEN is set", async () => {
    vi.stubEnv("MOCK_API", "0");
    vi.stubEnv("SQUADHQ_API_URL", "https://hq.example.com");
    vi.stubEnv("SQUADHQ_API_TOKEN", "fallback-token-123");
    await expect(getConnection()).rejects.toThrow("NEXT_REDIRECT /settings?signin=required");
    expect(await getConnection({ redirectToSignIn: false })).toMatchObject({ mode: "unconfigured", reason: "no_token" });
  });

  it("uses the fallback token only with the anonymous opt-in", async () => {
    vi.stubEnv("MOCK_API", "0");
    vi.stubEnv("SQUADHQ_API_URL", "https://hq.example.com");
    vi.stubEnv("SQUADHQ_API_TOKEN", "fallback-token-123");
    vi.stubEnv("SQUADHQ_DASHBOARD_ANONYMOUS", "true");
    expect(await getConnection()).toMatchObject({ mode: "live", apiUrl: "https://hq.example.com" });
    expect(redirect).not.toHaveBeenCalled();
  });
});
