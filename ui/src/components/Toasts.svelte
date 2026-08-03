<script lang="ts">
  // Renders the live toast queue (toast.svelte.ts) bottom-right. Owns no
  // state of its own — push/dismiss/auto-dismiss all live in the store;
  // this is purely the stack's visual shell plus the manual dismiss button.
  import { dismiss, toasts } from "../lib/toast.svelte";
</script>

<!--
  The wrapper itself is the live region — always mounted (from before any
  toast ever exists), carrying `aria-live` so assistive tech registers it up
  front. A screen reader reliably announces *content changing inside an
  already-registered live region*, but is inconsistent about announcing a
  brand-new node that arrives with its own role already attached (which is
  what the old per-toast `role="status"` did — nothing existed to announce
  from until the first toast, and that first toast's own arrival was the
  thing that needed announcing).

  Error toasts additionally carry `role="alert"` themselves: unlike
  `status`, `alert` is specified for exactly this on-demand/interrupting
  case, so a dynamically-inserted alert node doesn't need to pre-exist to be
  announced — it layers assertive severity on top of the container's polite
  default rather than needing a persistent container of its own.
-->
<div class="toasts" aria-live="polite" aria-atomic="false">
  {#each toasts as item (item.id)}
    <div
      class="toast toast--{item.kind}"
      role={item.kind === "error" ? "alert" : undefined}
    >
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
  {/each}
</div>

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
