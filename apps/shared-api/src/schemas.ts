/**
 * Runtime schemas for the Agent Squad HQ REST API v1.
 *
 * Source of truth: docs/architecture/CONVENTIONS.md ("Server REST API (v1)")
 * and the FastAPI models in src/squadhq/server/app.py + src/squadhq/store/base.py.
 * Schemas are deliberately tolerant: unknown keys are stripped and optional
 * fields get defaults, so a newer server does not break an older client.
 */
import { z } from "zod";

const jsonRecord = z.record(z.string(), z.unknown());

export const HealthSchema = z.object({ status: z.string(), version: z.string().optional() });

export const ProjectSchema = z.object({
  name: z.string(),
  repo: z.string().default(""),
  tracker: z.string().default("github"),
  data: jsonRecord.default({}),
});
export const ProjectsResponseSchema = z.object({ projects: z.array(ProjectSchema) });

export const TicketSchema = z.object({
  project: z.string(),
  key: z.string(),
  title: z.string().default(""),
  state: z.string().default("open"),
  flow_state: z.string().default(""),
  labels: z.array(z.string()).default([]),
  url: z.string().default(""),
  tracker: z.string().default(""),
  updated_at: z.string().default(""),
});
export const TicketsResponseSchema = z.object({ tickets: z.array(TicketSchema) });

export const RunSchema = z.object({
  id: z.string(),
  project: z.string(),
  role: z.string(),
  outcome: z.string(),
  started_at: z.string(),
  model: z.string().default(""),
  executor: z.string().default(""),
  cost_usd: z.number().default(0),
  duration_s: z.number().default(0),
  input_tokens: z.number().int().default(0),
  output_tokens: z.number().int().default(0),
  error_class: z.string().default("none"),
  job_id: z.string().nullable().default(null),
  data: jsonRecord.default({}),
});
export const RunsResponseSchema = z.object({ runs: z.array(RunSchema) });

export const APPROVAL_KINDS = ["merge", "proposal", "deploy", "escalation"] as const;
export const APPROVAL_STATUSES = ["pending", "approved", "rejected", "resolved"] as const;

export const ApprovalSchema = z.object({
  id: z.string(),
  // Kept as a string so a new kind on the server does not fail validation.
  kind: z.string(),
  project: z.string().default(""),
  subject: z.string().default(""),
  title: z.string().default(""),
  url: z.string().default(""),
  status: z.string(),
  created_at: z.string(),
  updated_at: z.string().default(""),
  data: jsonRecord.default({}),
  decided_by: z.string().nullable().default(null),
  decided_at: z.string().nullable().default(null),
  decision_comment: z.string().default(""),
  action_job_id: z.string().nullable().default(null),
});
export const ApprovalsResponseSchema = z.object({ approvals: z.array(ApprovalSchema) });

export const DecisionSchema = z.object({
  decision: z.enum(["approve", "reject"]),
  comment: z.string().max(4000).default(""),
});

export const AuditEventSchema = z.object({
  id: z.string(),
  seq: z.number().int(),
  ts: z.string(),
  actor: z.string(),
  action: z.string(),
  subject: z.string().default(""),
  data: jsonRecord.default({}),
  prev_hash: z.string().default(""),
  hash: z.string().default(""),
});
export const AuditPageSchema = z.object({
  events: z.array(AuditEventSchema),
  next_since: z.number().int(),
});
export const AuditVerifySchema = z.object({
  ok: z.boolean(),
  checked: z.number().int(),
  first_bad_seq: z.number().int().nullable().default(null),
  reason: z.string().default(""),
  head_hash: z.string().default(""),
});

export const DecisionResultSchema = z.object({
  approval: ApprovalSchema,
  audit_event: AuditEventSchema.optional(),
  action_job_id: z.string().nullable().default(null),
});

const amountMap = z.record(z.string(), z.number());
export const CostsSchema = z.object({
  month: z.string().regex(/^\d{4}-\d{2}$/),
  total_usd: z.number(),
  runs: z.number().int().default(0),
  by_project: amountMap.default({}),
  by_role: amountMap.default({}),
  by_model: amountMap.default({}),
  // Requested from the server team (budgets.monthly_usd / per_project_usd);
  // optional so the UI works before it lands.
  budget_usd: z.number().nullable().optional(),
  budget_by_project: amountMap.optional(),
});

