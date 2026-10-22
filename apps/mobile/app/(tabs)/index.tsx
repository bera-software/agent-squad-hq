import { useRouter } from "expo-router";
import { ApprovalListScreen } from "../../src/screens/ApprovalListScreen";

export default function ApprovalsTab() {
  const router = useRouter();
  return <ApprovalListScreen kind="approvals" onOpen={(id) => router.push({ pathname: "/approval/[id]", params: { id } })} />;
}
