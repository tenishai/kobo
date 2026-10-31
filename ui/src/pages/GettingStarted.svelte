<script lang="ts">
  // The room's user guide, read from `GET /api/guide` and rendered here.
  //
  // Guide wave (2026-09-24): the old page was five short paragraphs written
  // by hand in this file, and it drifted from the room three times in a
  // month (suggested names that no longer exist, channel IDs the Slack tab
  // no longer leads with, a Harness it named in one sentence). The text now
  // lives in the API package as one markdown file per section, so the page
  // and the `kobo_guide` MCP tool read the same words, and a section is
  // updated once.
  //
  // Layout (2026-10-31): the room already has a side nav, so the guide's
  // own navigation is one bar across the top: a section picker, previous
  // and next, and a progress track with one segment per section. The
  // current section lives in the hash (`#/getting-started/<section>`) so a
  // section can be linked to.
  import { createQuery } from "@tanstack/svelte-query";
  import { ArrowLeft, ArrowRight, CaretDown, CaretLeft, CaretRight, Info } from "phosphor-svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import Skeleton from "../components/Skeleton.svelte";
  import { errorMessage } from "../lib/api";
  import { renderMarkdown } from "../lib/markdown";
  import { guideQuery } from "../lib/queries";
  import { formatHash, navigate, route } from "../lib/router.svelte";

  const guide = createQuery(() => guideQuery());

  let sections = $derived(guide.data?.sections ?? []);
  // The hash names the section; an unknown or missing id opens the first.
  let current = $derived(
    sections.find((section) => section.id === route.section) ?? sections[0],
  );
  let html = $derived(current ? renderMarkdown(current.body) : "");
  let index = $derived(current ? sections.findIndex((section) => section.id === current.id) : -1);
  let previous = $derived(index > 0 ? sections[index - 1] : undefined);
  let next = $derived(index >= 0 && index < sections.length - 1 ? sections[index + 1] : undefined);

  /** Two digits, so the list's numbers line up in one column. */
  function number(i: number): string {
    return String(i + 1).padStart(2, "0");
  }

  function jump(id: string): void {
    navigate({ page: "getting-started", section: id });
    document.querySelector(".shell__main")?.scrollTo?.({ top: 0 });
    window.scrollTo?.({ top: 0 });
  }

  function go(event: MouseEvent, id: string): void {
    event.preventDefault();
    jump(id);
  }
</script>

