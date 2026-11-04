/** Wire types mirroring apps/api. Nothing here is trusted input to render. */

export type PermissionClass = "read" | "write" | "delete" | "external" | "control";

export interface ApiErrorBody {
  error?: { code: string; message: string; details?: unknown };
}

export interface Health {
  status: string;
  version: string;
  db: boolean;
  ollama: { configured: boolean; reachable: boolean; detail: string; latency_ms: number | null };
  models_available: string[];
  privacy_level: string;
  external_models_in_use: boolean;
  emergency_stopped: boolean;
  setup_hint: string | null;
}

export interface ModelInfo {
  provider: string;
  name: string;
  capabilities?: { tags?: string[]; context_window?: number };
  ram_hint_gb?: number | null;
  license?: string | null;
  enabled?: boolean;
}

export interface ModelsResponse {
  models: ModelInfo[];
  providers: string[];
  ollama_default: string;
}

export interface RoutingDecision {
  provider: string | null;
  model: string | null;
  reason: string;
  setup_hint?: string | null;
  fallbacks?: string[];
  task_type?: string;
  complexity?: string;
}

export interface Approval {
  id: string;
  run_id: string | null;
  tool: string;
  permission_class: PermissionClass;
  params: Record<string, unknown>;
  hash: string;
  preview: string;
  risk_note: string | null;
  diff: string | null;
  created_at: string;
  expires_at: string;
  consumed: boolean;
  decided: boolean;
  approved: boolean | null;
}

export interface ActionPreview {
  summary: string;
  diff: string | null;
  files: string[];
  permission_class: string;
  risk_note: string | null;
}

export interface PlanStep {
  id: string;
  description: string;
  tool: string | null;
  params: Record<string, unknown>;
  required_permission: PermissionClass;
  status: string;
}

export interface Plan {
  goal: string;
  steps: PlanStep[];
  generated_by: string | null;
  risks?: string[];
}

export interface ToolResult {
  ok: boolean;
  output?: string | null;
  error?: string | null;
  action_id?: string | null;
}

export interface Source {
  doc_id: string;
  filename: string;
  page: number | null;
  chunk_id: number;
  score: number;
}

export interface MemoryItem {
  id: string;
  content: string;
  state: "pending" | "active" | "disabled" | string;
  scope: string;
  project_id: string | null;
  source: string | null;
  created_at: string;
}

export interface ConversationSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface ChatMessage {
  id: number;
  role: string;
  content: string;
  model: string | null;
  provider: string | null;
  sources: Source[] | null;
  created_at: string;
}

export interface DocumentRow {
  id: string;
  filename: string;
  content_type: string | null;
  size_bytes: number;
  created_at: string;
}

export interface SearchHit {
  chunk_id: number;
  doc_id: string;
  filename: string;
  page: number | null;
  score: number;
  excerpt: string;
}

export interface AuditEntry {
  id: string;
  ts: string;
  actor: string;
  run_id: string | null;
  tool: string | null;
  sanitized_params: Record<string, unknown>;
  permission_class: string | null;
  approval_id: string | null;
  outcome: string;
  error: string | null;
}

export interface RunRow {
  id: string;
  state: string;
  summary: string | null;
  error: string | null;
  created_at: string;
}

export interface StageRow {
  state: string;
  started_at: string;
  detail: Record<string, unknown> | null;
}

export interface Presence {
  state: string;
  mode: string;
  microphone: { capturing: boolean; permission: string; raw_audio_retained: boolean };
  pending_approvals: number;
  privacy_badge: string;
  models_ready: boolean;
  ollama_reachable: boolean;
  collected_metrics_only: boolean;
}

export interface SettingsResponse {
  stored: Record<string, unknown>;
  workspace_root: string;
  privacy_level: string;
  max_steps_per_run: number;
  approval_expiry_s: number;
  data_stays_local: boolean;
}

export interface AgentRow {
  name: string;
  description: string;
  allowed_tools: string[];
  status: string;
  executable: boolean;
}

export type StreamEvent =
  | { kind: "status"; run_id: string; state: string; detail?: Record<string, unknown> }
  | { kind: "token"; run_id: string; text: string; final?: boolean }
  | { kind: "routing"; run_id?: string } & RoutingDecision
  | { kind: "approval_required"; run_id?: string; approval?: Approval; approval_id?: string }
  | { kind: "tool_result"; run_id?: string; action?: Record<string, unknown>; result?: ToolResult }
  | { kind: "source"; run_id?: string; sources: Source[] }
  | { kind: "memory_suggestion"; run_id?: string; content?: string; reason?: string }
  | { kind: "plan"; goal?: string; steps?: PlanStep[]; generated_by?: string | null }
  | { kind: "error"; code?: string; message?: string; run_id?: string }
  | { kind: "done"; run_id: string }
  | { kind: "unknown"; event: string; data: Record<string, unknown> };
