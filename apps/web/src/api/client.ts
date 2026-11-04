import type {
  ActionPreview,
  AgentRow,
  Approval,
  AuditEntry,
  ChatMessage,
  ConversationSummary,
  DocumentRow,
  Health,
  MemoryItem,
  ModelsResponse,
  Presence,
  RunRow,
  SearchHit,
  SettingsResponse,
  StageRow,
  ToolResult,
} from "./types";

const TOKEN_KEY = "nyxora.token";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? "";
}

export function setToken(token: string): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

/** Every failure carries the API's own code, so the UI shows facts instead of guessing. */
export class RequestError extends Error {
  readonly code: string;
  readonly status: number;
  readonly details: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = "RequestError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function json<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("X-NYXORA-Token", token);
  if (init.body !== undefined && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...init, headers });
  if (response.status === 204) return undefined as T;
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    const record = (body ?? {}) as { error?: { code?: string; message?: string; details?: unknown } };
    const err = record.error;
    throw new RequestError(
      response.status,
      err?.code ?? `http_${response.status}`,
      err?.message ?? `${response.status} ${path}`,
      err?.details
    );
  }
  return body as T;
}

const post = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });

export interface ChatPayload {
  message: string;
  conversation_id?: string | null;
  model?: string | null;
  use_documents: boolean;
  use_memory: boolean;
}

export const api = {
  health: () => json<Health>("/health"),
  models: () => json<ModelsResponse>("/models"),
  refreshModels: () => json<{ discovered: string[]; yaml_overrides_applied: number }>(
    "/models/refresh",
    { method: "POST" }
  ),
  presence: () => json<Presence>("/assistant/presence"),
  settings: () => json<SettingsResponse>("/settings"),
  agents: () => json<{ agents: AgentRow[] }>("/agents"),
  audit: (limit = 60) =>
    json<{ entries: AuditEntry[]; append_only: boolean }>(`/audit-log?limit=${limit}`),

  conversations: () => json<{ conversations: ConversationSummary[] }>("/conversations"),
  conversation: (id: string) =>
    json<{ conversation: ConversationSummary; messages: ChatMessage[] }>(`/conversations/${id}`),
  createConversation: (title: string) =>
    json<{ conversation: ConversationSummary }>(
      "/conversations",
      post({ title, project_id: null })
    ),
  deleteConversation: (id: string) =>
    json<void>(`/conversations/${id}`, { method: "DELETE" }),

  tools: () => json<{ tools: { name: string; permission_class: string }[] }>("/tools"),
  requestTool: (name: string, params: Record<string, unknown>, runId?: string | null) =>
    json<{ auto_allowed?: boolean; result?: ToolResult; approval?: Approval; preview?: ActionPreview }>(
      `/tools/${name}/request`,
      post({ params, run_id: runId ?? null })
    ),

  approvals: () => json<{ approvals: Approval[] }>("/approvals"),
  decide: (id: string, approved: boolean, grantForRun = false) =>
    json<{ approval_id: string; approved: boolean; result: ToolResult | null }>(
      `/approvals/${id}/decide`,
      post({ approved, grant_for_run: grantForRun })
    ),
  undo: (actionId: string) =>
    json<{ ok: boolean; detail: unknown }>(`/actions/${actionId}/undo`, { method: "POST" }),

  stop: () => json<{ ok: boolean; cancelled_approvals: number; runs_cancelled: number }>("/stop", {
    method: "POST",
  }),
  resume: () =>
    json<{ ok: boolean; stopped: boolean }>("/resume", { method: "POST" }),

  documents: () => json<{ documents: DocumentRow[] }>("/documents"),
  uploadDocument: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return json<{ doc_id: string; filename: string; chunks: number }>("/documents/upload", {
      method: "POST",
      body: form,
    });
  },
  deleteDocument: (id: string) => json<{ ok: boolean }>(`/documents/${id}`, { method: "DELETE" }),
  searchDocuments: (query: string, limit = 5) =>
    json<{ query: string; results: SearchHit[]; confidence_note: string | null }>(
      "/documents/search",
      post({ query, limit })
    ),

  memory: () => json<{ memories: MemoryItem[] }>("/memory"),
  proposeMemory: (content: string, scope = "global") =>
    json<{ memory: MemoryItem; requires_confirmation: boolean }>("/memory", post({ content, scope })),
  confirmMemory: (id: string) =>
    json<{ memory: MemoryItem }>(`/memory/${id}/confirm`, { method: "POST" }),
  patchMemory: (id: string, patch: Record<string, unknown>) =>
    json<{ memory: MemoryItem }>(`/memory/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteMemory: (id: string) => json<{ ok: boolean }>(`/memory/${id}`, { method: "DELETE" }),

  runs: (limit = 20) => json<{ runs: RunRow[] }>(`/runs?limit=${limit}`),
  run: (id: string) =>
    json<{ run: RunRow; stages: StageRow[]; live: unknown }>(`/runs/${id}`),
};
