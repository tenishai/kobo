<script lang="ts">
  // The agent page's back office (page-shell wave, 2026-09-23).
  //
  // Convert and the Danger zone used to be SIBLINGS of the tab strip, which
  // meant both rendered under every tab: somebody who opened Chat to say
  // hello got a "type warden to confirm" field at the bottom of it, every
  // time, on every tab. They are rows in here now, behind a tab nobody
  // opens by accident, in the same idiom SecretsPanel uses -- a small-caps
  // heading, one muted lead, and a list where each row is a label, a line
  // saying what it does, and one quiet control on the right.
  //
  // Nothing about either mutation changed. Convert is still offered off the
  // same operator flag the page has always read (`destroy_visible`, whose
  // server-side gate is the same one the convert route enforces), still
  // asks no run-mode question, and still says exactly what stops working
  // before it will do it. Destroy is still DangerZone, with its own
  // kind-specific copy inside its own confirm.
  //
  // The build log is the row every agent has, whoever is looking and
  // whatever kind it is, which is why this tab is always worth opening even
  // when the other two rows are somebody else's to see.
  import { createMutation, useQueryClient } from "@tanstack/svelte-query";
  import BuildLogCard from "./BuildLogCard.svelte";
  import DangerZone from "./DangerZone.svelte";
  import Modal from "./Modal.svelte";
  import { errorMessage, KIND_COMPOSED, RUN_MODE_HTTP } from "../lib/api";
  import { convertToConnectedMutation } from "../lib/queries";
  import { toast } from "../lib/toast.svelte";

  let {
    agentName,
    status,
    error = null,
    kind = KIND_COMPOSED,
    canConvert = false,
    canDestroy = false,
  }: {
    agentName: string;
    status: string;
    /** The agent's own error, for a destruction that failed. */
    error?: string | null;
    kind?: string;
    /** Whether the convert row is this caller's to see: an operator, an
     * agent the room composed, and a status that has settled. Worked out
     * by the page, which is where the agent's own row lives. */
    canConvert?: boolean;
    /** The same `destroy_visible` gate the Danger zone has always had. */
    canDestroy?: boolean;
  } = $props();

  const queryClient = useQueryClient();
  // Read once, like every other panel here: AgentDetail remounts this
  // component via `{#key route.name}` when the routed agent changes.
  // svelte-ignore state_referenced_locally
  const convert = createMutation(() => convertToConnectedMutation(queryClient, agentName));

  let convertConfirming = $state(false);
  let convertError = $state<string | null>(null);

  // There is no run mode to choose. An agent the room composed serves web
  // traffic by construction, and the route refuses anything else, so asking
  // would only offer an answer the room is about to turn down.
  function handleConvert(): void {
    if (convert.isPending) {
      return;
    }
    convertError = null;
    convert.mutate(RUN_MODE_HTTP, {
      onSuccess: () => {
        convertConfirming = false;
        toast.success("Converted", `The room no longer edits ${agentName}.`);
      },
      onError: (err: unknown) => {
        convertError = errorMessage(err, "Couldn't convert this agent. Try again.");
      },
    });
  }
</script>

<section class="settings" aria-labelledby="settings-heading">
  <div class="settings__header">
    <h2 id="settings-heading" class="settings__heading">Settings</h2>
    <p class="settings__lead">
      What this agent was built from, and the two changes there is no way back from.
    </p>
  </div>

  <div class="settings__rows">
    <div class="settings__row">
      <div class="settings__text">
        <span class="settings__label">Build log</span>
        <p class="settings__desc">The last build of this agent's code, as the build itself told it.</p>
      </div>
      <div class="settings__action">
        <BuildLogCard {agentName} />
      </div>
    </div>

    {#if canConvert}
      <div class="settings__row">
        <div class="settings__text">
          <span class="settings__label">Convert to connected</span>
          <p class="settings__desc">
            For an agent whose repository no longer runs the room's runtime.
          </p>
        </div>
        <div class="settings__action">
          <button
            type="button"
            class="settings__button"
            onclick={() => {
              convertError = null;
              convertConfirming = true;
            }}
          >
            Convert...
          </button>
        </div>
      </div>
    {/if}

    {#if canDestroy}
      <div class="settings__row">
        <DangerZone {agentName} {status} {error} {kind} />
      </div>
    {/if}
  </div>
</section>

<Modal
  open={convertConfirming}
  title="Convert to connected"
  onclose={() => (convertConfirming = false)}
>
  <div class="settings__confirm">
    <p class="settings__copy">
      The room stops editing this agent: no chat, memory, learnings, skills, tooling or Slack from here, and other agents can no longer reach it. Its repository, deployment and cloud identity stay as they are. This cannot be undone from the room.
    </p>
    {#if convertError}
      <p class="settings__error" role="alert">{convertError}</p>
    {/if}
    <div class="settings__actions">
      <button
        type="button"
        class="settings__cancel"
        disabled={convert.isPending}
        onclick={() => {
          convertConfirming = false;
          convertError = null;
        }}
      >
        Cancel
      </button>
      <button
        type="button"
        class="settings__confirm-button"
        disabled={convert.isPending}
        onclick={handleConvert}
      >
        {convert.isPending ? "Converting…" : "Convert this agent"}
      </button>
    </div>
  </div>
</Modal>

<style>
  .settings {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .settings__header {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .settings__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .settings__lead {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .settings__rows {
    display: flex;
    flex-direction: column;
  }

  .settings__row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-sm);
    padding: var(--space-sm) 0;
    border-bottom: 1px solid var(--border);
  }

  .settings__row:last-child {
    border-bottom: none;
  }

  /* DangerZone draws its own label, line and control, so its row is the
     whole width and lays itself out. */
  .settings__row > :global(.danger-zone) {
    flex: 1;
    min-width: 0;
  }

  .settings__text {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 0.1rem;
  }

  .settings__label {
    color: var(--ink);
    font-size: 0.9rem;
    font-weight: 600;
  }

  .settings__desc {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .settings__action {
    min-width: 0;
    margin-left: auto;
  }

  .settings__button {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--ink);
    font: inherit;
    font-size: 0.85rem;
    font-weight: 600;
    line-height: 1.4;
    white-space: nowrap;
    cursor: pointer;
  }

  .settings__button:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .settings__confirm {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
  }

  .settings__copy {
    margin: 0;
    color: var(--muted);
    font-size: 0.9rem;
  }

  .settings__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .settings__actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-sm);
  }

  .settings__cancel {
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
    cursor: pointer;
  }

  .settings__cancel:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .settings__confirm-button {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font: inherit;
    font-weight: 600;
    cursor: pointer;
  }

  .settings__confirm-button:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .settings__confirm-button:disabled,
  .settings__cancel:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
