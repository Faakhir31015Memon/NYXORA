import { useStore } from "../state/store";

export function Rail(): React.JSX.Element {
  const { state, newConversation, loadConversation } = useStore();

  return (
    <nav className="rail" aria-label="Conversations">
      <section>
        <h2>Workspace</h2>
        <p className="muted" title="Every tool write is confined to this directory">
          {state.settings?.workspace_root ?? "…"}
        </p>
        <p className="muted">
          {state.settings?.data_stays_local
            ? "Data stays local"
            : "External models permitted by settings"}
        </p>
        <button className="primary" onClick={() => void newConversation()}>
          New chat
        </button>
      </section>

      <section>
        <h2>Conversations</h2>
        <ul className="list">
          {state.conversations.map((conversation) => (
            <li key={conversation.id}>
              <button
                aria-current={conversation.id === state.conversationId}
                onClick={() => void loadConversation(conversation.id)}
                title={conversation.title}
              >
                {conversation.title || "untitled"}
              </button>
            </li>
          ))}
          {state.conversations.length === 0 ? <li className="muted">none yet</li> : null}
        </ul>
      </section>

      <section>
        <h2>Agents</h2>
        <ul className="list">
          {state.agents.map((agent) => (
            <li key={agent.name}>
              <button
                disabled
                title={`${agent.description} — tools: ${agent.allowed_tools.join(", ")}`}
              >
                {agent.name} <span className="pill">{agent.status}</span>
              </button>
            </li>
          ))}
        </ul>
        <p className="muted">
          Interface only in V1: no agent runs without a plan you approve, step by step.
        </p>
      </section>
    </nav>
  );
}
