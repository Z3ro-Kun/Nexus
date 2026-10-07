import AppShell from "./components/AppShell";
import { navigate, useRoute } from "./lib/router";
import ObjectivePage from "./pages/ObjectivePage";
import RunPage from "./pages/RunPage";

export default function App() {
  const route = useRoute();
  return (
    <AppShell wide={route.page === "run"}>
      {route.page === "objective" && <ObjectivePage />}
      {route.page === "run" && <RunPage key={route.runId} runId={route.runId} />}
      {route.page === "not_found" && (
        <p className="text-sm text-dim">
          This page does not exist.{" "}
          <button type="button" className="text-accent underline-offset-2 hover:underline" onClick={() => navigate("/")}>
            Start a new objective
          </button>
        </p>
      )}
    </AppShell>
  );
}