export const BriefSchema = z.object({
  id: z.string(),
  created_at: z.string(),
  project: z.string().default(""),
  markdown: z.string(),
});

/**
 * GET /api/v1/capacity (viewer): coding-agent account pool, per-account
 * breaker state. Ids and secret NAMES only, never a credential value.
 * Source: src/squadhq/server/accounts.py `status()` + account_pool.status().
 */
export const CAPACITY_STRATEGIES = ["ordered", "round_robin"] as const;
export const AccountStatusSchema = z.object({
  id: z.string(),
  // api | subscription; kept as a string so a new auth mode does not fail validation.
  auth: z.string().default(""),
  /** Name of the env var / secret holding the credential (never its value). */
  secret: z.string().default(""),
  // available | tripped
  state: z.string(),
  tripped_until: z.string().default(""),
  tripped_at: z.string().default(""),
});
export const CapacitySchema = z.object({
  configured: z.boolean(),
  // auth.json | env | none
  source: z.string().default("none"),
  strategy: z.string().default("ordered"),
  fallback_on_capacity_trip: z.boolean().default(false),
  accounts: z.array(AccountStatusSchema).default([]),
  available: z.number().int().default(0),
  all_tripped_until: z.string().default(""),
});

export const WhoamiSchema = z.object({ name: z.string(), role: z.string() });

/** POST /api/v1/requests (requested endpoint; see docs/apps/mobile.md). */
export const IntakeRequestSchema = z.object({
  project: z.string().min(1).max(200),
  title: z.string().min(1).max(256),
  body: z.string().max(20000).default(""),
  source: z.enum(["mobile", "dashboard", "voice"]).default("mobile"),
});
export const IntakeResultSchema = z.object({
  id: z.string(),
  project: z.string(),
  status: z.string().default("queued"),
  ticket_key: z.string().nullable().default(null),
  ticket_url: z.string().nullable().default(null),
  created_at: z.string().default(""),
});

/** POST /api/v1/devices (requested endpoint; see docs/apps/mobile.md). */
export const DeviceRegistrationSchema = z.object({
  expo_push_token: z.string().regex(/^(ExponentPushToken|ExpoPushToken)\[[^\]]+\]$/),
  platform: z.enum(["ios", "android"]),
  name: z.string().max(128).default(""),
  topics: z.array(z.enum(["approvals", "escalations", "brief"])).default(["approvals", "escalations"]),
});
export const DeviceResultSchema = z.object({
  id: z.string(),
  created_at: z.string().default(""),
});

export type Health = z.infer<typeof HealthSchema>;
export type Project = z.infer<typeof ProjectSchema>;
export type Ticket = z.infer<typeof TicketSchema>;
export type Run = z.infer<typeof RunSchema>;
export type Approval = z.infer<typeof ApprovalSchema>;
export type ApprovalKind = (typeof APPROVAL_KINDS)[number];
export type ApprovalStatus = (typeof APPROVAL_STATUSES)[number];
export type Decision = z.input<typeof DecisionSchema>;
export type DecisionResult = z.infer<typeof DecisionResultSchema>;
export type AuditEvent = z.infer<typeof AuditEventSchema>;
export type AuditPage = z.infer<typeof AuditPageSchema>;
export type AuditVerify = z.infer<typeof AuditVerifySchema>;
export type Costs = z.infer<typeof CostsSchema>;
export type Brief = z.infer<typeof BriefSchema>;
export type Whoami = z.infer<typeof WhoamiSchema>;
export type AccountStatus = z.infer<typeof AccountStatusSchema>;
export type Capacity = z.infer<typeof CapacitySchema>;
export type IntakeRequest = z.input<typeof IntakeRequestSchema>;
export type IntakeResult = z.infer<typeof IntakeResultSchema>;
export type DeviceRegistration = z.input<typeof DeviceRegistrationSchema>;
export type DeviceResult = z.infer<typeof DeviceResultSchema>;
