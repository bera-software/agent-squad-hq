import { RefreshControl, ScrollView, StyleSheet, Text } from "react-native";
import { formatTimestamp } from "@squadhq/shared-api";
import { useApi } from "../SessionContext";
import { useAsync } from "../useAsync";
import { usePalette } from "../theme";
import { Empty, ErrorBanner } from "../components/ui";
import { Markdown } from "../components/Markdown";

export function BriefScreen() {
  const api = useApi();
  const p = usePalette();
  const state = useAsync(() => api.latestBrief());
  return (
    <ScrollView
      style={{ backgroundColor: p.bg }}
      contentContainerStyle={s.root}
      refreshControl={<RefreshControl refreshing={state.loading} onRefresh={() => void state.reload()} />}
    >
      <ErrorBanner message={state.error} />
      {state.data ? (
        <>
          <Text style={[s.meta, { color: p.muted }]}>Generated {formatTimestamp(state.data.created_at)}</Text>
          <Markdown source={state.data.markdown} />
        </>
      ) : state.loading || state.error ? null : (
        <Empty text="No daily brief yet." />
      )}
    </ScrollView>
  );
}

const s = StyleSheet.create({ root: { padding: 16 }, meta: { fontSize: 13, marginBottom: 8 } });
