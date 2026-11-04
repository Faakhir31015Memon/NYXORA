import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useReducer,
  useRef,
  type ReactNode,
} from "react";

import { api, RequestError, type ChatPayload } from "../api/client";
import { streamChat } from "../api/sse";
import type {
  AgentRow,
  Approval,
  AuditEntry,
  ChatMessage,
  ConversationSummary,
  DocumentRow,
  Health,
  MemoryItem,
  ModelInfo,
  PlanStep,
  Presence,
  RoutingDecision,
  SearchHit,
  SettingsResponse,
  Source,
  StageRow,
  StreamEvent,
  ToolResult,
} from "../api/types";

export interface UiState {
  health: Health | null;
  presence: Presence | null;
  settings: SettingsResponse | null;
  models: ModelInfo[];
  agents: AgentRow[];
  conversations: ConversationSummary[];
  conversationId: string | null;
  messages: ChatMessage[];
  approvals: Approval[];
  openApproval: Approval | null;
  memory: MemoryItem[];
  documents: DocumentRow[];
  searchHits: SearchHit[];
  searchNote: string | null;
  audit: AuditEntry[];
  stages: StageRow[];
  plan: PlanStep[] | null;
  planGoal: string | null;
  routing: RoutingDecision | null;
  sources: Source[];
  streaming: string;
  stage: string;
  runId: string | null;
  running: boolean;
  lastResult: ToolResult | null;
  stopped: boolean;
  notice: string | null;
  paletteOpen: boolean;
  inspectorTab: "context" | "activity" | "memory" | "docs";
}

const initial: UiState = {
  health: null,
  presence: null,
  settings: null,
  models: [],
  agents: [],
  conversations: [],
  conversationId: null,
  messages: [],
  approvals: [],
  openApproval: null,
  memory: [],
  documents: [],
  searchHits: [],
  searchNote: null,
  audit: [],
  stages: [],
  plan: null,
  planGoal: null,
  routing: null,
  sources: [],
  streaming: "",
  stage: "idle",
  runId: null,
  running: false,
  lastResult: null,
  stopped: false,
  notice: null,
  paletteOpen: false,
  inspectorTab: "context",
};

type Action = { type: "patch"; patch: Partial<UiState> };

function reducer(state: UiState, action: Action): UiState {
  return { ...state, ...action.patch };
}

/** Stream events fold into state here, never inside a component, so the mapping is testable. */
export function applyEvent(state: UiState, event: StreamEvent): Partial<UiState> {
  switch (event.kind) {
    case "status":
      return { stage: event.state, runId: event.run_id ?? state.runId };
    case "token":
      return { streaming: state.streaming + event.text };
    case "routing":
      return { routing: event };
    case "plan":
      return { plan: event.steps ?? [], planGoal: event.goal ?? null };
    case "source":
      return { sources: [...state.sources, ...event.sources] };
    case "tool_result":
      return { lastResult: event.result ?? state.lastResult };
    case "approval_required": {
      const approval = event.approval ?? null;
      return approval ? { openApproval: approval } : {};
    }
    case "error":
      return { notice: `${event.code ?? "run_failed"}: ${event.message ?? "no detail"}` };
    case "done":
      return { runId: event.run_id, stage: "done" };
    default:
      return {};
  }
}

interface Store {
  state: UiState;
  patch: (patch: Partial<UiState>) => void;
  refreshAll: () => Promise<void>;
  loadConversation: (id: string) => Promise<void>;
  newConversation: () => Promise<void>;
  send: (payload: ChatPayload) => Promise<void>;
  cancelStream: () => void;
  pollApprovals: () => Promise<void>;
  decide: (id: string, approved: boolean, grantForRun: boolean) => Promise<void>;
  runTool: (name: string, params: Record<string, unknown>) => Promise<void>;
  undo: (actionId: string) => Promise<void>;
  emergencyStop: () => Promise<void>;
  resumeAfterStop: () => Promise<void>;
  proposeMemory: (content: string) => Promise<void>;
  confirmMemory: (id: string) => Promise<void>;
  deleteMemory: (id: string) => Promise<void>;
  uploadDocument: (file: File) => Promise<void>;
  deleteDocument: (id: string) => Promise<void>;
  searchDocuments: (query: string) => Promise<void>;
  fail: (error: unknown) => void;
}

