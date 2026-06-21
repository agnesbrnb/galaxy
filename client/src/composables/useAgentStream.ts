/**
 * Consume the `/api/chat/stream` Server-Sent Events endpoint.
 *
 * EventSource only supports GET, but the chat query needs a POST body, so we
 * drive the stream with `fetch` + a ReadableStream reader and parse the SSE
 * frames by hand. Mirrors the fetch conventions used elsewhere in the client
 * (`withPrefix`, `credentials: "same-origin"`) -- see `useNotificationSSE.ts`.
 *
 * The backend emits zero or more `event: progress` frames during the agent
 * turn, then exactly one `event: result` carrying a `ChatResponse`, then a
 * terminal `event: done`.
 */
import type { components } from "@/api/schema";
import { withPrefix } from "@/utils/redirect";

import type { AgentProgressEvent } from "@/components/GalaxyAI/chatTypes";

export type ChatStreamResponse = components["schemas"]["ChatResponse"];

export interface AgentStreamPayload {
    query: string;
    context?: string | null;
    exchange_id?: string | null;
    entity_context?: Record<string, unknown> | null;
    page_id?: string | null;
}

export interface AgentStreamCallbacks {
    onProgress?: (event: AgentProgressEvent) => void;
    onResult?: (result: ChatStreamResponse) => void;
}

/** Raised when the stream cannot be established or ends without a result, so
 * callers can fall back to the blocking `/api/chat` endpoint. AbortError from a
 * cancelled request is NOT wrapped in this -- it propagates as-is. */
export class AgentStreamError extends Error {
    constructor(message: string) {
        super(message);
        this.name = "AgentStreamError";
    }
}

/** Parse a single SSE frame and dispatch it to the callbacks. */
function dispatchFrame(frame: string, callbacks: AgentStreamCallbacks): void {
    let event = "message";
    const dataLines: string[] = [];
    for (const line of frame.split("\n")) {
        if (line.startsWith(":")) {
            continue; // comment / keepalive
        } else if (line.startsWith("event:")) {
            event = line.slice("event:".length).trim();
        } else if (line.startsWith("data:")) {
            dataLines.push(line.slice("data:".length).replace(/^ /, ""));
        }
    }
    if (dataLines.length === 0) {
        return;
    }
    const data = dataLines.join("\n");
    if (event === "progress") {
        callbacks.onProgress?.(JSON.parse(data) as AgentProgressEvent);
    } else if (event === "result") {
        callbacks.onResult?.(JSON.parse(data) as ChatStreamResponse);
    }
    // `done` carries no payload we act on -- the read loop ends naturally.
}

/**
 * POST a chat query and consume the streamed progress + result.
 *
 * Resolves once the stream completes. Throws {@link AgentStreamError} on a
 * failed HTTP request or a stream that ends without a result frame. Pass an
 * `AbortSignal` to cancel an in-flight turn (e.g. when the conversation
 * changes); cancellation rejects with the underlying AbortError.
 */
export async function streamAgentQuery(
    agentType: string,
    payload: AgentStreamPayload,
    callbacks: AgentStreamCallbacks,
    signal?: AbortSignal,
): Promise<void> {
    const url = withPrefix(`/api/chat/stream?agent_type=${encodeURIComponent(agentType)}`);
    const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        credentials: "same-origin",
        body: JSON.stringify(payload),
        signal,
    });

    if (!response.ok || !response.body) {
        throw new AgentStreamError(`chat stream request failed: ${response.status}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let gotResult = false;

    const wrappedCallbacks: AgentStreamCallbacks = {
        onProgress: callbacks.onProgress,
        onResult: (result) => {
            gotResult = true;
            callbacks.onResult?.(result);
        },
    };

    try {
        for (;;) {
            const { done, value } = await reader.read();
            if (done) {
                break;
            }
            buffer += decoder.decode(value, { stream: true });
            let sepIndex: number;
            while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
                const frame = buffer.slice(0, sepIndex);
                buffer = buffer.slice(sepIndex + 2);
                if (frame.trim()) {
                    dispatchFrame(frame, wrappedCallbacks);
                }
            }
        }
    } finally {
        await reader.cancel().catch(() => undefined);
    }

    if (!gotResult) {
        throw new AgentStreamError("chat stream ended without a result");
    }
}
