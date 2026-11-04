import { useCallback, useEffect, useState } from "react";

import { getToken, setToken } from "./api/client";
import { api } from "./api/client";
import { StoreProvider, useStore } from "./state/store";
import { ApprovalDialog } from "./components/ApprovalDialog";
import { CommandPalette } from "./components/CommandPalette";
import { Composer } from "./components/Composer";
import { Inspector } from "./components/Inspector";
import { Rail } from "./components/Rail";
import { Thread } from "./components/Thread";
import { TopBar } from "./components/TopBar";

const THEME_KEY = "nyxora.theme";

function useTheme(): [string, () => void] {
  const [theme, setTheme] = useState<string>(() => localStorage.getItem(THEME_KEY) ?? "dark");
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);
  const toggle = useCallback(() => setTheme(theme === "dark" ? "light" : "dark"), [theme]);
  return [theme, toggle];
}

/** The token never goes to any host but the API origin, and it is never in a URL. */
function Gate({ onReady }: { onReady: () => void }): React.JSX.Element {
  const [token, setLocal] = useState(getToken());
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  async function connect(): Promise<void> {
    setBusy(true);
    setProblem(null);
    setToken(token.trim());
    try {
      await api.health();
      await api.models();
      onReady();
    } catch (error) {
      setProblem(
        error instanceof Error
          ? `${error.message} - the API may be down, or this token is not valid for it.`
          : "could not reach the API"
      );
      setToken("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="gate">
      <form
        className="panel"
        onSubmit={(event) => {
          event.preventDefault();
          void connect();
        }}
      >
        <h1 className="brand" style={{ fontSize: "var(--text-xl)" }}>
          NYXORA
        </h1>
        <p className="muted">
          Local-first assistant brain. Nothing here talks to a remote service unless you add a key
          and consent per request. Paste the <code>NYXORA_API_TOKEN</code> this API instance was
          started with to continue.
        </p>
        <label htmlFor="token">API token</label>
        <input
          id="token"
          type="password"
          value={token}
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => setLocal(event.target.value)}
          placeholder="from your .env"
        />
        {problem ? <p className="error">{problem}</p> : null}
        <button className="primary" type="submit" disabled={busy || token.trim().length === 0}>
          {busy ? "Checking…" : "Connect"}
        </button>
        <p className="muted">
          The token is kept in this browser profile only. Clearing site data removes it.
        </p>
      </form>
    </div>
  );
}

function Workspace(): React.JSX.Element {
  const store = useStore();
  const { refreshAll, pollApprovals, patch } = store;
  const paletteOpen = store.state.paletteOpen;
  const [theme, toggleTheme] = useTheme();

  // Only the stable callbacks may appear as dependencies: the store object is
  // rebuilt whenever state changes, so depending on it re-fires this effect on
  // every patch and turns the first load into an infinite refresh loop.
  useEffect(() => {
    void refreshAll();
    const timer = window.setInterval(() => void pollApprovals(), 15000);
    return () => window.clearInterval(timer);
  }, [refreshAll, pollApprovals]);

  useEffect(() => {
    function onKey(event: KeyboardEvent): void {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        patch({ paletteOpen: !paletteOpen });
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [patch, paletteOpen]);

  return (
    <div className="shell">
      <TopBar theme={theme} onToggleTheme={toggleTheme} />
      <div className="body">
        <Rail />
        <div className="thread">
          <Thread />
          <Composer />
        </div>
        <Inspector />
      </div>
      {store.state.openApproval ? <ApprovalDialog /> : null}
      {store.state.paletteOpen ? <CommandPalette /> : null}
    </div>
  );
}

export function App(): React.JSX.Element {
  const [ready, setReady] = useState(false);

  if (!ready) {
    return <Gate onReady={() => setReady(true)} />;
  }
  return (
    <StoreProvider>
      <Workspace />
    </StoreProvider>
  );
}
