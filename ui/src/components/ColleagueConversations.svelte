<script lang="ts">
  // The room's watching window (Task 4/B4), redesigned as a terminal (the
  // operator's ruling, verbatim): "a command line terminal, which showed
  // all the conversations the agent was having with the other agents in
  // order... and the text in full." The old per-conversation card grid
  // (truncated snippets, an expand-to-see-more detail view, a "show
  // everything" toggle) is gone -- `colleagueFeedQuery` (lib/queries.ts)
  // already hands back one flattened, chronological, full-text feed, so
  // there is nothing left here to expand or toggle.
  //
  // Mounted on the Chat tab under the existing ChatPanel per the operator's
  // original ruling (AgentDetail.svelte) -- unchanged by this redesign.
  //
  // Renders nothing at all (silent absence) when the feed query 409s — that
  // status means this room has no switchboard wired, which isn't an error
  // state worth a banner, just a feature that isn't on here. Any other
  // failure (a 502 upstream hiccup, etc.) still shows a normal error state
  // with retry.
  //
  // Live updates: any frame on `/api/colleagues/stream` (the switchboard's
  // named SSE frames - `hello`, `dispatch_start`, `dispatch_success`,
  // `dispatch_failure`; browsers only dispatch to `onmessage` for frames
  // with NO explicit `event:` field, so each name needs its own listener)
  // just refetches the feed query - no client-side event merging, same
  // "simple and correct" call the original brief made explicitly, and the
  // only sane one anyway: the switchboard's own SSE payloads never carry
  // message text (verified against source -- `dispatch_start`'s payload is
  // routing metadata, `dispatch_success`'s is just a state string), so there
  // is nothing to synthesize a feed entry FROM other than re-asking the feed
  // route itself. The connection only opens once the feed has loaded
  // successfully at least once (proof the switchboard is actually wired
  // here), so a room with none never opens a doomed EventSource that would
  // otherwise retry forever against a 409.
  //
  // `streamActive` is a plain local `$state` flag flipped once, on the first
  // successful load, and never re-derived after that - so the SSE handler's
  // own invalidateQueries refetching NEW data can't cause the stream-opening
  // effect to see a changed dependency and tear the connection down. A
  // second EventSource would mean a gap where frames get silently dropped,
  // since the switchboard never replays.
  import { createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { ApiError, errorMessage } from "../lib/api";
  import { colleagueFeedQuery } from "../lib/queries";
  import ErrorState from "./ErrorState.svelte";

  let { agentName }: { agentName: string } = $props();

  const queryClient = useQueryClient();
  // agentName is read once here rather than tracked reactively: AgentDetail
  // (the only caller) remounts every panel via `{#key route.name}` whenever
  // the routed agent changes, same reasoning as every other panel in this
  // room (ChatPanel/SlackPanel/ToolingPanel/AccessPanel).
  // svelte-ignore state_referenced_locally
  const feed = createQuery(colleagueFeedQuery(agentName));

  const switchboardDisabled = $derived(
    $feed.isError && $feed.error instanceof ApiError && $feed.error.status === 409,
  );

  const entries = $derived($feed.data?.entries ?? []);

  const EVENT_NAMES = ["hello", "dispatch_start", "dispatch_success", "dispatch_failure"];

  let streamActive = $state(false);

  $effect(() => {
    if (!streamActive && $feed.data !== undefined) {
      streamActive = true;
    }
  });

  $effect(() => {
    if (!streamActive) {
      return;
    }
    const source = new EventSource(
      `/api/colleagues/stream?agent=${encodeURIComponent(agentName)}`,
    );
    const handleFrame = () => {
      queryClient.invalidateQueries({ queryKey: ["colleagues", "feed", agentName] });
    };
    for (const name of EVENT_NAMES) {
      source.addEventListener(name, handleFrame);
    }
    return () => {
      source.close();
    };
  });

  // Auto-scroll pin (terminal redesign): pinned to the newest entry except
  // when the operator has scrolled up to read scrollback. `screenEl` is the
  // scrollable terminal viewport; `pinned` flips false the moment a scroll
  // leaves it more than a hair off the bottom, and true again either by
  // scrolling back down by hand or by clicking "Jump to latest" below.
  let screenEl: HTMLDivElement | undefined = $state();
  let pinned = $state(true);

  // Slack/Discord-sized slack, in px: "close enough to the bottom" still
  // counts as pinned, so a sub-pixel rounding difference after a resize
  // doesn't spuriously unpin someone who never touched the scrollbar.
  const BOTTOM_SLACK_PX = 24;

  function isNearBottom(el: HTMLDivElement): boolean {
    return el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_SLACK_PX;
  }

  function handleScroll(): void {
    if (!screenEl) {
      return;
    }
    pinned = isNearBottom(screenEl);
  }

  function jumpToLatest(): void {
    if (!screenEl) {
      return;
    }
    screenEl.scrollTop = screenEl.scrollHeight;
    pinned = true;
  }

  $effect(() => {
    // Re-run whenever the RENDERED row count changes -- a genuine append,
    // but also a filter or chip transition regrowing the list (review fix:
    // keying off entries.length left `pinned` stale-true after clearing a
    // filter, so the next SSE append yanked the scroll without warning).
    const _count = rows.length;
    if (pinned && screenEl) {
      screenEl.scrollTop = screenEl.scrollHeight;
    }
  });

  $effect(() => {
    // A feed that empties out resets the filter state: the box and chips
    // unmount on empty, and stale state must never lie in wait to silently
    // re-filter the feed's return.
    if (entries.length === 0 && (filterText !== "" || activeCounterpart !== null)) {
      filterText = "";
      activeCounterpart = null;
    }
  });

  function clearFilters(): void {
    filterText = "";
    activeCounterpart = null;
  }

  /** ISO-8601 timestamp -> its own embedded wall-clock time, `HH:MM:SS` --
   * sliced straight out of the string rather than parsed through `Date` and
   * a viewer's local timezone, so the same switchboard timestamp reads
   * identically for everyone (and deterministically in tests) instead of
   * drifting with whoever's machine is watching. */
  function formatTime(ts: string): string {
    const match = ts.match(/T(\d{2}:\d{2}:\d{2})/);
    return match ? match[1] : ts;
  }

  /** Same philosophy for the calendar day: the string's own `YYYY-MM-DD`,
   * never `Date`. The operator's report that "old conversations show up as
   * if they happened today" was exactly this - the terminal rendered only
   * `HH:MM:SS`, so day-old scrollback read as fresh. */
  function dateOf(ts: string): string {
    return ts.split("T")[0] ?? ts;
  }

  type FeedEntry = {
    ts: string;
    conversation_id: string;
    from: string;
    to: string;
    role: "ask" | "reply";
    text: string;
  };

  /** One rendered terminal line: a run of CONSECUTIVE identical entries
   * (same direction, same role, same text) collapsed to its first
   * occurrence plus a count. Repetition is real signal (an agent re-asking
   * the same question), so it is badged, never hidden; non-consecutive
   * repeats stay separate lines because the interleaving IS the story. */
  type Line = { entry: FeedEntry; count: number; lastTs: string };

  const lines = $derived.by((): Line[] => {
    const out: Line[] = [];
    for (const entry of entries as FeedEntry[]) {
      const prev = out[out.length - 1];
      if (
        prev &&
        prev.entry.role === entry.role &&
        prev.entry.from === entry.from &&
        prev.entry.to === entry.to &&
        prev.entry.text === entry.text &&
        // A run never crosses midnight: today's repeats must file under
        // today's date rule, or the collapse would reintroduce the exact
        // "old scrollback passes as today" confusion the rules fix.
        dateOf(prev.entry.ts) === dateOf(entry.ts)
      ) {
        prev.count += 1;
        prev.lastTs = entry.ts;
      } else {
        out.push({ entry, count: 1, lastTs: entry.ts });
      }
    }
    return out;
  });

  // The other side of each line, from the viewed agent's seat - the chip
  // vocabulary. Derived from the FULL feed, so chips don't vanish while one
  // of them is active.
  const counterparts = $derived.by((): string[] => {
    const seen = new Set<string>();
    for (const entry of entries as FeedEntry[]) {
      const other = entry.from === agentName ? entry.to : entry.from;
      if (other && other !== agentName) {
        seen.add(other);
      }
    }
    return [...seen].sort();
  });

  let filterText = $state("");
  let activeCounterpart = $state<string | null>(null);

  function toggleCounterpart(name: string): void {
    activeCounterpart = activeCounterpart === name ? null : name;
  }

  const visibleLines = $derived.by((): Line[] => {
    const query = filterText.trim().toLowerCase();
    return lines.filter((line) => {
      if (
        activeCounterpart !== null &&
        line.entry.from !== activeCounterpart &&
        line.entry.to !== activeCounterpart
      ) {
        return false;
      }
      if (query === "") {
        return true;
      }
      // `?? ""` on every field: the feed's shape is a cast, not a runtime
      // guarantee, and one null field must never take down the whole
      // $derived (a thrown TypeError here blanks the terminal).
      return (
        (line.entry.text ?? "").toLowerCase().includes(query) ||
        (line.entry.from ?? "").toLowerCase().includes(query) ||
        (line.entry.to ?? "").toLowerCase().includes(query)
      );
    });
  });

  const filtering = $derived(filterText.trim() !== "" || activeCounterpart !== null);

  /** The render list: visible lines with a dim date rule wherever the
   * calendar day changes (and one before the very first line, so a screen
   * of pure scrollback can never pass as today). */
  type Row = { kind: "date"; date: string } | { kind: "line"; line: Line };

  const rows = $derived.by((): Row[] => {
    const out: Row[] = [];
    let lastDate: string | null = null;
    for (const line of visibleLines) {
      const date = dateOf(line.entry.ts);
      if (date !== lastDate) {
        out.push({ kind: "date", date });
        lastDate = date;
      }
      out.push({ kind: "line", line });
    }
    return out;
  });

  function lineTitle(line: Line): string {
    if (line.count === 1) {
      return line.entry.ts;
    }
    return `${line.entry.ts} to ${line.lastTs} (${line.count} times)`;
  }
</script>

{#if !switchboardDisabled}
  <section class="colleague-terminal" aria-labelledby="colleague-terminal-heading">
    <div class="colleague-terminal__header">
      <h2 id="colleague-terminal-heading" class="colleague-terminal__heading">
        Colleague conversations
      </h2>
      {#if entries.length > 0}
        <input
          type="search"
          class="colleague-terminal__filter"
          placeholder="Filter…"
          aria-label="Filter conversations"
          bind:value={filterText}
        />
      {/if}
    </div>
    {#if counterparts.length > 1}
      <div class="colleague-terminal__chips" role="group" aria-label="Scope to one colleague">
        {#each counterparts as name (name)}
          <button
            type="button"
            class="colleague-terminal__chip"
            class:colleague-terminal__chip--active={activeCounterpart === name}
            aria-pressed={activeCounterpart === name}
            onclick={() => toggleCounterpart(name)}
          >
            {name}
          </button>
        {/each}
      </div>
    {/if}

    {#if $feed.isPending}
      <p class="colleague-terminal__status">Loading…</p>
    {:else if $feed.isError}
      <ErrorState
        message={errorMessage($feed.error, "Couldn't load colleague conversations.")}
        onretry={() => $feed.refetch()}
      />
    {:else}
      <div class="colleague-terminal__wrap">
        <div
          class="colleague-terminal__screen"
          role="log"
          aria-live="polite"
          aria-label="Colleague conversation terminal"
          bind:this={screenEl}
          onscroll={handleScroll}
        >
          {#if entries.length === 0}
            <p class="colleague-terminal__line colleague-terminal__line--empty">
              No agent conversations yet.
            </p>
          {:else if visibleLines.length === 0 && filtering}
            <p class="colleague-terminal__line colleague-terminal__line--empty">
              No lines match the filter.
              <button
                type="button"
                class="colleague-terminal__clear"
                onclick={clearFilters}
              >
                Clear filter
              </button>
            </p>
          {:else}
            {#each rows as row (row.kind === "date" ? "date:" + row.date : row.line.entry.conversation_id + ":" + row.line.entry.role)}
              {#if row.kind === "date"}
                <div class="colleague-terminal__date" role="separator">
                  <span class="colleague-terminal__date-label">{row.date}</span>
                </div>
              {:else}
                {@const entry = row.line.entry}
                <div class="colleague-terminal__entry colleague-terminal__entry--{entry.role}">
                  <div class="colleague-terminal__prompt">
                    <span class="colleague-terminal__time" title={lineTitle(row.line)}
                      >{formatTime(entry.ts)}</span
                    >
                    <span class="colleague-terminal__direction colleague-terminal__direction--{entry.role}"
                      >{entry.from} -&gt; {entry.to}</span
                    >
                    {#if row.line.count > 1}
                      <span
                        class="colleague-terminal__count"
                        title="Repeated {row.line.count} times in a row"
                        >x{row.line.count}</span
                      >
                    {/if}
                  </div>
                  <p class="colleague-terminal__text colleague-terminal__text--{entry.role}">
                    {entry.text}
                  </p>
                </div>
              {/if}
            {/each}
          {/if}
        </div>
        {#if !pinned}
          <button type="button" class="colleague-terminal__jump" onclick={jumpToLatest}>
            Jump to latest
          </button>
        {/if}
      </div>
      {#if $feed.data?.truncated}
        <p class="colleague-terminal__truncated">
          Only the most recent 200 lines are shown.
        </p>
      {/if}
    {/if}
  </section>
{/if}

<style>
  /* A terminal is dark in both the room's light and dark themes -- these
     are fixed values (deliberately NOT `var(--bg)`/`var(--panel)`, which
     flip with the room's own theme), matching this room's OWN dark-theme
     palette (app.css's `@media (prefers-color-scheme: dark)` block) so the
     terminal always looks like a natural extension of the room, never an
     unrelated black box dropped onto a light page. */
  .colleague-terminal {
    --term-bg: #1b1917;
    --term-panel: #242220;
    --term-border: #38342f;
    --term-ink: #f2ede7;
    --term-muted: #9c9188;
    --term-outgoing: #e08a3e;
    --term-incoming: #6fbf96;

    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .colleague-terminal__header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
  }

  .colleague-terminal__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .colleague-terminal__status {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .colleague-terminal__filter {
    min-width: 10rem;
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.8rem;
  }

  .colleague-terminal__filter:focus {
    outline: none;
    border-color: var(--accent);
  }

  .colleague-terminal__chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2xs);
  }

  .colleague-terminal__chip {
    padding: 0.1rem var(--space-sm);
    border: 1px solid var(--border);
    border-radius: 999px;
    background: transparent;
    color: var(--muted);
    font-family: var(--font-mono);
    font-size: 0.75rem;
    cursor: pointer;
  }

  .colleague-terminal__chip:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .colleague-terminal__chip--active {
    border-color: var(--accent);
    background: var(--accent);
    color: var(--panel);
  }

  /* A dim horizontal rule with the date sitting in it -- day boundaries in
     the scrollback, so old conversations can never pass as today. */
  .colleague-terminal__date {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
    color: var(--term-muted);
    font-size: 0.75rem;
    user-select: none;
  }

  .colleague-terminal__date::before,
  .colleague-terminal__date::after {
    content: "";
    flex: 1;
    border-top: 1px solid var(--term-border);
  }

  .colleague-terminal__count {
    padding: 0 0.4rem;
    border: 1px solid var(--term-border);
    border-radius: 999px;
    color: var(--term-muted);
    font-size: 0.75rem;
  }

  /* The always-reachable escape hatch: an active chip's counterpart can
     evict from the feed entirely (room-window churn upstream), taking the
     chip row with it -- this button lives in the no-match state itself, so
     a scoping dead-end can always be walked back in place. */
  .colleague-terminal__clear {
    margin-left: var(--space-xs);
    padding: 0 var(--space-sm);
    border: 1px solid var(--term-border);
    border-radius: 999px;
    background: transparent;
    color: var(--term-ink);
    font-family: var(--font-mono);
    font-size: 0.75rem;
    cursor: pointer;
  }

  .colleague-terminal__clear:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .colleague-terminal__wrap {
    position: relative;
  }

  .colleague-terminal__screen {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    max-height: 22rem;
    padding: var(--space-md);
    overflow-y: auto;
    border: 1px solid var(--term-border);
    border-radius: var(--radius-sm);
    background: var(--term-bg);
    font-family: var(--font-mono);
    font-size: 0.85rem;
  }

  .colleague-terminal__line--empty {
    margin: 0;
    color: var(--term-muted);
  }

  .colleague-terminal__entry {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }

  .colleague-terminal__prompt {
    display: flex;
    align-items: baseline;
    gap: var(--space-xs);
  }

  .colleague-terminal__time {
    color: var(--term-muted);
  }

  .colleague-terminal__direction {
    font-weight: 600;
  }

  .colleague-terminal__direction--ask {
    color: var(--term-outgoing);
  }

  .colleague-terminal__direction--reply {
    color: var(--term-incoming);
  }

  .colleague-terminal__text {
    margin: 0;
    color: var(--term-ink);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  /* Ask text runs full width; a reply is visually indented two monospace
     characters, echoing a shell's own "the response is what came back"
     convention. */
  .colleague-terminal__text--ask {
    padding-left: 0;
  }

  .colleague-terminal__text--reply {
    padding-left: 2ch;
  }

  .colleague-terminal__jump {
    position: absolute;
    bottom: var(--space-md);
    left: 50%;
    transform: translateX(-50%);
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--term-border);
    border-radius: 999px;
    background: var(--term-panel);
    color: var(--term-ink);
    font-family: var(--font-sans);
    font-size: 0.75rem;
    cursor: pointer;
  }

  .colleague-terminal__jump:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .colleague-terminal__truncated {
    margin: 0;
    color: var(--muted);
    font-size: 0.75rem;
  }
</style>
