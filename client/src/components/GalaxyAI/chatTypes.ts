import type { ActionSuggestion, AgentResponse } from "@/composables/agentActions";

/** A step-level progress update streamed from the backend during an agent turn.
 * Mirrors `galaxy.schema.agents.AgentProgressEvent`. */
export interface AgentProgressEvent {
    step: string;
    label: string;
    status: "start" | "done" | "skip" | "error";
    agent_type: string;
    detail?: string | null;
}

export interface ChatMessage {
    id: string;
    role: "user" | "assistant";
    content: string;
    timestamp: Date;
    agentType?: string;
    confidence?: string;
    feedback?: "up" | "down" | null;
    agentResponse?: AgentResponse;
    suggestions?: ActionSuggestion[];
    isSystemMessage?: boolean;
}

export interface ChatHistoryItem {
    id: string;
    query: string;
    response: string;
    agent_type: string;
    agent_response?: AgentResponse;
    timestamp: string;
    feedback?: number | null;
    message_count?: number;
}
