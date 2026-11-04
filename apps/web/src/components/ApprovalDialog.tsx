import { useEffect, useMemo, useState } from "react";

import { useStore } from "../state/store";
import { DiffView } from "./DiffView";

const MAX_PARAM_CHARS = 2000;

function shorten(value: unknown): string {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  if (text.length <= MAX_PARAM_CHARS) return text;
  return `${text.slice(0, MAX_PARAM_CHARS)}\n… ${text.length - MAX_PARAM_CHARS} more characters`;
}

export function ApprovalDialog(): React.JSX.Element | null {
  const { state, decide, patch, pollApprovals } = useStore();
  const approval = state.openApproval;
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    function onKey(event: KeyboardEvent): void {
      if (event.key === "Escape") {
        // Closing is not a decision: the approval stays pending and simply expires.
        patch({ openApproval: null });
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [patch]);

  const remaining = useMemo(() => {
    if (!approval) return 0;
    return Math.max(0, Math.floor((new Date(approval.expires_at).getTime() - now) / 1000));
  }, [approval, now]);

  if (!approval) return null;

  const diff = approval.diff;
  const expired = remaining === 0;

  return (
    <div className="modal" role="dialog" aria-modal="true" aria-label="Approval required">
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>
          {approval.tool} <span className={`pill ${approval.permission_class}`}>{approval.permission_class}</span>
        </h2>
        <p>{approval.preview}</p>
        {approval.risk_note ? <div className="note">{approval.risk_note}</div> : null}

        {diff ? (
          <>
            <h3 className="untrusted">Exact change being proposed</h3>
            <DiffView diff={diff} />
          </>
        ) : null}

        <h3 className="untrusted">Parameters this approval authorises</h3>
        <pre className="params mono">{shorten(approval.params)}</pre>

        <p className="muted mono" title="The approval is bound to a hash of these parameters; changing one byte invalidates it.">
          hash {approval.hash.slice(0, 16)}… · expires in {remaining}s
          {approval.run_id ? ` · run ${approval.run_id.slice(0, 8)}…` : " · not tied to a run"}
        </p>

        {expired ? (
          <p className="error">
            This approval expired. Nothing ran. Request the action again — an expired approval is
            never reusable.
          </p>
        ) : null}

        <div className="actions">
          <button
            className="primary"
            disabled={expired}
            onClick={() => void decide(approval.id, true, false)}
          >
            Approve once
          </button>
          <button
            disabled={expired}
            title="Allow this permission class for the rest of this run. Still audited, still never for deletions outside the workspace."
            onClick={() => void decide(approval.id, true, true)}
          >
            Allow for this run
          </button>
          <button
            className="danger"
            onClick={() => {
              void decide(approval.id, false, false);
              void pollApprovals();
            }}
          >
            Deny
          </button>
          <button onClick={() => patch({ openApproval: null })}>Close (no decision)</button>
        </div>
        <p className="muted">
          One approval executes one action with exactly these bytes. It cannot be replayed, and the
          tool refuses to run without consuming it.
        </p>
      </div>
    </div>
  );
}
