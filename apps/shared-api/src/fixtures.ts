/**
 * Deterministic fixtures and an in-memory mock `fetch` that speaks API v1.
 * Used by the dashboard's MOCK_API=1 mode, the mobile app's demo mode and tests.
 * All data is fictional.
 */
import type { FetchLike } from "./client";
import type { Approval, AuditEvent, Brief, Capacity, Costs, Project, Run, Ticket } from "./schemas";

export interface Fixtures {
  projects: Project[];
  tickets: Ticket[];
  runs: Run[];
  approvals: Approval[];
  audit: AuditEvent[];
  brief: Brief;
  capacity: Capacity;
}

function iso(d: Date): string {
  // Server format: microsecond precision, e.g. 2026-10-21T08:30:00.123000Z
  return d.toISOString().replace(/Z$/, "000Z");
}

function hoursAgo(now: Date, h: number): string {
  return iso(new Date(now.getTime() - h * 3_600_000));
}

function fakeHash(n: number): string {
  // Not a real SHA-256; mock data only.
  let x = (n * 2654435761) >>> 0;
  let out = "";
  for (let i = 0; i < 64; i++) {
    x = (x * 1103515245 + 12345) >>> 0;
    out += "0123456789abcdef"[x % 16];
  }
  return out;
}

export function buildFixtures(now: Date = new Date()): Fixtures {
  const projects: Project[] = [
    { name: "Storefront", repo: "example-org/storefront", tracker: "github", data: { packs: ["node-next"] } },
    { name: "Billing", repo: "example-org/billing", tracker: "jira", data: { packs: ["java-spring"] } },
    { name: "Mobile", repo: "example-org/mobile", tracker: "linear", data: { packs: ["react-native"] } },
  ];

  const t = (
    project: string,
    key: string,
    title: string,
    flow_state: string,
    labels: string[],
    h: number,
    state = "open",
  ): Ticket => ({
    project,
    key,
    title,
    state,
    flow_state,
    labels,
    url: `https://example.com/${project.toLowerCase()}/${key}`,
    tracker: projects.find((p) => p.name === project)?.tracker ?? "github",
    updated_at: hoursAgo(now, h),
  });

  const tickets: Ticket[] = [
    t("Storefront", "#101", "Add guest checkout", "in_progress", ["agent:implement", "status:in-progress"], 1),
    t("Storefront", "#102", "Product search is slow on mobile", "in_review", ["agent:implement", "status:in-review"], 2),
    t("Storefront", "#103", "Cookie banner copy", "new", [], 5),
    t("Storefront", "#97", "Refactor cart reducer", "closed", [], 30, "closed"),
    t("Billing", "ENG-41", "Proration for annual plans", "needs_design", ["needs-design"], 3),
    t("Billing", "ENG-44", "Invoice PDF footer", "shaped", ["triaged:ready"], 4),
    t("Billing", "ENG-45", "Stripe webhook retries", "needs_human +blocked", ["needs-human"], 6),
    t("Mobile", "MOB-7", "Offline approvals queue", "dispatched", ["agent:implement"], 2),
    t("Mobile", "MOB-8", "Dark mode polish", "needs_shaping", ["proposal:pending"], 8),
  ];

  const r = (
    id: string,
    project: string,
    role: string,
    outcome: string,
    h: number,
    cost: number,
    dur: number,
    model: string,
    account = "",
  ): Run => ({
    id,
    project,
    role,
    outcome,
    started_at: hoursAgo(now, h),
    model,
    executor: "claude-code",
    cost_usd: cost,
    duration_s: dur,
    input_tokens: Math.round(cost * 90_000),
    output_tokens: Math.round(cost * 12_000),
    error_class: outcome === "failure" ? "AGENT" : "none",
    job_id: `job-${id}`,
    data: {
      ...(outcome === "failure" ? { error: "tests failed after 3 fix rounds" } : {}),
      ...(account ? { account } : {}),
    },
  });

  const runs: Run[] = [
    r("run-001", "Storefront", "implement", "success", 0.5, 1.82, 1260, "claude-sonnet-4-5", "team-b"),
    r("run-002", "Storefront", "review", "success", 0.8, 0.41, 240, "claude-opus-4-1", "team-b"),
    r("run-003", "Billing", "design", "success", 1.5, 0.95, 610, "claude-opus-4-1", "org-key"),
    r("run-004", "Billing", "implement", "failure", 2.2, 2.4, 1980, "claude-sonnet-4-5", "team-a"),
    r("run-005", "Mobile", "po-shape", "success", 3, 0.12, 45, "claude-haiku-4-5", "team-a"),
    r("run-006", "Mobile", "implement", "skipped", 4, 0, 3, "claude-sonnet-4-5", "team-a"),
    r("run-007", "Storefront", "daily-brief", "success", 26, 0.08, 30, "claude-haiku-4-5"),
    r("run-008", "Billing", "review", "success", 50, 0.38, 200, "claude-opus-4-1"),
  ];

  const a = (
    id: string,
    kind: string,
    project: string,
    subject: string,
    title: string,
    h: number,
    data: Record<string, unknown> = {},
  ): Approval => ({
    id,
    kind,
    project,
    subject,
    title,
    url: `https://example.com/${project.toLowerCase()}/${subject.replace("#", "")}`,
    status: "pending",
    created_at: hoursAgo(now, h),
    updated_at: hoursAgo(now, h),
    data,
    decided_by: null,
    decided_at: null,
    decision_comment: "",
    action_job_id: null,
  });

  const approvals: Approval[] = [
    a("apv-1", "merge", "Storefront", "pr#118", "Merge: Add guest checkout", 0.6, { risk: "medium", checks: "passing" }),
    a("apv-2", "proposal", "Mobile", "MOB-8", "Proposal: Dark mode polish (S)", 7, { size: "S" }),
    a("apv-3", "deploy", "Billing", "release v2.4.0", "Deploy Billing v2.4.0 to production", 2, { environment: "production" }),
    a("apv-4", "escalation", "Billing", "ENG-45", "Escalation: Stripe webhook retries failing", 1, {
      severity: "P1",
      reason: "3 consecutive agent failures",
    }),
  ];

  const audit: AuditEvent[] = Array.from({ length: 60 }, (_, i) => {
    const seq = i + 1;
    const actions = ["run.reported", "approval.created", "approval.decided", "job.enqueued", "brief.added"];
    return {
      id: `evt-${seq}`,
      seq,
      ts: hoursAgo(now, 60 - i),
      actor: seq % 4 === 0 ? "token:alice" : "system",
      action: actions[seq % actions.length] ?? "run.reported",
      subject: seq % 3 === 0 ? `pr#${100 + seq}` : `job:${seq}`,
      data: {},
      prev_hash: seq === 1 ? "0".repeat(64) : fakeHash(seq - 1),
      hash: fakeHash(seq),
    };
  });

  const brief: Brief = {
    id: "brief-1",
    created_at: hoursAgo(now, 2),
    project: "",
    markdown: [
      "# Daily brief",
      "",
      "**3 projects**, 6 runs in the last 24 h, spend **$5.70**.",
      "",
      "## Needs you",
      "",
      "- Merge `pr#118` on *Storefront* (risk: medium)",
      "- Escalation on Billing ENG-45: Stripe webhook retries",
      "",
      "## Shipped",
      "",
      "1. Refactor cart reducer",
      "2. Invoice PDF header",
      "",
      "> Budget is at 38% of the monthly limit.",
      "",
      "[Open the pipeline](https://example.com/pipeline)",
      "",
      "<script>alert('this must never execute')</script>",
    ].join("\n"),
  };

  const capacity = buildCapacity(now);

  return { projects, tickets, runs, approvals, audit, brief, capacity };
}

