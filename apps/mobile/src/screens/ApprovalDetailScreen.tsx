import { useState } from "react";
import { Linking, ScrollView, StyleSheet, Text, View } from "react-native";
import { formatTimestamp } from "@squadhq/shared-api";
import { safeHref } from "@squadhq/shared-api/markdown";
import { useApi, useSession } from "../SessionContext";
import { useAsync } from "../useAsync";
import { decideWithBiometrics } from "../decide";
import { usePalette } from "../theme";
import { Badge, Button, Card, ErrorBanner, Field } from "../components/ui";

export function ApprovalDetailScreen({ id, onDone }: { id: string; onDone?: () => void }) {
  const api = useApi();
  const { mode } = useSession();
  const p = usePalette();
  const state = useAsync(() => api.approval(id));
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  async function decide(decision: "approve" | "reject") {
    if (decision === "reject" && !comment.trim()) {
      setError("Add a comment explaining the rejection.");
      return;
    }
    setBusy(decision);
    setError(null);
    const res = await decideWithBiometrics(api, id, decision, comment, { demo: mode === "demo" });
    setBusy(null);
    if (res.ok) {
      setDone(res.result.approval.status);
      onDone?.();
    } else setError(res.error);
  }

  const a = state.data;
  const href = a ? safeHref(a.url) : null;
  return (
    <ScrollView style={{ backgroundColor: p.bg }} contentContainerStyle={s.root}>
      <ErrorBanner message={state.error ?? error} />
      {a ? (
        <>
          <Card>
            <Badge text={a.kind} />
            <Text accessibilityRole="header" style={[s.title, { color: p.text }]}>
              {a.title || a.subject}
            </Text>
            <Text style={[s.meta, { color: p.muted }]}>
              {a.project} · {a.subject} · {formatTimestamp(a.created_at)}
            </Text>
            {Object.entries(a.data).map(([k, v]) => (
              <Text key={k} style={[s.meta, { color: p.text }]}>
                {k}: {typeof v === "string" || typeof v === "number" ? String(v) : JSON.stringify(v)}
              </Text>
            ))}
            {href ? (
              <Text accessibilityRole="link" style={[s.link, { color: p.primary }]} onPress={() => void Linking.openURL(href)}>
                Open in tracker
              </Text>
            ) : null}
          </Card>
          {done ? (
            <Text accessibilityRole="alert" style={[s.done, { color: p.success }]} testID="decided">
              Recorded: {done}
            </Text>
          ) : a.status !== "pending" ? (
            <Text style={[s.done, { color: p.muted }]}>Already {a.status}.</Text>
          ) : (
            <>
              <Field
                label="Comment"
                value={comment}
                onChangeText={setComment}
                multiline
                maxLength={4000}
                hint="Required when rejecting. Recorded in the audit log."
                testID="comment"
              />
              <View style={s.actions}>
                <View style={s.flex}>
                  <Button
                    label="Approve"
                    busy={busy === "approve"}
                    disabled={busy !== null}
                    accessibilityHint="Requires biometric confirmation"
                    onPress={() => void decide("approve")}
                    testID="approve"
                  />
                </View>
                <View style={s.flex}>
                  <Button
                    label="Reject"
                    variant="danger"
                    busy={busy === "reject"}
                    disabled={busy !== null}
                    accessibilityHint="Requires biometric confirmation"
                    onPress={() => void decide("reject")}
                    testID="reject"
                  />
                </View>
              </View>
            </>
          )}
        </>
      ) : null}
    </ScrollView>
  );
}

const s = StyleSheet.create({
  root: { padding: 14 },
  title: { fontSize: 20, fontWeight: "700", marginTop: 8 },
  meta: { fontSize: 14, marginTop: 4 },
  link: { fontSize: 16, marginTop: 10, textDecorationLine: "underline", paddingVertical: 8 },
  actions: { flexDirection: "row", gap: 10 },
  flex: { flex: 1 },
  done: { fontSize: 16, fontWeight: "600", textAlign: "center", padding: 16 },
});
