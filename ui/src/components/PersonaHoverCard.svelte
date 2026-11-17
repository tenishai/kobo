<script lang="ts">
  // What a persona actually is, shown where someone is already looking.
  //
  // The create dialog's persona list gives a row one truncated line, which
  // is enough to recognise a persona you already know and not enough to
  // choose one you don't. Resting on a row (or tabbing onto its radio)
  // opens this card beside the list with the whole of it: who they are,
  // what they do, the rules they hold to, and who made them.
  //
  // It is a `role="tooltip"`, not a dialog. Nothing in here is focusable
  // except the one button at the bottom, which opens the whole persona in
  // place of the list (`onopen`, personas-in-place wave 2026-09-30: the
  // Personas page it used to link to is gone), nothing here takes focus away from
  // the row, and Escape dismisses it — a dialog role would promise a
  // focus trap and a modal contract this deliberately does not have.
  //
  // The fetch is lazy and keyed per persona (`personaQuery`, five minutes
  // fresh), so sweeping up and down the same list costs one request per
  // persona and none at all for a list nobody hovers.
  import { createQuery } from "@tanstack/svelte-query";
  import Skeleton from "./Skeleton.svelte";
  import { personaQuery } from "../lib/queries";
  import { boldRuns } from "../lib/markdown";
  import { relativeTime } from "../lib/time";

  let {
    personaId,
    row = null,
    boundary = null,
    id = "persona-hover-card",
    onkeep = () => {},
    onrelease = () => {},
    onopen = () => {},
  }: {
    /** The persona to show, or null to fetch nothing at all. */
    personaId: string | null;
    /** The row this card belongs to, for vertical alignment. */
    row?: HTMLElement | null;
    /** The box the card must not spill out of — the dialog body. */
    boundary?: HTMLElement | null;
    id?: string;
    /** The pointer or focus has arrived here: hold the card open. Without
     * this the card would close on the way to its own link. */
    onkeep?: () => void;
    /** ...and left again. */
    onrelease?: () => void;
    /** Read the whole persona: the caller swaps it in over the list. */
    onopen?: (personaId: string) => void;
  } = $props();

  const persona = createQuery(() => personaQuery(personaId));

  /** Rules past this many are counted, not listed. */
  const RULES_SHOWN = 5;

  const rules = $derived(persona.data?.rules ?? []);
  const shownRules = $derived(rules.slice(0, RULES_SHOWN));
  const moreRules = $derived(Math.max(0, rules.length - RULES_SHOWN));

  /** The body as plain paragraphs: split on blank lines, nothing rendered
   * as markdown. A persona's text is prose a human wrote, not a document —
   * rendering it would invent headings and links that are not there.
   * Bold alone is drawn (`boldRuns`). */
  const paragraphs = $derived(
    (persona.data?.what_they_do ?? "")
      .split(/\n\s*\n/)
      .map((part) => part.trim())
      .filter((part) => part.length > 0),
  );

  const footer = $derived.by(() => {
    const data = persona.data;
    if (!data?.created_by) {
      return null;
    }
    const when = data.created_at ? ` · ${relativeTime(data.created_at)}` : "";
    return `Author: ${data.created_by}${when}`;
  });

  // --- placement ----------------------------------------------------------

  const GAP = 8;
  const EDGE = 8;

  let cardEl = $state<HTMLElement | null>(null);
  let top = $state(0);
  let left = $state(0);

  /** Beside the list when there is room for the whole card, under the row
   * when there isn't, and never past an edge either way. Measured against
   * the boundary box (the dialog body) rather than the row, so every row's
   * card lands in the same column. */
  function place(): void {
    if (!cardEl) {
      return;
    }
    const anchor = row ?? boundary;
    if (!anchor) {
      return;
    }
    const rowBox = anchor.getBoundingClientRect();
    const boundBox = (boundary ?? anchor).getBoundingClientRect();
    const width = cardEl.offsetWidth || 352;
    const height = cardEl.offsetHeight || 0;
    const viewWidth = window.innerWidth;
    const viewHeight = window.innerHeight;

    let x = boundBox.right + GAP;
    let y = rowBox.top;
    if (x + width > viewWidth - EDGE) {
      x = rowBox.left;
      y = rowBox.bottom + GAP;
    }

    left = Math.max(EDGE, Math.min(x, viewWidth - width - EDGE));
    top = Math.max(EDGE, Math.min(y, viewHeight - height - EDGE));
  }

  $effect(() => {
    // Re-place on the row it belongs to and on the content arriving, since
    // the card's own height is what decides whether it fits.
    void row;
    void persona.data;
    void persona.isPending;
    place();
  });

  $effect(() => {
    const onResize = () => place();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  });
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div
  {id}
  class="hover-card"
  role="tooltip"
  bind:this={cardEl}
  style="top: {top}px; left: {left}px;"
  onmouseenter={onkeep}
  onmouseleave={onrelease}
  onfocusin={onkeep}
  onfocusout={onrelease}
