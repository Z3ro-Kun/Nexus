import AppShell from "./components/AppShell";
import { navigate, useRoute } from "./lib/router";
import ChatPage from "./pages/ChatPage";
import ObjectivePage from "./pages/ObjectivePage";
import RunPage from "./pages/RunPage";

export default function App() {
  const route = useRoute();

  if (route.page === "chat") {
    return <ChatPage initialConversationId={route.conversationId} />;
  }

  if (route.page === "objective") {
    return (
      <AppShell>
        <ObjectivePage />
      </AppShell>
    );
  }

  if (route.page === "run") {
    return (
      <AppShell wide>
        <RunPage key={route.runId} runId={route.runId} />
      </AppShell>
    );
  }

  return (
    <AppShell>
      <p className="text-sm text-dim">
        This page does not exist.{" "}
        <button
          type="button"
          className="text-accent underline-offset-2 hover:underline"
          onClick={() => navigate("/")}
        >
          Back to chat
        </button>
      </p>
    </AppShell>
  );
}
