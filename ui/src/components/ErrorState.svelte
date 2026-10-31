<script lang="ts">
  // Presentation only: an error message plus a retry action. The caller
  // owns what "retry" means (refetch a query, resubmit a form, ...); this
  // component just renders the message and wires the button to it.
  //
  // One alert box (2026-10-31): a warning icon, the message, and Try again
  // at the right. Server messages often start in lower case, so the first
  // letter is capitalised for display only; the text itself is untouched.
  import { WarningCircle } from "phosphor-svelte";

  let {
    message,
    onretry,
  }: {
    message: string;
    onretry: () => void;
  } = $props();
</script>

<div class="error-state" role="alert">
  <WarningCircle class="error-state__icon" size={18} weight="fill" aria-hidden="true" />
  <p class="error-state__message">{message}</p>
  <button type="button" class="error-state__retry" onclick={onretry}>
    Try again
  </button>
</div>

<style>
  .error-state {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
    padding: var(--space-sm) var(--space-sm) var(--space-sm) var(--space-md);
    border: 1px solid color-mix(in srgb, var(--fail) 30%, var(--border));
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--fail) 5%, var(--panel));
  }

  .error-state :global(.error-state__icon) {
    flex-shrink: 0;
    color: var(--fail);
  }

  .error-state__message {
    flex: 1;
    min-width: 0;
    margin: 0;
    color: var(--ink);
    font-size: 0.875rem;
    line-height: 1.5;
  }

  .error-state__message::first-letter {
    text-transform: uppercase;
  }

  .error-state__retry {
    flex-shrink: 0;
    height: 2rem;
    padding: 0 var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-pill);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
    font-size: 0.82rem;
    font-weight: 500;
    cursor: pointer;
    transition: background 0.15s ease, border-color 0.15s ease;
  }

  .error-state__retry:hover {
    border-color: var(--ring);
    background: var(--accent-soft);
  }
</style>
