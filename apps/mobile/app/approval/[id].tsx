import { useLocalSearchParams, useRouter } from "expo-router";
import { ApprovalDetailScreen } from "../../src/screens/ApprovalDetailScreen";

export default function ApprovalRoute() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const router = useRouter();
  return <ApprovalDetailScreen id={String(id)} onDone={() => setTimeout(() => router.back(), 900)} />;
}
