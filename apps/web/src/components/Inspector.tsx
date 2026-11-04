import { useState } from "react";

import { api } from "../api/client";
import { useStore } from "../state/store";

export function Inspector(): React.JSX.Element {
  const { state, patch } = useStore();
  const tabs = ["context", "activity", "memory", "docs"] as const;

  return (
    <aside className="inspector" aria-label="Inspector">
      <div className="tabs" role="tablist">
        {tabs.map((tab) => (
          <button
            key={tab}
            role="tab"
            aria-selected={state.inspectorTab === tab}
            onClick={() => patch({ inspectorTab: tab })}
          >
            {tab}
          </button>
        ))}
      </div>
      {state.inspectorTab === "context" ? <ContextTab /> : null}
      {state.inspectorTab === "activity" ? <ActivityTab /> : null}
      {state.inspectorTab === "memory" ? <MemoryTab /> : null}
      {state.inspectorTab === "docs" ? <DocsTab /> : null}
    </aside>
  );
}

function ContextTab(): React.JSX.Element {
  const { state } = useStore();
  const routing = state.routing;
  return (
    <>
      <section>
        <h2>Routing decision</h2>
        {routing ? (
          <div className="stack">
            <p className="muted">
              {routing.provider ?? "none"}:{routing.model ?? "unavailable"}
            </p>
            <p>{routing.reason}</p>
            {routing.fallbacks?.length ? (
              <p className="muted">fallbacks: {routing.fallbacks.join(", ")}</p>
            ) : null}
            {routing.setup_hint ? <div className="note mono">{routing.setup_hint}</div> : null}
          </div>
        ) : (
          <p className="muted">
            Nothing routed yet. Every choice the router makes is explainable and lands here.
          </p>
        )}
      </section>

      <section>
        <h2>Injected context</h2>
        <p className="muted">
          {state.sources.length
            ? `${state.sources.length} document chunk(s) went into this prompt.`
            : "No document chunks in this prompt."}
        </p>
        <ul className="list">
          {state.sources.map((source) => (
            <li key={`${source.doc_id}:${source.chunk_id}`}>
              <button
                disabled
                title={source.doc_id}
              >
                {source.filename}
                {source.page ? ` p.${source.page}` : ""} · score {source.score}
              </button>
            </li>
          ))}
        </ul>
        <p className="untrusted">
          Document and memory text is treated as untrusted data, never as instructions.
        </p>
      </section>

      <section>
        <h2>Ground rules</h2>
        <p className="muted">workspace: <span className="mono">{state.settings?.workspace_root}</span></p>
        <p className="muted">privacy: <span className="mono">{state.settings?.privacy_level}</span></p>
        <p className="muted">
          max steps per run: {state.settings?.max_steps_per_run} · approvals expire after{" "}
          {state.settings?.approval_expiry_s}s
        </p>
      </section>
    </>
  );
}

function ActivityTab(): React.JSX.Element {
  const { state, patch } = useStore();
  const [runId, setRunId] = useState<string>("");
  const [runStages, setRunStages] = useState<{ state: string; started_at: string }[]>([]);

  async function inspect(id: string): Promise<void> {
    if (!id) return;
    try {
      const detail = await api.run(id);
      setRunStages(detail.stages);
      patch({ notice: null });
    } catch (error) {
      patch({ notice: String(error) });
    }
  }

  const shown = runStages.length ? runStages : state.stages;

  return (
    <>
      <section>
        <h2>Stages of the last run</h2>
        <ul className="timeline">
          {shown.map((stage, index) => (
            <li key={`${stage.started_at}-${index}`}>
              <span>{stage.state}</span>
              <span className="muted mono">{new Date(stage.started_at).toLocaleTimeString()}</span>
            </li>
          ))}
          {shown.length === 0 ? <li className="muted">idle</li> : null}
        </ul>
        <div className="row" style={{ display: "flex", gap: "var(--s-2)", marginTop: "var(--s-2)" }}>
          <input
            value={runId}
            placeholder="run id"
            onChange={(event) => setRunId(event.target.value)}
            aria-label="Run id"
          />
          <button onClick={() => void inspect(runId)}>Inspect</button>
        </div>
      </section>

      <section>
        <h2>Audit trail</h2>
        <p className="muted">
          Append-only. File contents are stored as a length and hash, never as text.
        </p>
        <ul className="timeline">
          {state.audit.slice(0, 30).map((entry) => (
            <li key={entry.id}>
              <span>
                {entry.outcome}
                {entry.tool ? ` · ${entry.tool}` : ""}
              </span>
              <span className="muted mono">{new Date(entry.ts).toLocaleTimeString()}</span>
            </li>
          ))}
        </ul>
      </section>
    </>
  );
}

