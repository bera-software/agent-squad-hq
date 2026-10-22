import type { ApiClient, DecisionResult } from "@squadhq/shared-api";
import { confirmWithBiometrics } from "./biometric";
import { errorMessage } from "./useAsync";

export type DecideOutcome = { ok: true; result: DecisionResult } | { ok: false; error: string };

/**
 * Biometric gate, then POST /approvals/{id}/decision. The request is never
 * sent unless the user confirms on-device.
 */
export async function decideWithBiometrics(
  api: ApiClient,
  id: string,
  decision: "approve" | "reject",
  comment: string,
  opts: { demo?: boolean; prompt?: string } = {},
): Promise<DecideOutcome> {
  const gate = await confirmWithBiometrics(opts.prompt ?? `Confirm: ${decision}`, { allowUnavailable: !!opts.demo });
  if (!gate.ok) return { ok: false, error: gate.message };
  try {
    const result = await api.decide(id, { decision, comment: comment.trim() });
    return { ok: true, result };
  } catch (e) {
    const status = (e as { status?: number }).status;
    if (status === 409) return { ok: false, error: "Someone already decided this item." };
    return { ok: false, error: errorMessage(e) };
  }
}
