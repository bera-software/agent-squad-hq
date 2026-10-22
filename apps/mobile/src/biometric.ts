/**
 * Biometric (or device passcode) confirmation gate for sensitive actions.
 * Approve / reject / acknowledge never reach the network unless this resolves ok.
 */
import * as LocalAuthentication from "expo-local-authentication";

export type GateResult = { ok: true } | { ok: false; reason: "unavailable" | "cancelled" | "failed"; message: string };

export interface GateOptions {
  /** Allow the action when the device has no biometrics or passcode at all (demo mode only). */
  allowUnavailable?: boolean;
}

export async function isDeviceSecured(): Promise<boolean> {
  try {
    const level = await LocalAuthentication.getEnrolledLevelAsync();
    return level !== LocalAuthentication.SecurityLevel.NONE;
  } catch {
    return false;
  }
}

export async function confirmWithBiometrics(promptMessage: string, opts: GateOptions = {}): Promise<GateResult> {
  const secured = await isDeviceSecured();
  if (!secured) {
    if (opts.allowUnavailable) return { ok: true };
    return {
      ok: false,
      reason: "unavailable",
      message: "Set up Face ID, Touch ID, fingerprint or a device passcode to approve from this device.",
    };
  }
  try {
    const res = await LocalAuthentication.authenticateAsync({
      promptMessage,
      cancelLabel: "Cancel",
      disableDeviceFallback: false,
    });
    if (res.success) return { ok: true };
    const cancelled = res.error === "user_cancel" || res.error === "system_cancel" || res.error === "app_cancel";
    return {
      ok: false,
      reason: cancelled ? "cancelled" : "failed",
      message: cancelled ? "Confirmation cancelled." : "Could not confirm your identity.",
    };
  } catch {
    return { ok: false, reason: "failed", message: "Could not confirm your identity." };
  }
}
