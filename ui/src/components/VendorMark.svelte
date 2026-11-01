<script lang="ts">
  // One vendor's mark (lib/admin/logos): the brand path in its brand colour,
  // or initials on a tinted tile for a brand the icon set does not carry.
  // GitHub's hex is empty on purpose and draws in the ink colour, so it is
  // visible on the dark surface too. Drawn the same way everywhere a vendor
  // is named: the admin Integrations table, the Add integration dialog and
  // the agent's Access tab.
  import { vendorMark } from "../lib/admin/logos";

  let {
    id,
    name,
    size = 22,
  }: {
    /** The vendor id the catalogue uses (`github`, `aws`, `datadog`, ...). */
    id: string;
    /** The display name; its first letter is the fallback mark. */
    name: string;
    /** The tile's inner mark size in px; the tile itself scales with it. */
    size?: number;
  } = $props();

  const mark = $derived(vendorMark(id, name));
  const tile = $derived(`${Math.round(size * 1.6)}px`);
</script>

{#if mark.kind === "path"}
  <span class="vendor-mark" style={`--tile:${tile}`} aria-hidden="true">
    <svg viewBox="0 0 24 24" width={size} height={size} fill={mark.hex ? `#${mark.hex}` : "currentColor"}>
      <path d={mark.path} />
    </svg>
  </span>
{:else}
  <span
    class="vendor-mark vendor-mark--initials"
    style={`--tile:${tile};${mark.hex ? `background:${mark.hex};color:#fff;border-color:${mark.hex}` : ""}`}
    aria-hidden="true">{mark.text}</span
  >
{/if}

<style>
  .vendor-mark {
    display: inline-flex;
    flex-shrink: 0;
    align-items: center;
    justify-content: center;
    width: var(--tile);
    height: var(--tile);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
  }

  .vendor-mark--initials {
    background: var(--accent-soft);
    font-family: var(--font-display-mono);
    font-size: calc(var(--tile) * 0.34);
    font-weight: 700;
    letter-spacing: -0.02em;
  }
</style>