<article class="guide">
  {#if guide.isPending}
    <div class="guide__loading">
      <Skeleton rows={4} />
    </div>
  {:else if guide.isError}
    <ErrorState
      message={errorMessage(guide.error, "Couldn't load the guide.")}
      onretry={() => guide.refetch()}
    />
  {:else if current}
    <div class="guide__bar">
      <div class="guide__bar-row">
        <!-- The native select sits invisibly over a styled face, so the
             picker keeps the browser's own menu, keyboard and screen-reader
             behaviour while looking like the rest of the room. -->
        <div class="guide__picker">
          <span class="guide__picker-face" aria-hidden="true">
            <span class="guide__picker-badge">{number(index)}</span>
            <span class="guide__picker-title">{current.title}</span>
            <CaretDown class="guide__picker-caret" size={14} weight="bold" />
          </span>
          <select
            class="guide__select"
            aria-label="Jump to section"
            value={current.id}
            onchange={(event) => jump(event.currentTarget.value)}
          >
            {#each sections as section, i (section.id)}
              <option value={section.id}>{number(i)}  {section.title}</option>
            {/each}
          </select>
        </div>

        <div class="guide__stepper">
          <button
            type="button"
            class="guide__step"
            aria-label="Previous section"
            disabled={!previous}
            onclick={() => previous && jump(previous.id)}
          >
            <CaretLeft size={14} weight="bold" aria-hidden="true" />
          </button>
          <span class="guide__count"><b>{index + 1}</b>&nbsp;/&nbsp;{sections.length}</span>
          <button
            type="button"
            class="guide__step"
            aria-label="Next section"
            disabled={!next}
            onclick={() => next && jump(next.id)}
          >
            <CaretRight size={14} weight="bold" aria-hidden="true" />
          </button>
        </div>
      </div>

      <!-- One segment per section, filled up to the one you are on. Each is
           a link (its number and title are screen-reader text and the
           hover title), so the track doubles as the section list. -->
      <nav class="guide__track" aria-label="Guide sections">
        {#each sections as section, i (section.id)}
          <a
            class="guide__segment"
            class:guide__segment--done={i < index}
            href={formatHash({ page: "getting-started", section: section.id })}
            aria-current={section.id === current.id ? "page" : undefined}
            title={`${number(i)} ${section.title}`}
            onclick={(event) => go(event, section.id)}
          ><span class="guide__sr">{number(i)} {section.title}</span></a>
        {/each}
      </nav>
      <p class="guide__bar-foot">
        <span>{Math.round(((index + 1) / sections.length) * 100)}% through the guide</span>
        {#if next}<span>Next: {next.title}</span>{:else}<span>You have reached the end</span>{/if}
      </p>
    </div>


    <section class="guide__body" aria-labelledby="guide-title">
      <header class="guide__header">
        <p class="guide__eyebrow">Section {index + 1} of {sections.length}</p>
        <h1 id="guide-title">{current.title}</h1>
        <p class="guide__summary">{current.summary}</p>
      </header>

      <div class="guide__prose">
        <!-- eslint-disable-next-line svelte/no-at-html-tags -- renderMarkdown escapes its input -->
        {@html html}
      </div>

      <aside class="guide__callout">
        <Info class="guide__callout-icon" size={18} aria-hidden="true" />
        <p>
          Your own assistant can read this guide too. Connect it to the room as
          an MCP server and ask for the guide tool. See "From your own
          assistant".
        </p>
      </aside>

      {#if previous || next}
        <footer class="guide__pager">
          {#if previous}
            <a
              class="guide__page guide__page--prev"
              href={formatHash({ page: "getting-started", section: previous.id })}
              onclick={(event) => go(event, previous.id)}
            >
              <span class="guide__page-label"><ArrowLeft size={12} aria-hidden="true" /> Previous</span>
              <span class="guide__page-title">{previous.title}</span>
            </a>
          {/if}
          {#if next}
            <a
              class="guide__page guide__page--next"
              href={formatHash({ page: "getting-started", section: next.id })}
              onclick={(event) => go(event, next.id)}
            >
              <span class="guide__page-label">Next <ArrowRight size={12} aria-hidden="true" /></span>
              <span class="guide__page-title">{next.title}</span>
            </a>
          {/if}
        </footer>
      {/if}
    </section>
  {/if}
</article>

<style>
  /* One column: the navigation bar on top, the section card under it. */
  /* Centred in the frame, wide enough for the reading column plus a
     generous margin inside each card. */
  .guide {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    width: 100%;
    max-width: 64rem;
    margin: 0 auto;
  }

  .guide__loading {
    max-width: 40rem;
  }

  .guide__bar {
    position: sticky;
    top: 0;
    z-index: 1;
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    padding: var(--space-md) var(--space-lg) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03), 0 8px 24px -12px rgba(0, 0, 0, 0.08);
  }

  .guide__bar-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-md);
  }

  .guide__picker {
    position: relative;
    min-width: 0;
    border-radius: var(--radius-sm);
  }

  .guide__picker-face {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
    padding: var(--space-2xs) var(--space-sm) var(--space-2xs) var(--space-2xs);
    border-radius: var(--radius-sm);
    transition: background 0.15s ease;
  }

  .guide__picker:hover .guide__picker-face {
    background: var(--accent-soft);
  }

  .guide__picker:focus-within .guide__picker-face {
    box-shadow: 0 0 0 3px color-mix(in oklab, var(--ring) 30%, transparent);
  }

  .guide__picker-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 2rem;
    height: 2rem;
    border-radius: calc(var(--radius-sm) - 2px);
    background: var(--accent);
    color: var(--accent-ink);
    font-family: var(--font-display-mono);
    font-size: 0.75rem;
    font-weight: 600;
  }

  .guide__picker-title {
    overflow: hidden;
    color: var(--ink);
    font-size: 0.95rem;
    font-weight: 600;
    letter-spacing: -0.01em;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .guide__picker-face :global(.guide__picker-caret) {
    flex-shrink: 0;
    color: var(--muted);
  }

  /* Invisible, but the thing that is actually clicked and focused. */
  .guide__select {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    opacity: 0;
    cursor: pointer;
    font: inherit;
  }

  /* Previous, the count and next as one segmented control. */
  .guide__stepper {
    display: flex;
    flex-shrink: 0;
    align-items: stretch;
    height: 2.25rem;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
  }

  .guide__count {
    display: inline-flex;
    align-items: center;
    padding: 0 var(--space-sm);
    border-right: 1px solid var(--border);
    border-left: 1px solid var(--border);
    color: var(--muted);
    font-family: var(--font-display-mono);
    font-size: 0.75rem;
    white-space: nowrap;
  }

  .guide__count b {
    color: var(--ink);
    font-weight: 600;
  }

  .guide__step {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 2.25rem;
    border: none;
    background: none;
    color: var(--ink);
    cursor: pointer;
    transition: background 0.15s ease;
  }

  .guide__step:hover:not(:disabled) {
    background: var(--accent-soft);
  }

  .guide__step:disabled {
    color: var(--muted);
    opacity: 0.4;
    cursor: not-allowed;
  }

  .guide__track {
    display: flex;
    gap: 3px;
    padding: var(--space-2xs) 0;
  }

  /* Each segment is a tall invisible hit area with a thin bar drawn in it,
     so the track is easy to click without looking heavy. */
  .guide__segment {
    position: relative;
    flex: 1;
    height: 12px;
    cursor: pointer;
  }

  .guide__segment::before {
    content: "";
    position: absolute;
    top: 50%;
    right: 0;
    left: 0;
    height: 4px;
    border-radius: 999px;
    background: var(--border);
    transform: translateY(-50%);
    transition: background 0.2s ease, height 0.15s ease;
  }

  .guide__segment:hover::before {
    height: 6px;
    background: var(--ring);
  }

  .guide__segment--done::before {
    background: color-mix(in oklab, var(--accent) 45%, var(--border));
  }

  .guide__segment[aria-current="page"]::before {
    height: 6px;
    background: var(--accent);
  }

  .guide__bar-foot {
    display: flex;
    justify-content: space-between;
    gap: var(--space-md);
    margin: 0;
    color: var(--muted);
    font-family: var(--font-display-mono);
    font-size: 0.68rem;
    letter-spacing: 0.04em;
  }

  .guide__sr {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  .guide__body {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: var(--space-xl);
    padding: 3rem 4rem;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .guide__header {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    padding-bottom: var(--space-lg);
    border-bottom: 1px solid var(--border);
  }

  .guide__eyebrow {
    margin: 0;
    color: var(--muted);
    font-family: var(--font-display-mono);
    font-size: 0.7rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .guide__header h1 {
    margin: 0;
    color: var(--ink);
    font-size: 1.85rem;
    font-weight: 600;
    letter-spacing: -0.02em;
    line-height: 1.2;
  }

  .guide__summary {
    margin: 0;
    color: var(--muted);
    font-size: 1rem;
  }

  /* The reading column: a comfortable measure, generous leading, and room
     between blocks so a list of steps reads as steps. */
  .guide__prose {
    display: flex;
    max-width: 42rem;
    flex-direction: column;
    gap: var(--space-md);
    color: var(--ink);
    font-size: 0.95rem;
    line-height: 1.7;
  }

  .guide__prose :global(h2) {
    margin: var(--space-md) 0 0;
    font-size: 1.15rem;
    letter-spacing: -0.01em;
  }

  .guide__prose :global(h3) {
    margin: var(--space-xs) 0 0;
    font-size: 1rem;
  }

  .guide__prose :global(p),
  .guide__prose :global(ul),
  .guide__prose :global(ol) {
    margin: 0;
  }

  .guide__prose :global(ul),
  .guide__prose :global(ol) {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    padding-left: 1.5rem;
  }

  .guide__prose :global(li) {
    padding-left: var(--space-2xs);
  }

  .guide__prose :global(li::marker) {
    color: var(--muted);
    font-family: var(--font-display-mono);
    font-size: 0.85em;
  }

  .guide__prose :global(strong) {
    font-weight: 600;
  }

  /* The mono face itself comes from app.css's global code rule. */
  .guide__prose :global(code) {
    padding: 0.1em 0.35em;
    border: 1px solid var(--border);
    border-radius: 0.35rem;
    background: var(--accent-soft);
    font-size: 0.85em;
  }

  .guide__prose :global(a) {
    color: var(--link);
    text-underline-offset: 2px;
  }

  .guide__callout {
    display: flex;
    max-width: 42rem;
    align-items: flex-start;
    gap: var(--space-sm);
    padding: var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--accent-soft);
    color: var(--muted);
    font-size: 0.85rem;
    line-height: 1.6;
  }

  .guide__callout p {
    margin: 0;
  }

  .guide__callout :global(.guide__callout-icon) {
    flex-shrink: 0;
    margin-top: 0.1rem;
    color: var(--ink);
  }

  .guide__pager {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--space-sm);
    padding-top: var(--space-lg);
    border-top: 1px solid var(--border);
  }

  .guide__page {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
    padding: var(--space-sm) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    color: var(--ink);
    text-decoration: none;
    transition: border-color 0.15s ease, background 0.15s ease;
  }

  .guide__page:hover {
    border-color: var(--ring);
    background: var(--accent-soft);
  }

  /* Next is the way forward through the guide, so it is the filled one:
     black, like the room's other primary buttons. */
  .guide__page--next {
    grid-column: 2;
    align-items: flex-end;
    border-color: var(--accent);
    background: var(--accent);
    color: var(--accent-ink);
    text-align: right;
  }

  .guide__page--next:hover {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 85%, var(--panel));
  }

  .guide__page--next .guide__page-label {
    color: color-mix(in srgb, var(--accent-ink) 70%, transparent);
  }

  .guide__page-label {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    color: var(--muted);
    font-family: var(--font-display-mono);
    font-size: 0.68rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .guide__page-title {
    font-size: 0.9rem;
    font-weight: 500;
  }

  @media (max-width: 40rem) {
    .guide__body {
      padding: var(--space-lg);
    }

    .guide__count {
      display: none;
    }
  }
</style>
