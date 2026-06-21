<script setup lang="ts">
import { faCheck, faCircleNotch } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/vue-fontawesome";

import type { AgentProgressEvent } from "./chatTypes";

const props = defineProps<{
    steps: AgentProgressEvent[];
    /** Live (in-flight) turn: show a spinner for the running step. When false the
     * steps belong to a finished message, so every step renders as done. */
    live?: boolean;
}>();

function isDone(step: AgentProgressEvent): boolean {
    return !props.live || step.status === "done";
}
</script>

<template>
    <div class="progress-steps">
        <div
            v-for="step in steps"
            :key="step.step"
            class="progress-step"
            :class="{ 'progress-step-done': isDone(step) }">
            <div class="progress-step-row">
                <FontAwesomeIcon
                    :icon="isDone(step) ? faCheck : faCircleNotch"
                    :spin="!isDone(step)"
                    fixed-width
                    class="progress-step-icon" />
                <span class="progress-step-label">{{ step.label }}</span>
            </div>
            <!-- Intermediate artifact (produced YAML, critique, container pick) streamed
                 over SSE. Collapsed by default so it doesn't crowd the step list. -->
            <details v-if="step.detail" class="progress-step-detail">
                <summary>Show details</summary>
                <pre>{{ step.detail }}</pre>
            </details>
        </div>
    </div>
</template>

<style scoped lang="scss">
@import "@/style/scss/theme/blue.scss";

.progress-steps {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    padding-top: 0.125rem;
}

.progress-step {
    font-size: 0.85rem;
    color: $text-color;
    animation: fadeIn 0.2s ease-out;
}

.progress-step-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
}

.progress-step-icon {
    color: $brand-primary;
    font-size: 0.75rem;
}

.progress-step-done {
    color: $text-muted;

    .progress-step-icon {
        color: $brand-success;
    }
}

.progress-step-detail {
    // Indent under the step label so the disclosure lines up past the icon.
    margin-left: 1.25rem;
    margin-top: 0.2rem;

    summary {
        cursor: pointer;
        font-size: 0.8rem;
        color: $text-muted;
        user-select: none;
    }

    pre {
        margin: 0.3rem 0 0;
        padding: 0.5rem;
        max-height: 18rem;
        overflow: auto;
        font-size: 0.78rem;
        white-space: pre-wrap;
        word-break: break-word;
        background: $gray-100;
        border: 1px solid $border-color;
        border-radius: 0.25rem;
    }
}

@keyframes fadeIn {
    from {
        opacity: 0;
        transform: translateY(4px);
    }
    to {
        opacity: 1;
        transform: translateY(0);
    }
}
</style>
