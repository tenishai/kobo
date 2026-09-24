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
  // Layout follows the Harness tab: sections down the left, one section's
  // body on the right, the current one in the hash
  // (`#/getting-started/<section>`) so a section can be linked to.
  import { createQuery } from "@tanstack/svelte-query";
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
    <nav class="guide__nav" aria-label="Guide sections">
      {#each sections as section, index (section.id)}
        <a
          class="guide__item"
          href={formatHash({ page: "getting-started", section: section.id })}
          aria-current={section.id === current.id ? "page" : undefined}
          onclick={(event) => {
            event.preventDefault();
            navigate({ page: "getting-started", section: section.id });
          }}
        >
          <span class="guide__number">{index + 1}</span>
          <span>{section.title}</span>
        </a>
      {/each}
    </nav>

    <section class="guide__body" aria-labelledby="guide-title">
      <h1 id="guide-title">{current.title}</h1>
      <p class="guide__summary">{current.summary}</p>
      <div class="guide__prose">
        <!-- eslint-disable-next-line svelte/no-at-html-tags -- renderMarkdown escapes its input -->
        {@html html}
      </div>
      <p class="guide__foot">
        Your own assistant can read this guide too: connect it to the room
        as an MCP server and ask for the guide tool. See "From your own
        assistant".
      </p>
    </section>
  {/if}
</article>

<style>
  .guide {
    display: grid;
    grid-template-columns: 14rem minmax(0, 40rem);
    gap: var(--space-lg);
    align-items: start;
  }

  .guide__loading {
    grid-column: 1 / -1;
    max-width: 40rem;
  }

  .guide__nav {
    display: flex;
    flex-direction: column;
    gap: 2px;
    position: sticky;
    top: var(--space-md);
  }

  .guide__item {
    display: flex;
    align-items: baseline;
    gap: var(--space-xs);
    padding: var(--space-2xs) var(--space-xs);
    border-left: 2px solid transparent;
    color: var(--muted);
    font-size: 0.85rem;
    text-decoration: none;
  }

  .guide__item:hover {
    color: var(--ink);
  }

  .guide__item[aria-current="page"] {
    border-left-color: var(--accent);
    color: var(--ink);
    font-weight: 600;
  }

  .guide__number {
    min-width: 1.4em;
    color: var(--muted);
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.04em;
  }

  .guide__body h1 {
    margin: 0 0 var(--space-2xs);
    color: var(--ink);
  }

  .guide__summary {
    margin: 0 0 var(--space-md);
    color: var(--muted);
    font-size: 0.95rem;
  }

  .guide__prose {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    color: var(--ink);
    line-height: 1.6;
  }

  .guide__prose :global(h2) {
    margin: var(--space-sm) 0 0;
    font-size: 1.05rem;
  }

  .guide__prose :global(h3) {
    margin: var(--space-xs) 0 0;
    font-size: 0.95rem;
  }

  .guide__prose :global(p),
  .guide__prose :global(ul),
  .guide__prose :global(ol) {
    margin: 0;
  }

  .guide__prose :global(ul),
  .guide__prose :global(ol) {
    padding-left: 1.4rem;
  }

  .guide__prose :global(li) {
    margin: var(--space-2xs) 0;
  }

  /* The mono face itself comes from app.css's global code rule. */
  .guide__prose :global(code) {
    padding: 0 0.3em;
    border-radius: var(--radius-sm);
    background: var(--accent-soft);
    font-size: 0.85em;
  }

  .guide__prose :global(a) {
    color: var(--link);
  }

  .guide__foot {
    margin: var(--space-lg) 0 0;
    padding-top: var(--space-sm);
    border-top: 1px solid var(--border);
    color: var(--muted);
    font-size: 0.85rem;
  }

  @media (max-width: 44rem) {
    .guide {
      grid-template-columns: minmax(0, 1fr);
    }

    .guide__nav {
      position: static;
      flex-direction: row;
      flex-wrap: wrap;
    }

    .guide__item {
      border-left: none;
      border-bottom: 2px solid transparent;
    }

    .guide__item[aria-current="page"] {
      border-bottom-color: var(--accent);
    }
  }
</style>
