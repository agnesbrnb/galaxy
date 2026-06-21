/**
 * Session-scoped, in-memory cache of the step checklist for the most recent turn
 * of each chat exchange.
 *
 * The streamed progress steps (with their expandable artifacts) are *not*
 * persisted server-side, but in center/route mode `GalaxyAI.vue` is remounted
 * when the route changes to the freshly-created exchange id (the router-view is
 * keyed by the route), which refetches the conversation from the server and
 * would otherwise drop them. Stashing the steps here lets the rebuilt
 * conversation re-attach them to the answer so they stay available until the tab
 * is left (or the steps are explicitly forgotten).
 */
import type { AgentProgressEvent } from "./chatTypes";

const stepsByExchange = new Map<string, AgentProgressEvent[]>();

/** Remember the completed steps for `exchangeId` (no-op for an empty list). */
export function rememberSteps(exchangeId: string, steps: AgentProgressEvent[]): void {
    if (steps.length) {
        stepsByExchange.set(exchangeId, steps);
    }
}

/** Steps stashed for `exchangeId`, or undefined if none. */
export function recallSteps(exchangeId: string): AgentProgressEvent[] | undefined {
    return stepsByExchange.get(exchangeId);
}

/** Drop the stash for `exchangeId` (e.g. the conversation was cleared). */
export function forgetSteps(exchangeId: string): void {
    stepsByExchange.delete(exchangeId);
}
