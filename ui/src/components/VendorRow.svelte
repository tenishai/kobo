<script lang="ts">
  // One vendor on the Access tab (issue #382): a compact row, closed by
  // default, so the tab stays one screen as vendors multiply. Closed, it is
  // the mark, the name, one line that says where things stand, and the one
  // action on the right; no card chrome, a hairline under each row. Open,
  // the body underneath shows what the vendor does today. The row is a
  // disclosure, so it opens for a reader and for a frozen agent alike; the
  // controls inside are what the freeze bites on. Who is open is the
  // parent's (one at a time); this component only draws and reports clicks.
  import type { Snippet } from "svelte";
  import VendorMark from "./VendorMark.svelte";

  export type Tone = "ink" | "ok" | "warn" | "fail";

  let {
    id,
    label,
    summary,
    tone = "ink",
    title = null,
    meta = null,
    action = null,
    live = false,
    open,
    lit = false,
    onToggle,
    children,
  }: {
    /** The vendor id the catalogue uses, for the mark. */
    id: string;
    label: string;
    /** The one line, in ink: counts, a state, or the failure line. */
    summary: string;
    /** `ok`, `warn` and `fail` add a dot and colour the line; `ink` is plain. */
    tone?: Tone;
    /** The full text behind a trimmed summary, on hover. */
    title?: string | null;
    /** A grey detail after the summary (GitHub's repository count). */
    meta?: string | null;
    /** The one action, drawn on the right while closed; Close while open. */
    action?: string | null;
    /** True for a summary that changes on its own (Slack's state), so it is
     * announced. */
    live?: boolean;
    open: boolean;
    /** Brought into view and lit for a moment: the row a link pointed at. */
    lit?: boolean;
    onToggle: () => void;
    children: Snippet;
  } = $props();

  let el = $state<HTMLElement | null>(null);
  $effect(() => {
    if (lit && el) el.scrollIntoView({ block: "start", behavior: "smooth" });
  });
</script>

<section class="row" class:row--open={open} class:row--lit={lit} aria-label={label} bind:this={el}>
  <button type="button" class="row__head" aria-expanded={open} onclick={onToggle}>
    <VendorMark {id} name={label} size={18} />
    <span class="row__name">{label}</span>
    <span class="row__summary row__summary--{tone}" {title} role={live ? "status" : undefined}>
      {#if tone !== "ink"}<span class="row__dot" aria-hidden="true"></span>{/if}
      {summary}
    </span>
    {#if meta}<span class="row__meta">{meta}</span>{/if}
    {#if action}<span class="row__action">{open ? "Close" : action}</span>{/if}
    <span class="row__chevron" aria-hidden="true">›</span>
  </button>
  {#if open}
    <div class="row__body">
      {@render children()}
    </div>
  {/if}
</section>

<style>
  .row { display: flex; flex-direction: column; border-bottom: 1px solid var(--border); transition: box-shadow 0.6s ease; }
  .row--lit { box-shadow: 0 0 0 3px var(--accent-soft); border-radius: var(--radius-sm); }
  .row__head { display: flex; align-items: center; gap: var(--space-sm); width: 100%; min-height: 3rem; padding: var(--space-sm) var(--space-xs); border: 0; background: none; color: var(--ink); font: inherit; text-align: left; cursor: pointer; }
  .row__head:hover { background: var(--accent-soft); }
  .row__name { flex-shrink: 0; min-width: 5.5rem; font-size: 0.95rem; font-weight: 600; }
  .row__summary { display: inline-flex; flex: 1; min-width: 0; align-items: center; gap: 0.4rem; color: var(--ink); font-size: 0.85rem; }
  .row__summary--warn { color: var(--warn); }
  .row__summary--fail { color: var(--fail); }
  .row__dot { flex-shrink: 0; width: 0.5rem; height: 0.5rem; border-radius: 50%; background: var(--muted); }
  .row__summary--ok .row__dot { background: var(--ok); }
  .row__summary--warn .row__dot { background: var(--warn); }
  .row__summary--fail .row__dot { background: var(--fail); }
  .row__meta { flex-shrink: 0; color: var(--muted); font-size: 0.8rem; }
  .row__action { flex-shrink: 0; color: var(--accent, var(--ink)); font-size: 0.85rem; font-weight: 500; }
  .row__chevron { flex-shrink: 0; color: var(--muted); transition: transform 0.15s ease; }
  .row--open .row__chevron { transform: rotate(90deg); }
  .row__body { display: flex; flex-direction: column; padding: 0 0 var(--space-md); border-top: 1px solid var(--border); }
  @media (max-width: 48rem) {
    .row__head { flex-wrap: wrap; }
    .row__summary { flex-basis: 100%; order: 3; }
  }
</style>
