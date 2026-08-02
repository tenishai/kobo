<script lang="ts">
  // Presentation only: a centered message for "there's nothing here yet",
  // with an optional heading and an optional slot for a call to action
  // (e.g. a "create your first agent" button) — callers that don't need one
  // just omit it.
  import type { Snippet } from "svelte";

  let {
    title,
    message,
    children,
  }: {
    title?: string;
    message: string;
    children?: Snippet;
  } = $props();
</script>

<div class="empty-state">
  {#if title}
    <p class="empty-state__title">{title}</p>
  {/if}
  <p class="empty-state__message">{message}</p>
  {#if children}
    <div class="empty-state__action">
      {@render children()}
    </div>
  {/if}
</div>

<style>
  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-sm);
    padding: var(--space-2xl) var(--space-lg);
    text-align: center;
    color: var(--muted);
  }

  .empty-state__title {
    margin: 0;
    color: var(--ink);
    font-weight: 600;
    font-size: 1.05rem;
  }

  .empty-state__message {
    margin: 0;
    max-width: 28rem;
  }

  .empty-state__action {
    margin-top: var(--space-xs);
  }
</style>
