<script lang="ts">
  // Renders the live toast queue (toast.svelte.ts) bottom-right. Owns no
  // state of its own — push/dismiss/auto-dismiss all live in the store;
  // this is purely the stack's visual shell plus the manual dismiss button.
  import { dismiss, toasts, type Toast } from "../lib/toast.svelte";
</script>

<!--
  Two always-mounted live regions, one per severity, instead of one shared
  container: nesting a per-toast `role="alert"` inside a persistent
  `aria-live="polite"` wrapper (the previous approach) is a documented ARIA
  anti-pattern — a newly-inserted node that sits inside two live regions at
  once can get announced twice, once per region. Splitting by kind means
  each toast lives inside exactly one live region for its whole life, so it
  is announced exactly once:

  - success toasts render inside a `role="status"` (`aria-live="polite"`)
    region, mounted before any toast ever exists so a screen reader has
    already registered it and reliably picks up content changing inside it.
  - error toasts render inside a `role="alert"` region — assertive by
    implication of the role itself, no explicit `aria-live` needed —
    likewise always mounted rather than attached per-toast.

  Toast items themselves carry no `role`/`aria-live` of their own: the
  region they land in already supplies it, and giving both the region and
  the item their own would just recreate the double-announcement problem
  this split exists to fix. Both regions sit in the same fixed-position
  wrapper so the two severities still read as one visually stacked list; an
  empty region collapses (`.toasts__region:empty`) so it doesn't leave a
  gap when only one kind of toast is present.
-->
<div class="toasts">
  <div
    class="toasts__region"
    role="status"
    aria-live="polite"
    aria-atomic="false"
  >
    {#each toasts.filter((item) => item.kind === "success") as item (item.id)}
      {@render toastItem(item)}
    {/each}
  </div>
  <div class="toasts__region" role="alert" aria-atomic="false">
    {#each toasts.filter((item) => item.kind === "error") as item (item.id)}
      {@render toastItem(item)}
    {/each}
  </div>
</div>

{#snippet toastItem(item: Toast)}
  <div class="toast toast--{item.kind}">
    <div class="toast__body">
      <p class="toast__title">{item.title}</p>
      {#if item.body}
        <p class="toast__detail">{item.body}</p>
      {/if}
    </div>
    <button
      type="button"
      class="toast__dismiss"
      aria-label="Dismiss notification"
      onclick={() => dismiss(item.id)}
    >
      &times;
    </button>
  </div>
{/snippet}

<style>
  .toasts {
    position: fixed;
    right: var(--space-lg);
    bottom: var(--space-lg);
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    width: min(22rem, calc(100vw - 2 * var(--space-lg)));
    z-index: 200;
  }

  .toasts__region {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
  }

  .toasts__region:empty {
    display: none;
  }

  .toast {
    display: flex;
    align-items: flex-start;
    gap: var(--space-sm);
    padding: var(--space-sm) var(--space-md);
    border-radius: var(--radius);
    border: 1px solid var(--border);
    border-left: 4px solid var(--ok);
    background: var(--panel);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
  }

  .toast--error {
    border-left-color: var(--fail);
  }

  .toast__body {
    flex: 1;
    min-width: 0;
  }

  .toast__title {
    margin: 0;
    color: var(--ink);
    font-weight: 600;
  }

  .toast__detail {
    margin: var(--space-2xs) 0 0;
    color: var(--muted);
    font-size: 0.875rem;
  }

  .toast__dismiss {
    padding: 0;
    border: none;
    background: none;
    color: var(--muted);
    font-size: 1.1rem;
    line-height: 1;
    cursor: pointer;
  }

  .toast__dismiss:hover {
    color: var(--ink);
  }
</style>
