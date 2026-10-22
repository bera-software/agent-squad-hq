import { useState } from "react";
import { KeyboardAvoidingView, Platform, ScrollView, StyleSheet, Text, View } from "react-native";
import { useSession } from "../SessionContext";
import { usePalette } from "../theme";
import { Button, ErrorBanner, Field } from "../components/ui";

export function SignInScreen() {
  const session = useSession();
  const p = usePalette();
  const [serverUrl, setServerUrl] = useState("");
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function run(fn: () => Promise<string | null | void>) {
    setBusy(true);
    setError(null);
    try {
      const err = await fn();
      if (err) setError(err);
    } finally {
      setBusy(false);
    }
  }

  if (session.status === "locked") {
    return (
      <View style={[s.root, { backgroundColor: p.bg }]}>
        <Text accessibilityRole="header" style={[s.title, { color: p.text }]}>
          Agent Squad HQ is locked
        </Text>
        <ErrorBanner message={error} />
        <Button label="Unlock" busy={busy} onPress={() => void run(session.unlock)} testID="unlock" />
        <View style={s.gap} />
        <Button label="Sign out" variant="secondary" onPress={() => void session.signOut()} />
      </View>
    );
  }

  return (
    <KeyboardAvoidingView behavior={Platform.OS === "ios" ? "padding" : undefined} style={{ flex: 1, backgroundColor: p.bg }}>
      <ScrollView contentContainerStyle={s.root} keyboardShouldPersistTaps="handled">
        <Text accessibilityRole="header" style={[s.title, { color: p.text }]}>
          Sign in to Agent Squad HQ
        </Text>
        <Text style={[s.sub, { color: p.muted }]}>
          Use your server URL and an API token with the approver role (create one with `squadhq server create-token`).
        </Text>
        <ErrorBanner message={error} />
        <Field
          label="Server URL"
          value={serverUrl}
          onChangeText={setServerUrl}
          placeholder="https://hq.example.com"
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
          textContentType="URL"
          testID="server-url"
        />
        <Field
          label="API token"
          value={token}
          onChangeText={setToken}
          secureTextEntry
          autoCapitalize="none"
          autoCorrect={false}
          textContentType="password"
          hint="Stored only in the device keychain / keystore."
          testID="token"
        />
        <Button label="Sign in" busy={busy} onPress={() => void run(() => session.signIn({ serverUrl, token }))} testID="sign-in" />
        <View style={s.gap} />
        <Button
          label="Try demo mode"
          variant="secondary"
          accessibilityHint="Explore the app with sample data; nothing is sent to a server"
          onPress={() => void run(session.startDemo)}
          testID="demo"
        />
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const s = StyleSheet.create({
  root: { flexGrow: 1, padding: 20, justifyContent: "center" },
  title: { fontSize: 26, fontWeight: "700", marginBottom: 8 },
  sub: { fontSize: 15, marginBottom: 18, lineHeight: 21 },
  gap: { height: 12 },
});
