import { useEffect, useMemo, useState } from "react";

import { api } from "../api/client";
import { useStore } from "../state/store";

interface Command {
  id: string;
  label: string;
  hint: string;
  run: () => void;
}

export function CommandPalette(): React.JSX.Element {
  const { state, patch, newConversation, emergencyStop, resumeAfterStop, runTool, refreshAll } =
    useStore();
  const [filter, setFilter] = useState("");
  const [active, setActive] = useState(0);

  const commands = useMemo<Command[]>(() => {
    const close = (): void => patch({ paletteOpen: false });
    const list: Command[] = [
      {
        id: "new-chat",
        label: "New chat",
        hint: "start an empty conversation",
        run: () => {
          void newConversation();
          close();
        },
      },
      {
        id: "refresh",
        label: "Re-discover models",
        hint: "ask Ollama what is actually installed",
        run: () => {
          void api.refreshModels().then(refreshAll);
          close();
        },
      },
      {
        id: "list-files",
        label: "List workspace files",
        hint: "read-only tool, auto-allowed",
        run: () => {
          void runTool("fs.list", { path: "." });
          close();
        },
      },
      {
        id: "note",
        label: "Create note in workspace",
        hint: "write tool - stops for your approval",
        run: () => {
          void runTool("fs.create", {
            path: `nyxora-note-${Date.now()}.md`,
            content: "# Created from the command palette\n",
          });
          close();
        },
      },
      {
        id: "tab-context",
        label: "Show context inspector",
        hint: "what the model was actually given",
        run: () => {
          patch({ inspectorTab: "context" });
          close();
        },
      },
      {
        id: "tab-activity",
        label: "Show activity and audit trail",
        hint: "stages, runs, append-only log",
        run: () => {
          patch({ inspectorTab: "activity" });
          close();
        },
      },
      {
        id: "tab-memory",
        label: "Show memory",
        hint: "pending items need confirmation",
        run: () => {
          patch({ inspectorTab: "memory" });
          close();
        },
      },
      {
        id: "tab-docs",
        label: "Show documents",
        hint: "upload, search, delete",
        run: () => {
          patch({ inspectorTab: "docs" });
          close();
        },
      },
      {
        id: "stop",
        label: "Emergency stop",
        hint: "cancel every approval and running task",
        run: () => {
          void emergencyStop();
          close();
        },
      },
      {
        id: "resume",
        label: "Resume after emergency stop",
        hint: "explicitly re-allow tool execution",
        run: () => {
          void resumeAfterStop();
          close();
        },
      },
    ];
    if (!state.stopped) return list.filter((command) => command.id !== "resume");
    return list.filter((command) => command.id !== "stop");
  }, [
    state.stopped,
    patch,
    newConversation,
    emergencyStop,
    resumeAfterStop,
    runTool,
    refreshAll,
  ]);

  const matches = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (!needle) return commands;
    return commands.filter(
      (command) =>
        command.label.toLowerCase().includes(needle) || command.hint.toLowerCase().includes(needle)
    );
  }, [commands, filter]);

  useEffect(() => setActive(0), [filter]);

  return (
    <>
      <div
        className="modal"
        style={{ position: "fixed", inset: 0, background: "transparent", zIndex: 150 }}
        onClick={() => patch({ paletteOpen: false })}
      />
      <div className="palette" role="dialog" aria-label="Command palette">
        <input
          autoFocus
          value={filter}
          placeholder="Type a command…"
          onChange={(event) => setFilter(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              setActive((index) => Math.min(index + 1, matches.length - 1));
            } else if (event.key === "ArrowUp") {
              event.preventDefault();
              setActive((index) => Math.max(index - 1, 0));
            } else if (event.key === "Enter") {
              event.preventDefault();
              matches[active]?.run();
            } else if (event.key === "Escape") {
              patch({ paletteOpen: false });
            }
          }}
        />
        <ul>
          {matches.map((command, index) => (
            <li key={command.id} aria-selected={index === active}>
              <button onClick={command.run}>
                {command.label} <span className="muted">— {command.hint}</span>
              </button>
            </li>
          ))}
          {matches.length === 0 ? <li className="muted">no matching command</li> : null}
        </ul>
      </div>
    </>
  );
}
