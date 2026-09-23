<script lang="ts">
  // The Danger zone: destroys the running agent, its platform identity, and
  // its Slack presence (the repo is archived, not deleted; memories die
  // with the namespace, per the confirm copy below, verbatim from the
  // approved spec). Rendered by AgentDetail only when `destroy_visible` is
  // true (the caller's own gate check, see AgentDetail.svelte); the route
  // itself is the real enforcement boundary, this is UX only.
  //
  // A connected agent is destroyed by the same ledger with two differences
  // (connected-agents design 2026-09-21, `destroy.py`): its repository is
  // left exactly as it is, since it belongs to its team, and there is no
  // chat app to remove because the room never made one. Both are in the
  // copy rather than only in the code: this paragraph is the last thing
  // anyone reads before typing the name, and telling an owner their
  // repository will be archived when it will not is the one mistake here
  // that cannot be taken back afterwards.
  //
  // Three states, driven entirely by `status` (the agent's own detail,
  // refetched by AgentDetail's polling, same "no local state machine of my
  // own" idiom SlackPanel documents for its own three states):
  //  - anything else (ready/pending/provisioning/failed/...): a quiet
  //    "Destroy..." that opens the confirm. Typing the agent's exact name
  //    arms the "Destroy this agent" button, a plain equality check with no
  //    partial-match leniency, so a typo can't accidentally arm it.
  //  - "destroying": no confirm, no button, just a plain in-progress line.
  //    The live checklist itself (StepChecklist, fed the same steps[] this
  //    agent's provisioning used) already renders progress; this would only
  //    be repeating it.
  //  - "destroy_failed": the failure reason plus a single "Destroy again"
  //    button, no re-typing the name (resume is entirely server-side, same
  //    as a provisioning "Retry").
  //
  // Page-shell wave (2026-09-23): this is a ROW in the Settings panel now,
  // not a card of its own under every tab on the page. The card carried an
  // always-open "type the name" field that a visitor read as a threat on a
  // page they came to for something else; behind a confirm it is read at
  // the moment it is meant to be read, which is the only moment it says
  // anything. The copy inside the confirm is unchanged, word for word.
  // Restrained, not a red-bordered box: the one danger-specific touch is
  // `var(--fail)` on the two destroy buttons, the same token
  // `.failed-banner` and `.memory-modal__danger` already use.
  import { createMutation, useQueryClient } from "@tanstack/svelte-query";
  import Modal from "./Modal.svelte";
  import { errorMessage, KIND_COMPOSED, KIND_CONNECTED } from "../lib/api";
  import { destroyAgentMutation } from "../lib/queries";

  let {
    agentName,
    status,
    error,
    kind = KIND_COMPOSED,
  }: {
    agentName: string;
    status: string;
    error: string | null;
    /** The agent's own kind. Defaults to composed, so a caller that has not
     * been taught about kinds gets today's copy rather than the narrower
     * one. */
    kind?: string;
  } = $props();

  const queryClient = useQueryClient();
  // agentName is read once here rather than tracked reactively: AgentDetail
  // (the only caller) remounts via `{#key route.name}` whenever the routed
  // agent changes, so this prop is fixed for the component's entire
  // lifetime, same reasoning as ChatPanel/SlackPanel's own mutations.
  // svelte-ignore state_referenced_locally
  const destroy = createMutation(() => (destroyAgentMutation(queryClient, agentName)));

  let typedName = $state("");
  let destroyError = $state<string | null>(null);
  let confirming = $state(false);

  const armed = $derived(typedName === agentName);

  function openConfirm(): void {
    typedName = "";
    destroyError = null;
    confirming = true;
  }

  function handleDestroy(): void {
    if (!armed || destroy.isPending) {
      return;
    }
    destroyError = null;
    destroy.mutate(undefined, {
      onError: (err: unknown) => {
        destroyError = errorMessage(err, "Couldn't destroy this agent. Try again.");
      },
    });
  }

  function handleDestroyAgain(): void {
    if (destroy.isPending) {
      return;
    }
    destroyError = null;
    destroy.mutate(undefined, {
      onError: (err: unknown) => {
        destroyError = errorMessage(err, "Couldn't destroy this agent. Try again.");
      },
    });
  }
</script>

