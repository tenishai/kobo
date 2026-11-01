<script module lang="ts">
  /** Which station a row stands for. Exported from the module block so the
   * shell can name the choice it is handed without redeclaring the union. */
  export type NewAgentChoice = "build" | "connect";
</script>

<script lang="ts">
  // The one door into making an agent (new-agent entry wave, 2026-09-23).
  // The rail used to carry three buttons side by side, each opening its own
  // station, and an operator read them as three competing primaries with no
  // way to tell which one they wanted. So the rail now has one button, and
  // this chooser asks the one question those three buttons were really
  // asking: how do you want to start?
  //
  // It owns no state and starts no work. It renders three rows and hands
  // the pick back through `onchoose`; the caller (App.svelte) closes this
  // dialog and opens the station the pick names. Closing the station goes
  // back to the dashboard, not back here -- a chooser that reappears behind
  // every cancel is a trap, not a step.
  import Modal from "./Modal.svelte";

  let {
    open,
    onclose,
    onchoose,
  }: {
    open: boolean;
    onclose: () => void;
    onchoose: (choice: NewAgentChoice) => void;
  } = $props();

  // Copy lives in one list rather than three near-identical blocks of
  // markup, so the three ways to start stay parallel in shape and length.
  const options: { choice: NewAgentChoice; title: string; desc: string }[] = [
    {
      choice: "build",
      title: "Build an agent",
      desc: "Pick a persona and the room builds and deploys it on its runtime.",
    },
    {
      choice: "connect",
      title: "Connect a repository you already have",
      desc: "Your code, your runtime. The room deploys and watches it.",
    },
  ];
</script>

{#snippet chooserFooter()}
  <div class="modal__actions">
    <!-- Cancel and nothing else: choosing a row is the action, so a primary
         button here would only ask the same question twice. -->
    <button type="button" class="btn btn--quiet" onclick={onclose}>Cancel</button>
  </div>
{/snippet}

<Modal {open} title="New agent" {onclose} footer={chooserFooter}>
  <div class="modal__section">
    <p class="chooser__question">How do you want to start?</p>
    <div class="list">
      {#each options as option (option.choice)}
        <button
          type="button"
          class="list__row chooser__row"
          onclick={() => onchoose(option.choice)}
        >
          <span class="chooser__text">
            <span class="chooser__title">{option.title}</span>
            <span class="chooser__desc">{option.desc}</span>
          </span>
          <span class="chooser__chevron" aria-hidden="true">›</span>
        </button>
      {/each}
    </div>
  </div>
</Modal>

<style>
  .chooser__question {
    margin: 0 0 var(--space-sm);
    color: var(--ink);
    font-weight: 600;
  }

  /* A real button wearing the shared row idiom: the UA's own background,
     border and font are cleared so it reads as a row, and the row's own
     divider is redrawn here because clearing the border took it with it. */
  .chooser__row {
    width: 100%;
    align-items: center;
    border: 0;
    border-bottom: 1px solid var(--border);
    background: none;
    font: inherit;
  }

  .chooser__row:last-child {
    border-bottom: none;
  }

  .chooser__row:hover {
    background: var(--bg);
  }

  .chooser__row:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }

  .chooser__text {
    display: flex;
    flex: 1;
    min-width: 0;
    flex-direction: column;
    gap: 0.15rem;
    padding: var(--space-2xs) 0;
    text-align: left;
  }

  .chooser__title {
    color: var(--ink);
    font-size: 0.9rem;
    font-weight: 600;
  }

  .chooser__desc {
    color: var(--ink);
    font-size: 0.8rem;
    line-height: 1.4;
  }

  .chooser__chevron {
    flex-shrink: 0;
    color: var(--muted);
    font-size: 1.1rem;
  }
</style>
