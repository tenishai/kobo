<script lang="ts">
  // Presentation only: an error message plus a retry action. The caller
  // owns what "retry" means (refetch a query, resubmit a form, ...); this
  // component just renders the message and wires the button to it.
  let {
    message,
    onretry,
  }: {
    message: string;
    onretry: () => void;
  } = $props();
</script>

<div class="error-state" role="alert">
  <p class="error-state__message">{message}</p>
  <button type="button" class="error-state__retry" onclick={onretry}>
    Try again
  </button>
</div>

<style>
  .error-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-sm);
    padding: var(--space-xl) var(--space-lg);
    text-align: center;
  }

  .error-state__message {
    margin: 0;
    max-width: 28rem;
    color: var(--fail);
  }

  .error-state__retry {
    padding: var(--space-xs) var(--space-md);
    border-radius: var(--radius-sm);
    border: 1px solid var(--border);
    background: var(--panel);
    color: var(--ink);
    cursor: pointer;
  }

  .error-state__retry:hover {
    border-color: var(--accent);
    color: var(--accent);
  }
</style>
