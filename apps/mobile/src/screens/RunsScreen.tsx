import { FlatList, RefreshControl, StyleSheet, Text, View } from "react-native";
import { formatDuration, formatTimestamp, formatUsd } from "@squadhq/shared-api";
import { useApi } from "../SessionContext";
import { useAsync } from "../useAsync";
import { usePalette } from "../theme";
import { Badge, Card, Empty, ErrorBanner } from "../components/ui";

export function RunsScreen() {
  const api = useApi();
  const p = usePalette();
  const state = useAsync(() => api.runs({ limit: 50 }));
  return (
    <View style={{ flex: 1, backgroundColor: p.bg }}>
      <ErrorBanner message={state.error} />
      <FlatList
        data={state.data ?? []}
        keyExtractor={(r) => r.id}
        contentContainerStyle={s.list}
        refreshControl={<RefreshControl refreshing={state.loading} onRefresh={() => void state.reload()} />}
        ListEmptyComponent={state.loading ? undefined : <Empty text="No runs yet." />}
        renderItem={({ item }) => (
          <View
            accessible
            accessibilityLabel={`${item.role} on ${item.project}, ${item.outcome}, ${formatUsd(item.cost_usd)}, ${formatDuration(item.duration_s)}`}
          >
            <Card>
              <View style={s.row}>
                <Text style={[s.title, { color: p.text }]}>
                  {item.role} · {item.project}
                </Text>
                <Badge
                  text={item.outcome}
                  tone={item.outcome === "success" ? "success" : item.outcome === "failure" ? "danger" : "muted"}
                />
              </View>
              <Text style={[s.meta, { color: p.muted }]}>
                {formatUsd(item.cost_usd)} · {formatDuration(item.duration_s)} · {item.model || "unknown model"}
              </Text>
              <Text style={[s.meta, { color: p.muted }]}>{formatTimestamp(item.started_at)}</Text>
            </Card>
          </View>
        )}
      />
    </View>
  );
}

const s = StyleSheet.create({
  list: { padding: 14 },
  row: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", gap: 8 },
  title: { fontSize: 16, fontWeight: "600", flexShrink: 1 },
  meta: { fontSize: 13, marginTop: 4 },
});
