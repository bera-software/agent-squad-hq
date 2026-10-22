/**
 * Hand-written typed client for the Agent Squad HQ REST API v1.
 *
 * Every response is validated with zod before it reaches UI code. Network
 * failures, timeouts, HTTP errors and malformed bodies all surface as
 * `ApiError`, so callers handle a single error type.
 */
import type { z } from "zod";
import {
  ApprovalSchema,
  ApprovalsResponseSchema,
  AuditPageSchema,
  AuditVerifySchema,
  BriefSchema,
  CapacitySchema,
  CostsSchema,
  DecisionResultSchema,
  DecisionSchema,
  DeviceRegistrationSchema,
  DeviceResultSchema,
  HealthSchema,
  IntakeRequestSchema,
  IntakeResultSchema,
  ProjectsResponseSchema,
  RunsResponseSchema,
  TicketsResponseSchema,
  WhoamiSchema,
  type Approval,
  type AuditPage,
  type AuditVerify,
  type Brief,
  type Capacity,
  type Costs,
  type Decision,
  type DecisionResult,
  type DeviceRegistration,
  type DeviceResult,
  type Health,
  type IntakeRequest,
  type IntakeResult,
  type Project,
  type Run,
  type Ticket,
  type Whoami,
} from "./schemas";

export const API_PREFIX = "/api/v1";
export const DEFAULT_TIMEOUT_MS = 15_000;

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface ClientOptions {
  /** Server origin, e.g. `https://hq.example.com`. Empty string = same origin. */
  baseUrl: string;
  /** Bearer token. Omit when a proxy adds it (dashboard browser code). */
  token?: string | undefined;
  /** Path prefix prepended to every endpoint. Defaults to `/api/v1`. */
  apiPrefix?: string;
  fetch?: FetchLike;
  timeoutMs?: number;
  headers?: Record<string, string>;
}

export type ApiErrorCode = "http" | "network" | "timeout" | "invalid_response" | "invalid_request";

export class ApiError extends Error {
  readonly status: number;
  readonly code: ApiErrorCode;
  readonly path: string;
  readonly detail: string;

  constructor(opts: { status: number; code: ApiErrorCode; path: string; detail: string }) {
    super(`${opts.code}${opts.status ? ` ${opts.status}` : ""} on ${opts.path}: ${opts.detail}`);
    this.name = "ApiError";
    this.status = opts.status;
    this.code = opts.code;
    this.path = opts.path;
    this.detail = opts.detail;
  }

  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  get isForbidden(): boolean {
    return this.status === 403;
  }
}

export function normalizeBaseUrl(raw: string): string {
  const trimmed = raw.trim().replace(/\/+$/, "");
  if (trimmed === "") return "";
  let url: URL;
  try {
    url = new URL(trimmed);
  } catch {
    throw new ApiError({ status: 0, code: "invalid_request", path: "", detail: "server URL is not a valid URL" });
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new ApiError({ status: 0, code: "invalid_request", path: "", detail: "server URL must use http(s)" });
  }
  // Strip a trailing /api/v1 so both "https://x" and "https://x/api/v1" work.
  return trimmed.replace(/\/api\/v1$/, "");
}