<div class="danger-zone">
  <div class="danger-zone__row">
    <div class="danger-zone__text">
      <span class="danger-zone__name">Destroy this agent</span>
      <p class="danger-zone__desc">
        Takes it out of the room for good. There is no way back.
      </p>
    </div>
    <div class="danger-zone__action">
      {#if status === "destroying"}
        <p class="danger-zone__notice">Destroying this agent…</p>
      {:else if status === "destroy_failed"}
        <button
          type="button"
          class="danger-zone__button"
          disabled={destroy.isPending}
          onclick={handleDestroyAgain}
        >
          {destroy.isPending ? "Destroying…" : "Destroy again"}
        </button>
      {:else}
        <button type="button" class="danger-zone__button" onclick={openConfirm}>
          Destroy...
        </button>
      {/if}
    </div>
  </div>

  {#if status === "destroy_failed"}
    <p class="danger-zone__notice danger-zone__notice--fail" role="alert">
      {error ?? "Something went wrong during destruction."}
    </p>
  {/if}

  {#if destroyError}
    <p class="danger-zone__notice danger-zone__notice--fail" role="alert">{destroyError}</p>
  {/if}
</div>

<Modal open={confirming} title="Destroy this agent" onclose={() => (confirming = false)}>
  <div class="danger-zone__confirm">
    <!-- Verbatim from the approved spec, and kind-specific: this paragraph
         is the last thing anyone reads before typing the name, and telling
         an owner their repository will be archived when it will not is the
         one mistake here that cannot be taken back afterwards. -->
    {#if kind === KIND_CONNECTED}
      <p class="danger-zone__copy">
        This destroys the running agent and its platform identity. The repository belongs to your team and is left as it is. Memories die with the namespace.
      </p>
    {:else}
      <p class="danger-zone__copy">
        This destroys the running agent, its platform identity, and its Slack presence. The repo is archived, not deleted. Memories die with the namespace.
      </p>
    {/if}
    <div class="danger-zone__field">
      <label class="danger-zone__label" for="danger-zone-confirm-name">
        Type {agentName} to confirm
      </label>
      <input
        id="danger-zone-confirm-name"
        class="danger-zone__input"
        type="text"
        autocomplete="off"
        spellcheck="false"
        bind:value={typedName}
      />
    </div>
    <div class="danger-zone__actions">
      <button
        type="button"
        class="danger-zone__cancel"
        disabled={destroy.isPending}
        onclick={() => (confirming = false)}
      >
        Cancel
      </button>
      <button
        type="button"
        class="danger-zone__destroy"
        disabled={!armed || destroy.isPending}
        onclick={handleDestroy}
      >
        {destroy.isPending ? "Destroying…" : "Destroy this agent"}
      </button>
    </div>
  </div>
</Modal>

<style>
  .danger-zone {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .danger-zone__row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-sm);
  }

  .danger-zone__text {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 0.1rem;
  }

  .danger-zone__name {
    color: var(--ink);
    font-size: 0.9rem;
    font-weight: 600;
  }

  .danger-zone__desc {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .danger-zone__action {
    margin-left: auto;
  }

  /* Quiet on the row, fail-toned only in its lettering: a filled red
     button on a settings list reads as an invitation. */
  .danger-zone__button {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--fail);
    font: inherit;
    font-size: 0.85rem;
    font-weight: 600;
    line-height: 1.4;
    white-space: nowrap;
    cursor: pointer;
  }

  .danger-zone__button:hover:not(:disabled) {
    border-color: var(--fail);
  }

  .danger-zone__button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .danger-zone__confirm {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
  }

  .danger-zone__copy {
    margin: 0;
    color: var(--muted);
    font-size: 0.9rem;
  }

  .danger-zone__field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .danger-zone__label {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.85rem;
  }

  .danger-zone__input {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .danger-zone__input:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .danger-zone__actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-sm);
  }

  .danger-zone__cancel {
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
    cursor: pointer;
  }

  .danger-zone__cancel:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .danger-zone__destroy {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--fail);
    color: var(--accent-ink);
    font: inherit;
    font-weight: 600;
    cursor: pointer;
  }

  .danger-zone__destroy:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .danger-zone__destroy:disabled,
  .danger-zone__cancel:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .danger-zone__notice {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .danger-zone__notice--fail {
    color: var(--fail);
  }
</style>
