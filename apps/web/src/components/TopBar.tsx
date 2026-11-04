import { useStore } from "../state/store";

export function TopBar({
  theme,
  onToggleTheme,
}: {
  theme: string;
  onToggleTheme: () => void;
}): React.JSX.Element {
  const { state, emergencyStop, resumeAfterStop, patch } = useStore();
  const health = state.health;
  const stopped = state.stopped;

  return (
    <header className="topbar">
      <span className="brand">NYXORA</span>
      <button
        className="ghost"
        onClick={() => patch({ paletteOpen: true })}
        title="Command palette (Ctrl+K)"
      >
        ⌘K
      </button>

      <span
        className={`badge ${health?.external_models_in_use ? "warn" : "ok"}`}
        title={
          health?.external_models_in_use
            ? "an external model may be used when you consent per request"
            : "privacy level " + (health?.privacy_level ?? "unknown") + ": nothing leaves this machine"
        }
      >
        {health?.external_models_in_use ? "external model consent" : "on-device"}
      </span>

      <span
        className={`badge ${health && health.models_available.length ? "ok" : "warn"}`}
        title={health?.setup_hint ?? undefined}
      >
        {health ? `${health.models_available.length} model(s)` : "models unknown"}
      </span>

      <span className={`badge ${health?.ollama.reachable ? "ok" : "warn"}`}>
        {health?.ollama.reachable ? `ollama ${health.ollama.latency_ms ?? 0}ms` : "ollama down"}
      </span>

      <span className={`badge ${state.approvals.length ? "warn" : ""}`}>
        {state.approvals.length} pending approval(s)
      </span>

      <span className="spacer" />

      {state.stage !== "idle" ? <span className="pill">{state.stage}</span> : null}

      <button onClick={onToggleTheme} title="Toggle theme">
        {theme === "dark" ? "Light" : "Dark"}
      </button>

      {stopped ? (
        <button onClick={() => void resumeAfterStop()}>Resume</button>
      ) : (
        <button
          className="danger"
          onClick={() => void emergencyStop()}
          title="Cancel every pending approval and running task. Blocks all tool writes until you resume."
        >
          Emergency stop
        </button>
      )}
    </header>
  );
}
