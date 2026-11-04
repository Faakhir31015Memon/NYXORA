import { getToken, RequestError } from "./client";
import type { StreamEvent } from "./types";

/** EventSource cannot POST, and /chat needs a body, so the frames are read here. */
export async function streamChat(
  payload: Record<string, unknown>,
  onEvent: (event: StreamEvent) => void,
  signal: AbortSignal
): Promise<void> {
  const response = await fetch("/chat", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(getToken() ? { "X-NYXORA-Token": getToken() } : {}),
    },
    body: JSON.stringify(payload),
    signal,
  });

  if (!response.ok) {
    let code = `http_${response.status}`;
    let message = `${response.status} /chat`;
    let details: unknown = null;
    try {
      const body = (await response.json()) as { error?: { code?: string; message?: string; details?: unknown } };
      code = body.error?.code ?? code;
      message = body.error?.message ?? message;
      details = body.error?.details ?? null;
    } catch {
      /* the body was not JSON; the status alone is still the truth */
    }
    throw new RequestError(response.status, code, message, details);
  }

  const reader = response.body?.getReader();
  if (!reader) throw new RequestError(500, "no_stream", "the browser gave us no response body");

  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut = buffer.indexOf("\n\n");
    while (cut >= 0) {
      const frame = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      dispatch(frame, onEvent);
      cut = buffer.indexOf("\n\n");
    }
  }
  if (buffer.trim()) dispatch(buffer, onEvent);
}

function dispatch(frame: string, onEvent: (event: StreamEvent) => void): void {
  let name = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) name = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (!dataLines.length) return;
  let data: Record<string, unknown>;
  try {
    data = JSON.parse(dataLines.join("\n")) as Record<string, unknown>;
  } catch {
    onEvent({ kind: "unknown", event: name, data: { raw: dataLines.join("\n") } });
    return;
  }
  if (name === "status") {
    onEvent({ ...(data as object), kind: "status", state: String(data.state ?? "") } as StreamEvent);
    return;
  }
  const known = [
    "token",
    "routing",
    "approval_required",
    "tool_result",
    "source",
    "memory_suggestion",
    "plan",
    "error",
    "done",
  ] as const;
  if ((known as readonly string[]).includes(name)) {
    onEvent({ ...(data as object), kind: name } as StreamEvent);
    return;
  }
  onEvent({ kind: "unknown", event: name, data });
}