const StoreContext = createContext<Store | null>(null);

export function StoreProvider({ children }: { children: ReactNode }): React.JSX.Element {
  const [state, dispatch] = useReducer(reducer, initial);
  const abort = useRef<AbortController | null>(null);
  // Stream callbacks run outside React's update cycle, so they read the newest
  // state through a ref instead of closing over a stale snapshot.
  const stateRef = useRef(state);
  stateRef.current = state;

  const patch = useCallback((p: Partial<UiState>) => dispatch({ type: "patch", patch: p }), []);

  const fail = useCallback(
    (error: unknown) => {
      if (error instanceof RequestError) {
        patch({ notice: `${error.code}: ${error.message}`, running: false });
      } else {
        patch({ notice: String(error), running: false });
      }
    },
    [patch]
  );

  const refreshAll = useCallback(async () => {
    try {
      const [health, models, conversations, memory, documents, audit, approvals, settings, agents] =
        await Promise.all([
          api.health(),
          api.models(),
          api.conversations(),
          api.memory(),
          api.documents(),
          api.audit(40),
          api.approvals(),
          api.settings(),
          api.agents(),
        ]);
      patch({
        health,
        models: models.models,
        conversations: conversations.conversations,
        memory: memory.memories,
        documents: documents.documents,
        audit: audit.entries,
        approvals: approvals.approvals,
        settings,
        agents: agents.agents,
        stopped: health.emergency_stopped,
      });
    } catch (error) {
      fail(error);
    }
  }, [patch, fail]);

  const pollApprovals = useCallback(async () => {
    try {
      const { approvals } = await api.approvals();
      patch({ approvals });
    } catch (error) {
      fail(error);
    }
  }, [patch, fail]);

  const loadConversation = useCallback(
    async (id: string) => {
      try {
        const { messages } = await api.conversation(id);
        patch({
          conversationId: id,
          messages,
          streaming: "",
          plan: null,
          planGoal: null,
          sources: [],
          routing: null,
          stages: [],
        });
      } catch (error) {
        fail(error);
      }
    },
    [patch, fail]
  );

  const newConversation = useCallback(async () => {
    try {
      const { conversation } = await api.createConversation("New chat");
      patch({
        conversations: [conversation, ...state.conversations],
        conversationId: conversation.id,
        messages: [],
        streaming: "",
        plan: null,
        planGoal: null,
        sources: [],
        routing: null,
        stages: [],
      });
    } catch (error) {
      fail(error);
    }
  }, [patch, state.conversations, fail]);

  const send = useCallback(
    async (payload: ChatPayload) => {
      abort.current?.abort();
      const controller = new AbortController();
      abort.current = controller;
      patch({
        running: true,
        streaming: "",
        notice: null,
        stage: "created",
        plan: null,
        planGoal: null,
        sources: [],
        routing: null,
        lastResult: null,
        conversationId: payload.conversation_id ?? state.conversationId,
      });
      try {
        await streamChat({ ...payload, conversation_id: payload.conversation_id ?? state.conversationId }, (event) => {
          dispatch({ type: "patch", patch: applyEvent(stateRef.current, event) });
        }, controller.signal);
        const id = stateRef.current.conversationId;
        if (id) {
          const { messages } = await api.conversation(id);
          const { runs } = await api.runs(1);
          const latest = runs[0];
          const stages = latest ? await api.run(latest.id) : null;
          patch({
            messages,
            stages: stages?.stages ?? [],
            streaming: "",
            conversations: (await api.conversations()).conversations,
          });
        }
      } catch (error) {
        if (!controller.signal.aborted) fail(error);
      } finally {
        patch({ running: false });
        await pollApprovals();
      }
    },
    [patch, fail, pollApprovals, state.conversationId]
  );

  const cancelStream = useCallback(() => {
    abort.current?.abort();
    patch({ running: false, stage: "cancelled" });
  }, [patch]);

  const decide = useCallback(
    async (id: string, approved: boolean, grantForRun: boolean) => {
      try {
        const result = await api.decide(id, approved, grantForRun);
        patch({
          openApproval: null,
          lastResult: result.result ?? null,
          notice: approved
            ? result.result?.ok === false
              ? `tool reported: ${result.result.error ?? "failed"}`
              : null
            : "denied - nothing was executed",
        });
        await Promise.all([pollApprovals(), refreshAll()]);
      } catch (error) {
        fail(error);
      }
    },
    [patch, fail, pollApprovals, refreshAll]
  );

  const runTool = useCallback(
    async (name: string, params: Record<string, unknown>) => {
      try {
        const response = await api.requestTool(name, params);
        if (response.approval) {
          patch({ openApproval: response.approval });
          await pollApprovals();
          return;
        }
        patch({ lastResult: response.result ?? null });
        await refreshAll();
      } catch (error) {
        fail(error);
      }
    },
    [patch, fail, pollApprovals, refreshAll]
  );

  const undo = useCallback(
    async (actionId: string) => {
      try {
        await api.undo(actionId);
        patch({ notice: null, lastResult: null });
        await refreshAll();
      } catch (error) {
        fail(error);
      }
    },
    [patch, fail, refreshAll]
  );

  const emergencyStop = useCallback(async () => {
    try {
      const result = await api.stop();
      abort.current?.abort();
      patch({
        stopped: true,
        running: false,
        openApproval: null,
        notice: `emergency stop: ${result.cancelled_approvals} approval(s) cancelled, ${result.runs_cancelled} run(s) cancelled`,
      });
      await pollApprovals();
    } catch (error) {
      fail(error);
    }
  }, [patch, fail, pollApprovals]);

  const resumeAfterStop = useCallback(async () => {
    try {
      await api.resume();
      patch({ stopped: false, notice: null });
    } catch (error) {
      fail(error);
    }
  }, [patch, fail]);

  const proposeMemory = useCallback(
    async (content: string) => {
      try {
        await api.proposeMemory(content);
        patch({ memory: (await api.memory()).memories });
      } catch (error) {
        fail(error);
      }
    },
    [patch, fail]
  );

  const confirmMemory = useCallback(
    async (id: string) => {
      try {
        await api.confirmMemory(id);
        patch({ memory: (await api.memory()).memories });
      } catch (error) {
        fail(error);
      }
    },
    [patch, fail]
  );

  const deleteMemory = useCallback(
    async (id: string) => {
      try {
        await api.deleteMemory(id);
        patch({ memory: (await api.memory()).memories });
      } catch (error) {
        fail(error);
      }
    },
    [patch, fail]
  );

  const uploadDocument = useCallback(
    async (file: File) => {
      try {
        await api.uploadDocument(file);
        patch({ documents: (await api.documents()).documents });
      } catch (error) {
        fail(error);
      }
    },
    [patch, fail]
  );

  const deleteDocument = useCallback(
    async (id: string) => {
      try {
        await api.deleteDocument(id);
        patch({ documents: (await api.documents()).documents });
      } catch (error) {
        fail(error);
      }
    },
    [patch, fail]
  );

  const searchDocuments = useCallback(
    async (query: string) => {
      try {
        const result = await api.searchDocuments(query);
        patch({ searchHits: result.results, searchNote: result.confidence_note });
      } catch (error) {
        fail(error);
      }
    },
    [patch, fail]
  );

  const value = useMemo<Store>(
    () => ({
      state,
      patch,
      refreshAll,
      loadConversation,
      newConversation,
      send,
      cancelStream,
      pollApprovals,
      decide,
      runTool,
      undo,
      emergencyStop,
      resumeAfterStop,
      proposeMemory,
      confirmMemory,
      deleteMemory,
      uploadDocument,
      deleteDocument,
      searchDocuments,
      fail,
    }),
    [
      state,
      patch,
      refreshAll,
      loadConversation,
      newConversation,
      send,
      cancelStream,
      pollApprovals,
      decide,
      runTool,
      undo,
      emergencyStop,
      resumeAfterStop,
      proposeMemory,
      confirmMemory,
      deleteMemory,
      uploadDocument,
      deleteDocument,
      searchDocuments,
      fail,
    ]
  );

  return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>;
}

export function useStore(): Store {
  const store = useContext(StoreContext);
  if (!store) throw new Error("useStore must be used inside StoreProvider");
  return store;
}