/**
 * Sample account pool at `now`: team-a hit its usage limit 20 min ago (60 min
 * breaker, so limited for 40 more minutes); with the "ordered" strategy the
 * next job runs on team-b.
 */
export function buildCapacity(now: Date = new Date()): Capacity {
  const trippedAt = new Date(now.getTime() - 20 * 60_000);
  return {
    configured: true,
    source: "auth.json",
    strategy: "ordered",
    fallback_on_capacity_trip: false,
    accounts: [
      {
        id: "team-a",
        auth: "subscription",
        secret: "CLAUDE_CODE_OAUTH_TOKEN_1",
        state: "tripped",
        tripped_until: new Date(trippedAt.getTime() + 60 * 60_000).toISOString(),
        tripped_at: trippedAt.toISOString(),
      },
      { id: "team-b", auth: "subscription", secret: "CLAUDE_CODE_OAUTH_TOKEN_2", state: "available", tripped_until: "", tripped_at: "" },
      { id: "org-key", auth: "api", secret: "ANTHROPIC_API_KEY_3", state: "available", tripped_until: "", tripped_at: "" },
    ],
    available: 2,
    all_tripped_until: "",
  };
}

/** What GET /capacity returns when no account pool is configured. */
export function emptyCapacity(): Capacity {
  return {
    configured: false,
    source: "none",
    strategy: "ordered",
    fallback_on_capacity_trip: false,
    accounts: [],
    available: 0,
    all_tripped_until: "",
  };
}

