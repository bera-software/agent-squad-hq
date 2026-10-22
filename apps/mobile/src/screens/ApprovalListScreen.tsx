import { useState } from "react";
import { FlatList, Pressable, RefreshControl, StyleSheet, Text, View } from "react-native";
import { formatTimestamp, type Approval } from "@squadhq/shared-api";
import { useApi, useSession } from "../SessionContext";
import { useAsync } from "../useAsync";
import { decideWithBiometrics } from "../decide";
import { TOUCH, usePalette } from "../theme";
import { Badge, Button, Card, Empty, ErrorBanner } from "../components/ui";

export type ListKind = "approvals" | "escalations";

export function ApprovalListScreen({ kind, onOpen }: { kind: ListKind; onOpen?: (id: string) => void }) {
  const api = useApi();
  const { mode } = useSession();
  const p = usePalette();
  const state = useAsync(async () => {
    const all = await api.approvals({ status: "pending" });
    return all.filter((a) => (kind === "escalations" ? a.kind === "escalation" : a.kind !== "escalation"));
  });
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  async function acknowledge(a: Approval) {
    setBusyId(a.id);
    setActionError(null);
    const res = await decideWithBiometrics(api, a.id, "approve", "acknowledged", {
      demo: mode === "demo",
      prompt: `Acknowledge ${a.subject}`,
    });
    setBusyId(null);
    if (res.ok) state.setData((state.data ?? []).filter((x) => x.id !== a.id));
    else setActionError(res.error);
  }

  const items = state.data ?? [];
  return (
    <View style={[s.root, { backgroundColor: p.bg }]}>
      <ErrorBanner message={state.error ?? actionError} />
      <FlatList
        data={items}
        keyExtractor={(a) => a.id}
        refreshControl={<RefreshControl refreshing={state.loading} onRefresh={() => void state.reload()} />}
        ListEmptyComponent={
          state.loading ? undefined : <Empty text={kind === "escalations" ? "No open escalations." : "Nothing is waiting for you."} />
        }
        renderItem={({ item }) => {
          const severity = typeof item.data.severity === "string" ? item.data.severity : null;
          const body = (
            <>
              <View style={s.row}>
                <Badge text={item.kind} tone={item.kind === "escalation" ? "danger" : "muted"} />
                {severity ? <Badge text={severity} tone="warning" /> : null}
              </View>
              <Text style={[s.title, { color: p.text }]}>{item.title || item.subject}</Text>
              <Text style={[s.meta, { color: p.muted }]}>
                {item.project} · {item.subject} · {formatTimestamp(item.created_at)}
              </Text>
            </>
          );
          if (kind === "escalations") {
            return (
              <Card>
                {body}
                <View style={s.actions}>
                  <Button
                    label="Acknowledge"
                    busy={busyId === item.id}
                    accessibilityHint="Requires biometric confirmation"
                    onPress={() => void acknowledge(item)}
                    testID={`ack-${item.id}`}
                  />
                </View>
              </Card>
            );
          }
          return (
            <Pressable
              accessibilityRole="button"
              accessibilityLabel={`${item.kind}: ${item.title || item.subject}, ${item.project}`}
              accessibilityHint="Opens details to approve or reject"
              onPress={() => onOpen?.(item.id)}
              style={{ minHeight: TOUCH }}
              testID={`open-${item.id}`}
            >
              <Card>{body}</Card>
            </Pressable>
          );
        }}
        contentContainerStyle={s.list}
      />
    </View>
  );
}

const s = StyleSheet.create({
  root: { flex: 1 },
  list: { padding: 14 },
  row: { flexDirection: "row", gap: 6, marginBottom: 6 },
  title: { fontSize: 17, fontWeight: "600" },
  meta: { fontSize: 13, marginTop: 4 },
  actions: { marginTop: 10 },
});
