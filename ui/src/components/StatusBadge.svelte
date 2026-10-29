<script lang="ts">
  // Presentation only: a colored dot + the raw status label. The color/pulse
  // mapping itself lives in status.ts (agentTone) so it's one shared,
  // unit-tested source of truth rather than duplicated per-component logic.
  import type { AgentHealth } from "../lib/api";
  import { agentTone } from "../lib/status";

  let {
    status,
    health = null,
    updating = false,
  }: { status: string; health?: AgentHealth | null; updating?: boolean } = $props();

  // Ready says the setup finished. If the cluster says the pod is not
  // running today, that is the fact a person needs, so it wears the badge
  // instead: Down, in the fail tone, with the reason on the hover. When
  // this page itself pushed a change that is still rolling (`updating`,
  // from lib/delivery), the pod is down because of that, so the badge says
  // Updating in the working tone instead (issue #268). Health itself is
  // never altered: an agent that is up stays Ready either way.
  const down = $derived(status === "ready" && health?.state === "down");
  const rolling = $derived(down && updating);
  const tone = $derived(rolling ? "warn" : down ? "fail" : agentTone(status));
  const label = $derived(rolling ? "updating" : down ? "down" : status);
</script>

<span class="status-badge" title={down && !rolling ? (health?.reason ?? undefined) : undefined}>
  <span class="status-badge__dot status-badge__dot--{tone}" class:pulse={tone === "warn"}></span>
  <span class="status-badge__label">{label}</span>
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

  /* Amber, like every other in-between state in the room. The ring it
     pulses is --pulse; the dot itself is the status colour. */
  .status-badge__dot--warn {
    background: var(--warn);
  }

  .status-badge__label {
    color: var(--muted);
    font-size: 0.875rem;
    text-transform: capitalize;
  }
</style>
