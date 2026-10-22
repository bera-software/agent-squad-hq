/**
 * Board columns derived from the flow engine states in config/flows.yml
 * (ticket-lifecycle and review-loop). The server reports `flow_state` as the
 * state name plus optional " +modifier" suffixes, e.g. "in_progress +blocked".
 */
import type { Ticket } from "./schemas";

export interface BoardColumn {
  id: string;
  label: string;
  /** Flow-state names (without modifiers) that land in this column. */
  states: readonly string[];
  /** Counts toward work in progress. */
  wip: boolean;
}

export const BOARD_COLUMNS: readonly BoardColumn[] = [
  { id: "intake", label: "Intake", states: ["new", "needs_shaping", "debt_proposal_created", "open"], wip: false },
  { id: "ready", label: "Ready", states: ["shaped", "accepted"], wip: false },
  { id: "design", label: "Design", states: ["needs_design"], wip: true },
  { id: "building", label: "Building", states: ["dispatched", "in_progress"], wip: true },
  {
    id: "review",
    label: "In review",
    states: ["in_review", "review_pending", "changes_requested", "approved"],
    wip: true,
  },
  { id: "human", label: "Needs human", states: ["needs_human"], wip: true },
  { id: "done", label: "Done", states: ["closed", "done", "merged"], wip: false },
];

export const OTHER_COLUMN: BoardColumn = { id: "other", label: "Other", states: [], wip: false };

export function baseFlowState(flowState: string): string {
  return flowState.split(" +")[0]?.trim() ?? "";
}

export function flowModifiers(flowState: string): string[] {
  return flowState
    .split(" +")
    .slice(1)
    .map((m) => m.trim())
    .filter(Boolean);
}

export function columnFor(ticket: Pick<Ticket, "flow_state" | "state">): BoardColumn {
  if (ticket.state === "closed") return BOARD_COLUMNS.find((c) => c.id === "done") ?? OTHER_COLUMN;
  const base = baseFlowState(ticket.flow_state || ticket.state);
  return BOARD_COLUMNS.find((c) => c.states.includes(base)) ?? OTHER_COLUMN;
}

export function groupByColumn(tickets: readonly Ticket[]): Map<string, Ticket[]> {
  const out = new Map<string, Ticket[]>();
  for (const c of [...BOARD_COLUMNS, OTHER_COLUMN]) out.set(c.id, []);
  for (const t of tickets) out.get(columnFor(t).id)?.push(t);
  return out;
}

export function wipByProject(tickets: readonly Ticket[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const t of tickets) {
    if (columnFor(t).wip) out[t.project] = (out[t.project] ?? 0) + 1;
  }
  return out;
}
