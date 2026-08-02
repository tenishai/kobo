<script lang="ts">
  // Renders the live toast queue (toast.svelte.ts) bottom-right. Owns no
  // state of its own — push/dismiss/auto-dismiss all live in the store;
  // this is purely the stack's visual shell plus the manual dismiss button.
  import { dismiss, toasts } from "../lib/toast.svelte";
</script>

<div class="toasts">
  {#each toasts as item (item.id)}
    <div
      class="toast toast--{item.kind}"
      role={item.kind === "error" ? "alert" : "status"}
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
