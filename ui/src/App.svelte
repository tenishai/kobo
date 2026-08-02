<script lang="ts">
  // Placeholder shell — later tasks replace this with the agent list/detail
  // views, wired up through src/lib/api.ts.
</script>

<main class="shell">
  <header class="shell__header">
    <h1>cortex</h1>
    <p class="muted">agent control plane — scaffold in progress</p>
  </header>
</main>

<style>
  .shell {
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-xl);
  }

  .shell__header h1 {
    margin: 0;
    color: var(--ink);
    font-weight: 600;
  }

  .muted {
    margin: var(--space-2xs) 0 0;
    color: var(--muted);
  }
</style>
