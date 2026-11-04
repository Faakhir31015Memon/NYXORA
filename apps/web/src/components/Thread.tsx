import { useEffect, useRef } from "react";

import { useStore } from "../state/store";
import { PlanCard } from "./PlanCard";

export function Thread(): React.JSX.Element {
  const { state, undo } = useStore();
  const bottom = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [state.messages.length, state.streaming, state.stage]);

  return (
    <div className="stream" aria-live="polite">
      {state.stopped ? (
        <div className="note">
          Emergency stop is active. No tool action can run, approvals are blocked, and this stays
          in force until you press Resume.
        </div>
      ) : null}

      {state.notice ? <div className="error" role="alert">{state.notice}</div> : null}

      {state.messages.map((message) => (
        <article key={message.id} className={`msg ${message.role}`}>
          <div className="who">
            {message.role}
            {message.model ? ` · ${message.provider ?? "?"}:${message.model}` : ""}
          </div>
          {/* Model and document text is untrusted: it is rendered as a text node only. */}
          <div className="content">{message.content}</div>
          {message.sources?.length ? (
            <p className="muted">
              sources: {message.sources.map((s) => `${s.filename}${s.page ? ` p.${s.page}` : ""} (${s.score})`).join(", ")}
            </p>
          ) : null}
        </article>
      ))}

      {state.plan ? <PlanCard goal={state.planGoal} steps={state.plan} /> : null}

      {state.streaming ? (
        <article className="msg assistant">
          <div className="who">assistant · {state.stage}</div>
          <div className="content">{state.streaming}</div>
        </article>
      ) : null}

      {state.lastResult ? (
        <section className="card">
          <h3>Last tool result</h3>
          <p className={state.lastResult.ok ? "muted" : "error"}>
            {state.lastResult.ok ? state.lastResult.output ?? "ok" : state.lastResult.error ?? "failed"}
          </p>
          {state.lastResult.action_id ? (
            <div className="row" style={{ display: "flex", gap: "var(--s-2)" }}>
              <button onClick={() => void undo(state.lastResult?.action_id ?? "")}>Undo</button>
              <span className="muted mono">{state.lastResult.action_id}</span>
            </div>
          ) : null}
        </section>
      ) : null}

      {state.messages.length === 0 && !state.streaming ? (
        <p className="muted">
          Ask something, or press Ctrl+K for the command palette. Reads run immediately; anything
          that writes, deletes or reaches outside this machine stops for your approval.
        </p>
      ) : null}

      <div ref={bottom} />
    </div>
  );
}
