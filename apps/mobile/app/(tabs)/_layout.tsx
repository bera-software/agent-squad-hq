import { useEffect } from "react";
import { Pressable, Text } from "react-native";
import { Tabs } from "expo-router";
import { useSession } from "../../src/SessionContext";
import { registerForPush } from "../../src/push";
import { TOUCH, usePalette } from "../../src/theme";

export default function TabsLayout() {
  const { api, mode, signOut } = useSession();
  const p = usePalette();

  useEffect(() => {
    if (api && mode === "server") void registerForPush(api);
  }, [api, mode]);

  return (
    <Tabs
      screenOptions={{
        headerStyle: { backgroundColor: p.card },
        headerTintColor: p.text,
        tabBarStyle: { backgroundColor: p.card, borderTopColor: p.border },
        tabBarActiveTintColor: p.primary,
        tabBarInactiveTintColor: p.muted,
        tabBarIconStyle: { display: "none" },
        tabBarLabelStyle: { fontSize: 13, fontWeight: "600" },
        headerRight: () => (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Sign out"
            onPress={() => void signOut()}
            style={{ minHeight: TOUCH, minWidth: TOUCH, justifyContent: "center", paddingHorizontal: 12 }}
          >
            <Text style={{ color: p.primary, fontSize: 15 }}>Sign out</Text>
          </Pressable>
        ),
      }}
    >
      <Tabs.Screen name="index" options={{ title: mode === "demo" ? "Approvals (demo)" : "Approvals", tabBarLabel: "Approvals" }} />
      <Tabs.Screen name="escalations" options={{ title: "Escalations" }} />
      <Tabs.Screen name="brief" options={{ title: "Daily brief", tabBarLabel: "Brief" }} />
      <Tabs.Screen name="runs" options={{ title: "Runs" }} />
      <Tabs.Screen name="request" options={{ title: "New request", tabBarLabel: "Request" }} />
    </Tabs>
  );
}
