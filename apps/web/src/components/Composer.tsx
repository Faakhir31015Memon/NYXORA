import { useState } from "react";

import { useStore } from "../state/store";

export function Composer(): React.JSX.Element {
  const { state, send, cancelStream } = useStore();
  const [text, setText] = useState("");
  const [useDocuments, setUseDocuments] = useState(true);
  const [useMemory, setUseMemory] = useState(true);
  const [model, setModel] = useState<string>("");

  const disabled = state.running || text.trim().length === 0;

  async function submit(): Promise<void> {
    const message = text.trim();
    if (!message) return;
    setText("");
    await send({
      message,
      conversation_id: state.conversationId,
      model: model || null,
      use_documents: useDocuments,
      use_memory: useMemory,
    });
  }

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <textarea
        value={text}
        placeholder="Ask, or describe a task. Ctrl+Enter sends."
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.ctrlKey && event.key === "Enter") {
            event.preventDefault();
            void submit();
          }
        }}
      />
      <div className="row">
        <div className="toggles">
          <label>
            <input
              type="checkbox"
              checked={useDocuments}
              onChange={(event) => setUseDocuments(event.target.checked)}
            />
            my documents
          </label>
          <label>
            <input
              type="checkbox"
              checked={useMemory}
              onChange={(event) => setUseMemory(event.target.checked)}
            />
            memory
          </label>
        </div>

        <select
          value={model}
          onChange={(event) => setModel(event.target.value)}
          aria-label="Preferred model"
          style={{ width: "auto" }}
        >
          <option value="">let the router decide</option>
          {state.models.map((entry) => (
            <option key={`${entry.provider}:${entry.name}`} value={entry.name}>
              {entry.provider}:{entry.name}
            </option>
          ))}
        </select>

        <span className="spacer" style={{ flex: 1 }} />

        {state.running ? (
          <button type="button" onClick={cancelStream}>
            Stop this answer
          </button>
        ) : null}
        <button className="primary" type="submit" disabled={disabled}>
          {state.running ? "Working…" : "Send"}
        </button>
      </div>
      <p className="muted">
        Toggling documents or memory only controls what gets injected into this prompt; the
        inspector shows exactly what was used.
      </p>
    </form>
  );
}