function MemoryTab(): React.JSX.Element {
  const { state, proposeMemory, confirmMemory, deleteMemory, patch } = useStore();
  const [draft, setDraft] = useState("");

  return (
    <section>
      <h2>Memory</h2>
      <p className="muted">
        Nothing is remembered without you confirming it, and anything secret-shaped is refused at
        the door.
      </p>
      <div className="row" style={{ display: "flex", gap: "var(--s-2)", marginBottom: "var(--s-3)" }}>
        <input
          value={draft}
          placeholder="remember that…"
          onChange={(event) => setDraft(event.target.value)}
          aria-label="New memory"
        />
        <button
          onClick={() => {
            const text = draft.trim();
            if (!text) return;
            void proposeMemory(text);
            setDraft("");
          }}
        >
          Propose
        </button>
      </div>

      <div className="chips">
        {state.memory.map((item) => (
          <div key={item.id} className={`chip ${item.state === "pending" ? "pending" : ""}`}>
            <span className="body">{item.content}</span>
            <span className="muted">
              {item.state} · {item.scope}
              {item.source ? ` · from ${item.source}` : ""}
            </span>
            <span className="actions">
              {item.state === "pending" ? (
                <button className="primary" onClick={() => void confirmMemory(item.id)}>
                  Confirm
                </button>
              ) : (
                <button
                  onClick={() =>
                    void patch({
                      notice: `memory ${item.id.slice(0, 8)} is ${item.state}`,
                    })
                  }
                >
                  {item.state}
                </button>
              )}
              <button className="danger" onClick={() => void deleteMemory(item.id)}>
                Forget
              </button>
            </span>
          </div>
        ))}
        {state.memory.length === 0 ? <p className="muted">No memories yet.</p> : null}
      </div>
    </section>
  );
}

function DocsTab(): React.JSX.Element {
  const { state, uploadDocument, deleteDocument, searchDocuments } = useStore();
  const [query, setQuery] = useState("");

  return (
    <>
      <section>
        <h2>Your documents</h2>
        <input
          type="file"
          accept=".pdf,.txt,.md"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void uploadDocument(file);
            event.target.value = "";
          }}
          aria-label="Upload document"
        />
        <ul className="list" style={{ marginTop: "var(--s-3)" }}>
          {state.documents.map((doc) => (
            <li key={doc.id} style={{ display: "flex", gap: "var(--s-2)" }}>
              <button disabled title={doc.id} style={{ flex: 1 }}>
                {doc.filename} · {(doc.size_bytes / 1024).toFixed(1)} KB
              </button>
              <button className="danger" onClick={() => void deleteDocument(doc.id)}>
                Delete
              </button>
            </li>
          ))}
          {state.documents.length === 0 ? <li className="muted">no documents</li> : null}
        </ul>
      </section>

      <section>
        <h2>Search them</h2>
        <div className="row" style={{ display: "flex", gap: "var(--s-2)" }}>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="BM25 over your chunks"
            aria-label="Search query"
          />
          <button onClick={() => void searchDocuments(query)}>Search</button>
        </div>
        {state.searchNote ? <div className="note">{state.searchNote}</div> : null}
        <div className="chips" style={{ marginTop: "var(--s-3)" }}>
          {state.searchHits.map((hit) => (
            <div className="chip" key={hit.chunk_id}>
              <span className="muted">
                {hit.filename}
                {hit.page ? ` p.${hit.page}` : ""} · score {hit.score}
              </span>
              <span className="body">{hit.excerpt}</span>
            </div>
          ))}
          {state.searchHits.length > 0 ? null : (
            <p className="muted">
              No results means no results - the assistant will not invent a citation for this.
            </p>
          )}
        </div>
      </section>
    </>
  );
}
