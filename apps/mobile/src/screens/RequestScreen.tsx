import { useState } from "react";
import { KeyboardAvoidingView, Platform, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { useApi } from "../SessionContext";
import { errorMessage, useAsync } from "../useAsync";
import { TOUCH, usePalette } from "../theme";
import { Button, ErrorBanner, Field } from "../components/ui";

/** "Build me…" intake: POST /api/v1/requests. Speech input is a planned follow-up. */
export function RequestScreen() {
  const api = useApi();
  const p = usePalette();
  const projects = useAsync(() => api.projects());
  const [project, setProject] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState<string | null>(null);

  async function submit() {
    setError(null);
    setSent(null);
    if (!project) return setError("Choose a project.");
    if (!title.trim()) return setError("Give the request a title.");
    setBusy(true);
    try {
      const res = await api.createRequest({ project, title: title.trim(), body: body.trim(), source: "mobile" });
      setSent(res.ticket_key ? `Created ${res.ticket_key}.` : `Request ${res.id} is ${res.status}.`);
      setTitle("");
      setBody("");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <KeyboardAvoidingView behavior={Platform.OS === "ios" ? "padding" : undefined} style={{ flex: 1, backgroundColor: p.bg }}>
      <ScrollView contentContainerStyle={s.root} keyboardShouldPersistTaps="handled">
        <ErrorBanner message={projects.error ?? error} />
        {sent ? (
          <Text accessibilityRole="alert" style={[s.ok, { color: p.success }]} testID="sent">
            {sent}
          </Text>
        ) : null}
        <Text style={[s.label, { color: p.text }]}>Project</Text>
        <View style={s.chips} accessibilityRole="radiogroup">
          {(projects.data ?? []).map((pr) => {
            const selected = pr.name === project;
            return (
              <Pressable
                key={pr.name}
                accessibilityRole="radio"
                accessibilityState={{ selected }}
                accessibilityLabel={pr.name}
                onPress={() => setProject(pr.name)}
                style={[s.chip, { borderColor: selected ? p.primary : p.border, backgroundColor: selected ? p.primary : p.card }]}
                testID={`project-${pr.name}`}
              >
                <Text style={{ color: selected ? p.onPrimary : p.text, fontWeight: "600" }}>{pr.name}</Text>
              </Pressable>
            );
          })}
        </View>
        <Field label="Title" value={title} onChangeText={setTitle} maxLength={256} placeholder="Build me…" testID="title" />
        <Field
          label="Details"
          value={body}
          onChangeText={setBody}
          multiline
          maxLength={20000}
          style={s.body}
          hint="Treated as untrusted input by the agents."
          testID="body"
        />
        <Button label="Send request" busy={busy} onPress={() => void submit()} testID="submit" />
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const s = StyleSheet.create({
  root: { padding: 16 },
  label: { fontSize: 15, fontWeight: "600", marginBottom: 6 },
  chips: { flexDirection: "row", flexWrap: "wrap", gap: 8, marginBottom: 14 },
  chip: { minHeight: TOUCH, paddingHorizontal: 14, borderWidth: 1, borderRadius: 999, justifyContent: "center" },
  body: { minHeight: 120, textAlignVertical: "top", paddingTop: 10 },
  ok: { fontSize: 16, fontWeight: "600", marginBottom: 12 },
});