function query(params: Record<string, string | number | undefined | null>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

async function readDetail(res: Response): Promise<string> {
  try {
    const text = await res.text();
    try {
      const body: unknown = JSON.parse(text);
      if (body && typeof body === "object" && "detail" in body) {
        const d = (body as { detail: unknown }).detail;
        return typeof d === "string" ? d : JSON.stringify(d);
      }
    } catch {
      /* not JSON */
    }
    return text.slice(0, 300) || res.statusText;
  } catch {
    return res.statusText;
  }
}

export interface ApiClient {
  healthz(): Promise<Health>;
  whoami(): Promise<Whoami>;
  projects(): Promise<Project[]>;
  tickets(params?: { project?: string; state?: string; limit?: number }): Promise<Ticket[]>;
  runs(params?: { project?: string; limit?: number }): Promise<Run[]>;
  approvals(params?: { status?: "pending" | "approved" | "rejected" | "resolved" | "all"; project?: string }): Promise<Approval[]>;
  approval(id: string): Promise<Approval>;
  decide(id: string, decision: Decision): Promise<DecisionResult>;
  costs(month?: string): Promise<Costs>;
  /** Coding-agent account pool status (read-only; no secret values). */
  capacity(): Promise<Capacity>;
  audit(params?: { since?: string | number; limit?: number }): Promise<AuditPage>;
  auditVerify(): Promise<AuditVerify>;
  /** Latest daily brief, or `null` when the server has none yet (404). */
  latestBrief(project?: string): Promise<Brief | null>;
  createRequest(req: IntakeRequest): Promise<IntakeResult>;
  registerDevice(dev: DeviceRegistration): Promise<DeviceResult>;
}

export function createApiClient(opts: ClientOptions): ApiClient {
  const base = normalizeBaseUrl(opts.baseUrl);
  const prefix = opts.apiPrefix ?? API_PREFIX;
  const doFetch: FetchLike = opts.fetch ?? ((input, init) => globalThis.fetch(input, init));
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  async function request<S extends z.ZodType>(
    method: "GET" | "POST",
    path: string,
    schema: S,
    body?: unknown,
  ): Promise<z.infer<S>> {
    const headers: Record<string, string> = { Accept: "application/json", ...opts.headers };
    if (opts.token) headers.Authorization = `Bearer ${opts.token}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let res: Response;
    try {
      res = await doFetch(`${base}${prefix}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
        cache: "no-store",
        credentials: opts.token ? "omit" : "same-origin",
      });
    } catch (e) {
      const aborted = controller.signal.aborted;
      throw new ApiError({
        status: 0,
        code: aborted ? "timeout" : "network",
        path,
        detail: aborted ? `no response within ${timeoutMs} ms` : e instanceof Error ? e.message : "network error",
      });
    } finally {
      clearTimeout(timer);
    }
    if (!res.ok) {
      throw new ApiError({ status: res.status, code: "http", path, detail: await readDetail(res) });
    }
    let json: unknown;
    try {
      json = await res.json();
    } catch {
      throw new ApiError({ status: res.status, code: "invalid_response", path, detail: "response is not JSON" });
    }
    const parsed = schema.safeParse(json);
    if (!parsed.success) {
      const issue = parsed.error.issues[0];
      const where = issue ? issue.path.join(".") || "(root)" : "(root)";
      throw new ApiError({
        status: res.status,
        code: "invalid_response",
        path,
        detail: `unexpected response shape at ${where}: ${issue?.message ?? "invalid"}`,
      });
    }
    return parsed.data;
  }

  function validated<S extends z.ZodType>(schema: S, value: unknown, path: string): z.infer<S> {
    const parsed = schema.safeParse(value);
    if (!parsed.success) {
      const issue = parsed.error.issues[0];
      throw new ApiError({
        status: 0,
        code: "invalid_request",
        path,
        detail: `${issue ? issue.path.join(".") : "body"}: ${issue?.message ?? "invalid"}`,
      });
    }
    return parsed.data;
  }

  const enc = encodeURIComponent;

  return {
    healthz: () => request("GET", "/healthz", HealthSchema),
    whoami: () => request("GET", "/whoami", WhoamiSchema),
    projects: async () => (await request("GET", "/projects", ProjectsResponseSchema)).projects,
    tickets: async (p = {}) =>
      (await request("GET", `/tickets${query({ project: p.project, state: p.state, limit: p.limit })}`, TicketsResponseSchema))
        .tickets,
    runs: async (p = {}) =>
      (await request("GET", `/runs${query({ project: p.project, limit: p.limit })}`, RunsResponseSchema)).runs,
    approvals: async (p = {}) =>
      (await request("GET", `/approvals${query({ status: p.status, project: p.project })}`, ApprovalsResponseSchema))
        .approvals,
    approval: (id) => request("GET", `/approvals/${enc(id)}`, ApprovalSchema),
    decide: async (id, decision) => {
      const path = `/approvals/${enc(id)}/decision`;
      return request("POST", path, DecisionResultSchema, validated(DecisionSchema, decision, path));
    },
    costs: async (month) => {
      if (month !== undefined && !/^\d{4}-(0[1-9]|1[0-2])$/.test(month)) {
        throw new ApiError({ status: 0, code: "invalid_request", path: "/costs", detail: "month must be YYYY-MM" });
      }
      return request("GET", `/costs${query({ month })}`, CostsSchema);
    },
    capacity: () => request("GET", "/capacity", CapacitySchema),
    audit: (p = {}) => request("GET", `/audit${query({ since: p.since, limit: p.limit })}`, AuditPageSchema),
    auditVerify: () => request("GET", "/audit/verify", AuditVerifySchema),
    latestBrief: async (project) => {
      try {
        return await request("GET", `/briefs/latest${query({ project })}`, BriefSchema);
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      }
    },
    createRequest: async (req) =>
      request("POST", "/requests", IntakeResultSchema, validated(IntakeRequestSchema, req, "/requests")),
    registerDevice: async (dev) =>
      request("POST", "/devices", DeviceResultSchema, validated(DeviceRegistrationSchema, dev, "/devices")),
  };
}
