import { describe, expect, it, vi } from "vitest";
import { ApiError, createApiClient, normalizeBaseUrl } from "../src/client";
import { formatRelative, lastRunByAccount, runAccount } from "../src/capacity";
import { createMockFetch, emptyCapacity } from "../src/fixtures";
import { columnFor, groupByColumn, wipByProject } from "../src/flow";
import { formatDuration, formatUsd } from "../src/format";
import { parseMarkdown, parseInline, safeHref } from "../src/markdown";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("createApiClient", () => {
  it("sends the bearer token and parses runs", async () => {
    const fetch = vi.fn(async () =>
      jsonResponse({ runs: [{ id: "r1", project: "p", role: "implement", outcome: "success", started_at: "2026-10-01T00:00:00Z" }] }),
    );
    const api = createApiClient({ baseUrl: "https://hq.example.com/", token: "tok", fetch });
    const runs = await api.runs({ project: "p", limit: 5 });
    expect(runs[0]?.cost_usd).toBe(0);
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://hq.example.com/api/v1/runs?project=p&limit=5");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok");
  });

  it("raises ApiError with the server detail on HTTP errors", async () => {
    const api = createApiClient({ baseUrl: "", fetch: async () => jsonResponse({ detail: "invalid token" }, 401) });
    await expect(api.projects()).rejects.toMatchObject({ status: 401, code: "http", detail: "invalid token" });
  });

  it("rejects malformed responses", async () => {
    const api = createApiClient({ baseUrl: "", fetch: async () => jsonResponse({ projects: [{ nope: 1 }] }) });
    const err = await api.projects().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("invalid_response");
  });

  it("maps network failures", async () => {
    const api = createApiClient({
      baseUrl: "",
      fetch: async () => {
        throw new TypeError("fetch failed");
      },
    });
    await expect(api.whoami()).rejects.toMatchObject({ code: "network", status: 0 });
  });

  it("returns null for a missing brief", async () => {
    const api = createApiClient({ baseUrl: "", fetch: async () => jsonResponse({ detail: "no brief yet" }, 404) });
    expect(await api.latestBrief()).toBeNull();
  });

  it("validates decision bodies before sending", async () => {
    const fetch = vi.fn();
    const api = createApiClient({ baseUrl: "", fetch });
    // @ts-expect-error invalid on purpose
    await expect(api.decide("a", { decision: "maybe" })).rejects.toMatchObject({ code: "invalid_request" });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("validates the month for costs", async () => {
    const api = createApiClient({ baseUrl: "", fetch: vi.fn() });
    await expect(api.costs("2026-13")).rejects.toBeInstanceOf(ApiError);
  });

  it("parses capacity with defaults and never needs secret values", async () => {
    const fetch = vi.fn(async () =>
      jsonResponse({
        configured: true,
        source: "env",
        strategy: "round_robin",
        fallback_on_capacity_trip: false,
        accounts: [
          { id: "api-1", auth: "api", secret: "ANTHROPIC_API_KEY_1", state: "tripped", tripped_until: "2026-10-21T13:00:00+00:00", tripped_at: "2026-10-21T12:00:00+00:00" },
          { id: "sub-1", auth: "subscription", state: "available" },
        ],
        available: 1,
        all_tripped_until: "",
      }),
    );
    const api = createApiClient({ baseUrl: "https://hq.example.com", token: "tok", fetch });
    const cap = await api.capacity();
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://hq.example.com/api/v1/capacity");
    expect(init.method).toBe("GET");
    expect(cap.strategy).toBe("round_robin");
    expect(cap.accounts[1]).toEqual({ id: "sub-1", auth: "subscription", secret: "", state: "available", tripped_until: "", tripped_at: "" });
  });

  it("rejects a malformed capacity response", async () => {
    const api = createApiClient({ baseUrl: "", fetch: async () => jsonResponse({ accounts: [] }) });
    await expect(api.capacity()).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("rejects non-http server URLs", () => {
    expect(() => normalizeBaseUrl("javascript:alert(1)")).toThrow(ApiError);
    expect(normalizeBaseUrl("https://x.example/api/v1/")).toBe("https://x.example");
  });
});

describe("mock fetch", () => {
  it("serves every endpoint and records decisions", async () => {
    const mock = createMockFetch({ now: new Date("2026-10-21T12:00:00Z") });
    const api = createApiClient({ baseUrl: "", fetch: mock });
    expect((await api.projects()).length).toBe(3);
    const pending = await api.approvals();
    expect(pending.length).toBe(4);
    const res = await api.decide("apv-1", { decision: "approve", comment: "ship it" });
    expect(res.approval.status).toBe("approved");
    expect((await api.approvals()).length).toBe(3);
    await expect(api.decide("apv-1", { decision: "reject" })).rejects.toMatchObject({ status: 409 });
    const costs = await api.costs("2026-10");
    expect(costs.total_usd).toBeGreaterThan(0);
    expect(costs.budget_usd).toBe(100);
    const page = await api.audit({ since: 50, limit: 5 });
    expect(page.events.map((e) => e.seq)).toEqual([51, 52, 53, 54, 55]);
    expect(page.next_since).toBe(55);
    expect((await api.auditVerify()).ok).toBe(true);
    const cap = await api.capacity();
    expect(cap.accounts.map((a) => a.id)).toEqual(["team-a", "team-b", "org-key"]);
    expect(cap.accounts[0]?.tripped_until).toBe("2026-10-21T12:40:00.000Z");
    expect(cap.available).toBe(2);
    expect((await api.createRequest({ project: "Mobile", title: "Build me a thing" })).status).toBe("queued");
    expect(
      (await api.registerDevice({ expo_push_token: "ExponentPushToken[abc]", platform: "ios" })).id,
    ).toMatch(/^dev-/);
  });
});

describe("flow helpers", () => {
  it("maps flow states (with modifiers) to columns", () => {
    expect(columnFor({ flow_state: "needs_human +blocked", state: "open" }).id).toBe("human");
    expect(columnFor({ flow_state: "in_progress", state: "closed" }).id).toBe("done");
    expect(columnFor({ flow_state: "weird", state: "open" }).id).toBe("other");
  });

  it("groups and counts WIP", () => {
    const { fixtures } = createMockFetch();
    const grouped = groupByColumn(fixtures.tickets);
    expect(grouped.get("building")?.length).toBe(2);
    expect(wipByProject(fixtures.tickets)).toEqual({ Storefront: 2, Billing: 2, Mobile: 1 });
  });
});

describe("capacity helpers", () => {
  it("attributes runs to accounts and finds the latest per account", () => {
    const { fixtures } = createMockFetch({ now: new Date("2026-10-21T12:00:00Z") });
    expect(runAccount({ data: { account: "team-a" } })).toBe("team-a");
    expect(runAccount({ data: { account: 3 } })).toBeNull();
    expect(runAccount({ data: {} })).toBeNull();
    const last = lastRunByAccount(fixtures.runs);
    expect(last.get("team-a")?.id).toBe("run-004");
    expect(last.get("team-b")?.id).toBe("run-001");
    expect(emptyCapacity().configured).toBe(false);
  });

  it("formats relative times", () => {
    const now = new Date("2026-10-21T12:00:00Z");
    expect(formatRelative("2026-10-21T12:38:10Z", now)).toBe("in 38 min");
    expect(formatRelative("2026-10-21T13:05:00+00:00", now)).toBe("in 1 h 5 min");
    expect(formatRelative("2026-10-21T14:00:00Z", now)).toBe("in 2 h");
    expect(formatRelative("2026-10-21T11:48:00Z", now)).toBe("12 min ago");
    expect(formatRelative("2026-10-21T12:00:20Z", now)).toBe("now");
    expect(formatRelative("nope", now)).toBe("");
  });
});

describe("format", () => {
  it("formats money and durations", () => {
    expect(formatUsd(1.5)).toBe("$1.50");
    expect(formatUsd(0.081)).toBe("$0.081");
    expect(formatDuration(45)).toBe("45s");
    expect(formatDuration(1260)).toBe("21m");
    expect(formatDuration(3900)).toBe("1h 5m");
  });
});

describe("markdown", () => {
  it("parses blocks and keeps raw HTML as text", () => {
    const blocks = parseMarkdown("# Title\n\n- a\n- **b**\n\n<script>x</script>\n\n```\ncode\n```");
    expect(blocks.map((b) => b.type)).toEqual(["heading", "list", "paragraph", "code"]);
    expect(blocks[2]).toEqual({ type: "paragraph", children: [{ type: "text", text: "<script>x</script>" }] });
  });

  it("drops unsafe link targets", () => {
    expect(safeHref("javascript:alert(1)")).toBeNull();
    const [link] = parseInline("[x](javascript:alert(1))");
    expect(link).toMatchObject({ type: "link", href: null });
    expect(parseInline("[x](https://e.com)")[0]).toMatchObject({ href: "https://e.com" });
  });
});