>
  {#if persona.isPending}
    <Skeleton rows={2} />
  {:else if persona.isError}
    <p class="hover-card__quiet">Could not load this persona.</p>
  {:else}
    <div class="hover-card__head">
      <span class="hover-card__name">{persona.data.name}</span>
      <span class="hover-card__tags">
        <span class="hover-card__tag hover-card__tag--team">
          {persona.data.builtin ? "Built-in" : persona.data.team}
        </span>
        <!-- Set, not the raw list: a repeated tag from the server would
             throw each_key_duplicate and take the whole card down. Same
             guard the persona sheet keeps too. -->
        {#each [...new Set(persona.data.tags)] as tag (tag)}
          <span class="hover-card__tag">{tag}</span>
        {/each}
      </span>
    </div>

    <p class="hover-card__heading">Who they are</p>
    <p class="hover-card__line">{persona.data.who_they_are}</p>

    <p class="hover-card__heading">What they do</p>
    <div class="hover-card__body">
      {#each paragraphs as para, index (index)}
        <p class="hover-card__para">{#each boldRuns(para) as run, at (at)}{#if run.bold}<strong>{run.text}</strong>{:else}{run.text}{/if}{/each}</p>
      {/each}
    </div>
    <button
      type="button"
      class="btn--link hover-card__link"
      onclick={() => {
        if (personaId !== null) {
          onopen(personaId);
        }
      }}
    >
      Read the full persona
    </button>

    {#if shownRules.length > 0}
      <p class="hover-card__heading">Rules</p>
      <ul class="hover-card__rules">
        {#each shownRules as rule, index (index)}
          <li class="hover-card__rule">{rule}</li>
        {/each}
      </ul>
      {#if moreRules > 0}
        <p class="hover-card__quiet">and {moreRules} more</p>
      {/if}
    {/if}

    {#if footer}
      <p class="hover-card__footer">{footer}</p>
    {/if}
  {/if}
</div>

<style>
  .hover-card {
    position: fixed;
    z-index: 60;
    display: flex;
    width: 22rem;
    max-width: calc(100vw - 1rem);
    flex-direction: column;
    gap: var(--space-2xs);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
    font-size: 0.8rem;
  }

  .hover-card__head {
    display: flex;
    align-items: center;
    gap: var(--space-2xs);
    flex-wrap: wrap;
  }

  .hover-card__name {
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.8rem;
    font-weight: 600;
  }

  .hover-card__tags {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2xs);
  }

  .hover-card__tag {
    padding: 0.1rem var(--space-2xs);
    border: 1px solid var(--border);
    border-radius: var(--radius-pill);
    color: var(--muted);
    font-size: 0.7rem;
    white-space: nowrap;
  }

  .hover-card__tag--team {
    border-color: transparent;
    background: var(--accent-soft);
    color: var(--ink);
  }

  .hover-card__heading {
    margin: 0;
    color: var(--ink);
    font-family: var(--font-heading);
    font-size: var(--title-size);
    font-weight: 600;
    letter-spacing: var(--title-tracking);
    text-transform: var(--label-case);
  }

  .hover-card__line {
    margin: 0;
    color: var(--ink);
  }

  /* About a dozen lines, then it fades. The rest is a click away rather
     than a card that grows past the window on a long persona. */
  .hover-card__body {
    position: relative;
    max-height: 17rem;
    overflow: hidden;
  }

  .hover-card__body::after {
    position: absolute;
    right: 0;
    bottom: 0;
    left: 0;
    height: 2rem;
    background: linear-gradient(to bottom, transparent, var(--panel));
    content: "";
  }

  .hover-card__para {
    margin: 0 0 var(--space-2xs);
    color: var(--ink);
    white-space: pre-wrap;
  }

  .hover-card__link {
    align-self: flex-start;
    color: var(--accent);
    font-size: 0.75rem;
  }

  .hover-card__rules {
    margin: 0;
    padding-left: 1.1rem;
    color: var(--ink);
  }

  .hover-card__rule {
    margin-bottom: 0.1rem;
  }

  .hover-card__quiet {
    margin: 0;
    color: var(--muted);
    font-size: 0.75rem;
  }

  .hover-card__footer {
    margin: var(--space-2xs) 0 0;
    padding-top: var(--space-2xs);
    border-top: 1px solid var(--border);
    color: var(--muted);
    font-size: 0.7rem;
  }
</style>
