/**
 * Push notification registration. Sends the Expo push token to
 * POST /api/v1/devices so the server can push escalations and approvals.
 * Skips quietly on simulators, when permission is denied, or when the app has
 * no EAS projectId configured (app.json -> expo.extra.eas.projectId).
 */
import Constants from "expo-constants";
import * as Device from "expo-device";
import * as Notifications from "expo-notifications";
import { Platform } from "react-native";
import type { ApiClient } from "@squadhq/shared-api";

export type PushResult =
  | { registered: true; deviceId: string }
  | { registered: false; reason: "not_device" | "no_project_id" | "denied" | "unsupported" | "error" };

export function configureNotificationHandler(): void {
  Notifications.setNotificationHandler({
    handleNotification: async () => ({
      shouldShowBanner: true,
      shouldShowList: true,
      shouldPlaySound: false,
      shouldSetBadge: true,
    }),
  });
}

function projectId(): string | null {
  const extra = Constants.expoConfig?.extra as { eas?: { projectId?: string } } | undefined;
  const id = extra?.eas?.projectId ?? Constants.easConfig?.projectId;
  return id ? String(id) : null;
}

export async function registerForPush(api: ApiClient): Promise<PushResult> {
  if (Platform.OS !== "ios" && Platform.OS !== "android") return { registered: false, reason: "unsupported" };
  if (!Device.isDevice) return { registered: false, reason: "not_device" };
  const pid = projectId();
  if (!pid) return { registered: false, reason: "no_project_id" };
  try {
    if (Platform.OS === "android") {
      await Notifications.setNotificationChannelAsync("escalations", {
        name: "Escalations and approvals",
        importance: Notifications.AndroidImportance.HIGH,
      });
    }
    let perm = await Notifications.getPermissionsAsync();
    if (!perm.granted) perm = await Notifications.requestPermissionsAsync();
    if (!perm.granted) return { registered: false, reason: "denied" };
    const { data } = await Notifications.getExpoPushTokenAsync({ projectId: pid });
    const res = await api.registerDevice({
      expo_push_token: data,
      platform: Platform.OS,
      name: Device.deviceName ?? "",
      topics: ["approvals", "escalations"],
    });
    return { registered: true, deviceId: res.id };
  } catch {
    return { registered: false, reason: "error" };
  }
}