export function costsFromRuns(runs: readonly Run[], month: string): Costs {
  const out: Costs = {
    month,
    total_usd: 0,
    runs: 0,
    by_project: {},
    by_role: {},
    by_model: {},
    budget_usd: 100,
    budget_by_project: { Storefront: 60, Billing: 40, Mobile: 40 },
  };
  for (const r of runs) {
    if (!r.started_at.startsWith(`${month}-`)) continue;
    out.total_usd += r.cost_usd;
    out.runs += 1;
    out.by_project[r.project] = (out.by_project[r.project] ?? 0) + r.cost_usd;
    out.by_role[r.role] = (out.by_role[r.role] ?? 0) + r.cost_usd;
    const m = r.model || "unknown";
    out.by_model[m] = (out.by_model[m] ?? 0) + r.cost_usd;
  }
  out.total_usd = Math.round(out.total_usd * 1e6) / 1e6;
  return out;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

export interface MockFetchOptions {
  now?: Date;
  fixtures?: Fixtures;
  /** Artificial latency in ms (default 0). */
  latencyMs?: number;
  /** Approval ids whose decision should fail with 409 (to exercise rollback). */
  failDecisionsFor?: readonly string[];
}

/**
 * A `fetch` replacement that serves fixtures for every API v1 endpoint the apps use.
 * State (decisions, devices, requests) lives in the returned closure.
 */
export function createMockFetch(opts: MockFetchOptions = {}): FetchLike & { fixtures: Fixtures } {
  const now = opts.now ?? new Date();
  const fx = opts.fixtures ?? buildFixtures(now);
  let seq = fx.audit.length;
  let counter = 0;

  const handler = async (input: string, init?: RequestInit): Promise<Response> => {
    if (opts.latencyMs) await new Promise((r) => setTimeout(r, opts.latencyMs));
    const url = new URL(input, "http://mock.local");
    const method = (init?.method ?? "GET").toUpperCase();
    const path = url.pathname.replace(/^.*\/api\/v1/, "").replace(/^\/api\/proxy/, "");
    const q = url.searchParams;

    if (method === "GET") {
      if (path === "/healthz") return json({ status: "ok", version: "mock" });
      if (path === "/whoami") return json({ name: "demo", role: "admin" });
      if (path === "/projects") return json({ projects: fx.projects });
      if (path === "/tickets") {
        const project = q.get("project");
        const state = q.get("state");
        return json({
          tickets: fx.tickets.filter(
            (t) => (!project || t.project === project) && (!state || t.state === state || t.flow_state === state),
          ),
        });
      }
      if (path === "/runs") {
        const project = q.get("project");
        const limit = Number(q.get("limit") ?? 50);
        return json({ runs: fx.runs.filter((r) => !project || r.project === project).slice(0, limit) });
      }
      if (path === "/approvals") {
        const status = q.get("status") ?? "pending";
        const project = q.get("project");
        return json({
          approvals: fx.approvals.filter(
            (a) => (status === "all" || a.status === status) && (!project || a.project === project),
          ),
        });
      }
      const one = /^\/approvals\/([^/]+)$/.exec(path);
      if (one) {
        const found = fx.approvals.find((a) => a.id === decodeURIComponent(one[1] ?? ""));
        return found ? json(found) : json({ detail: "approval not found" }, 404);
      }
      if (path === "/costs") {
        const month = q.get("month") ?? now.toISOString().slice(0, 7);
        if (!/^\d{4}-\d{2}$/.test(month)) return json({ detail: "month must be YYYY-MM" }, 422);
        return json(costsFromRuns(fx.runs, month));
      }
      // Without a fixed clock, keep the sample breaker window relative to the request time
      // so a long-running mock server still shows one limited account.
      if (path === "/capacity") return json(opts.now || opts.fixtures ? fx.capacity : buildCapacity(new Date()));
      if (path === "/audit") {
        const since = Number(q.get("since") ?? 0);
        const limit = Number(q.get("limit") ?? 500);
        const events = fx.audit.filter((e) => e.seq > since).slice(0, limit);
        return json({ events, next_since: events.length ? events[events.length - 1]?.seq : since });
      }
      if (path === "/audit/verify") {
        const last = fx.audit[fx.audit.length - 1];
        return json({ ok: true, checked: fx.audit.length, first_bad_seq: null, reason: "", head_hash: last?.hash ?? "" });
      }
      if (path === "/briefs/latest") return json(fx.brief);
    }

    if (method === "POST") {
      const body: unknown = typeof init?.body === "string" ? JSON.parse(init.body) : {};
      const decide = /^\/approvals\/([^/]+)\/decision$/.exec(path);
      if (decide) {
        const id = decodeURIComponent(decide[1] ?? "");
        const idx = fx.approvals.findIndex((a) => a.id === id);
        const current = fx.approvals[idx];
        if (!current) return json({ detail: "approval not found" }, 404);
        if (current.status !== "pending" || opts.failDecisionsFor?.includes(id)) {
          return json({ detail: `approval ${id} is already ${current.status === "pending" ? "decided" : current.status}` }, 409);
        }
        const b = body as { decision?: string; comment?: string };
        if (b.decision !== "approve" && b.decision !== "reject") return json({ detail: "invalid decision" }, 422);
        const ts = iso(new Date());
        const updated: Approval = {
          ...current,
          status: b.decision === "approve" ? "approved" : "rejected",
          decided_by: "token:demo",
          decided_at: ts,
          decision_comment: b.comment ?? "",
          updated_at: ts,
          action_job_id: b.decision === "approve" ? `job-action-${id}` : null,
        };
        fx.approvals[idx] = updated;
        seq += 1;
        const event: AuditEvent = {
          id: `evt-${seq}`,
          seq,
          ts,
          actor: "token:demo",
          action: "approval.decided",
          subject: `approval:${id}`,
          data: { decision: b.decision },
          prev_hash: fakeHash(seq - 1),
          hash: fakeHash(seq),
        };
        fx.audit.push(event);
        return json({ approval: updated, audit_event: event, action_job_id: updated.action_job_id }, 202);
      }
      if (path === "/requests") {
        const b = body as { project?: string; title?: string };
        if (!b.project || !b.title) return json({ detail: "project and title are required" }, 422);
        counter += 1;
        return json(
          { id: `req-${counter}`, project: b.project, status: "queued", ticket_key: null, ticket_url: null, created_at: iso(new Date()) },
          202,
        );
      }
      if (path === "/devices") {
        counter += 1;
        return json({ id: `dev-${counter}`, created_at: iso(new Date()) }, 201);
      }
    }

    return json({ detail: "not found" }, 404);
  };

  return Object.assign(handler, { fixtures: fx });
}
