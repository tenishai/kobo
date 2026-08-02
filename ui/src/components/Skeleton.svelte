<script lang="ts">
  // A generic loading placeholder: `rows` shimmering bars, standing in for
  // whatever content (a list, a detail panel) hasn't arrived yet. Purely
  // decorative — the accessible "loading" announcement is the one visible
  // string; screen readers should ignore the shimmer bars themselves.
  let { rows = 3 }: { rows?: number } = $props();
</script>

<div class="skeleton" role="status" aria-live="polite">
  <span class="skeleton__sr-only">Loading…</span>
  {#each { length: rows } as _, i (i)}
    <div class="skeleton__row" aria-hidden="true"></div>
  {/each}
</div>

<style>
  .skeleton {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
  }

  .skeleton__sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  .skeleton__row {
    height: 1rem;
    border-radius: var(--radius-sm);
    background: linear-gradient(
      90deg,
      var(--border) 25%,
      var(--panel) 50%,
      var(--border) 75%
    );
    background-size: 200% 100%;
    animation: skeleton-shimmer 1.6s ease-in-out infinite;
  }

  @keyframes skeleton-shimmer {
    0% {
      background-position: 200% 0;
    }
    100% {
      background-position: -200% 0;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .skeleton__row {
      animation: none;
    }
  }
</style>
