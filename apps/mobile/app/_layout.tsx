import { useEffect } from "react";
import { ActivityIndicator, View } from "react-native";
import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { SessionProvider, useSession } from "../src/SessionContext";
import { configureNotificationHandler } from "../src/push";
import { usePalette } from "../src/theme";

function RootNavigator() {
  const { status } = useSession();
  const p = usePalette();
  if (status === "loading") {
    return (
      <View style={{ flex: 1, alignItems: "center", justifyContent: "center", backgroundColor: p.bg }}>
        <ActivityIndicator accessibilityLabel="Loading" />
      </View>
    );
  }
  const ready = status === "ready";
  return (
    <Stack
      screenOptions={{
        headerStyle: { backgroundColor: p.card },
        headerTintColor: p.text,
        contentStyle: { backgroundColor: p.bg },
      }}
    >
      <Stack.Protected guard={ready}>
        <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
        <Stack.Screen name="approval/[id]" options={{ title: "Approval" }} />
      </Stack.Protected>
      <Stack.Protected guard={!ready}>
        <Stack.Screen name="sign-in" options={{ headerShown: false }} />
      </Stack.Protected>
    </Stack>
  );
}

export default function RootLayout() {
  useEffect(() => {
    configureNotificationHandler();
  }, []);
  return (
    <SessionProvider>
      <StatusBar style="auto" />
      <RootNavigator />
    </SessionProvider>
  );
}
