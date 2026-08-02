<script lang="ts">
  // Presentation only: a colored dot + the raw status label. The color/pulse
  // mapping itself lives in status.ts (agentTone) so it's one shared,
  // unit-tested source of truth rather than duplicated per-component logic.
  import { agentTone } from "../lib/status";

  let { status }: { status: string } = $props();

  const tone = $derived(agentTone(status));
</script>

<span class="status-badge">
  <span class="status-badge__dot status-badge__dot--{tone}" class:pulse={tone === "warn"}></span>
  <span class="status-badge__label">{status}</span>
</span>

<style>
  .status-badge {
    display: inline-flex;
    align-items: center;
    gap: var(--space-xs);
  }

  .status-badge__dot {
    width: 0.55rem;
    height: 0.55rem;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .status-badge__dot--ok {
    background: var(--ok);
  }

  .status-badge__dot--fail {
    background: var(--fail);
  }

  .status-badge__dot--warn {
    background: var(--pulse);
  }

  .status-badge__label {
    color: var(--muted);
    font-size: 0.875rem;
    text-transform: capitalize;
  }
</style>
