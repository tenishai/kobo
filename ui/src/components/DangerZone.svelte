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
  //    "Destroy" that opens the confirm. Typing the agent's exact name
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
  // `.failed-banner` and `.memory-modal__danger` already use. Modal idiom
  // wave (2026-09-23): both of them are the shared `.btn--danger` now, in
  // the confirm's pinned footer, and there is no filled button anywhere in
  // this dialog -- a red slab under a paragraph about memories dying reads
  // as an invitation.
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
      // The room accepted the destruction: the confirm has done its job and
      // closes. Left open (2026-10-12), it sat over the page with the name
      // still typed while the status read "Destroying", and a second click
      // was accepted as a second destroy (tenishai/kobo#123, item 6).
      onSuccess: () => {
        confirming = false;
        typedName = "";
      },
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
          class="btn btn--danger"
          disabled={destroy.isPending}
          onclick={handleDestroyAgain}
        >
          {destroy.isPending ? "Destroying…" : "Destroy again"}
        </button>
      {:else}
        <button type="button" class="btn btn--danger" onclick={openConfirm}>
          Destroy
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

{#snippet destroyFooter()}
  <div class="modal__actions">
    <button
      type="button"
      class="btn btn--quiet"
      disabled={destroy.isPending}
      onclick={() => (confirming = false)}
    >
      Cancel
    </button>
    <!-- Danger, never the room's filled button: nothing in here is an
         invitation, and the same reasoning already applies to the quiet
         "Destroy" on the row behind it. -->
    <button
      type="button"
      class="btn btn--danger"
      disabled={!armed || destroy.isPending}
      onclick={handleDestroy}
    >
      {destroy.isPending ? "Destroying…" : "Destroy this agent"}
    </button>
  </div>
{/snippet}

<Modal
  open={confirming}
  title="Destroy this agent"
  size="narrow"
  onclose={() => (confirming = false)}
  footer={destroyFooter}
>
  <!-- Verbatim from the approved spec, and kind-specific: this paragraph
       is the last thing anyone reads before typing the name, and telling
       an owner their repository will be archived when it will not is the
       one mistake here that cannot be taken back afterwards. -->
  {#if kind === KIND_CONNECTED}
    <p class="danger-zone__copy">
      This destroys the running agent and its cloud identity. The repository belongs to your team and is left as it is. Memories die with the namespace.
    </p>
  {:else}
    <p class="danger-zone__copy">
      This destroys the running agent, its cloud identity, and its Slack presence. The repo is archived, not deleted. Memories die with the namespace.
    </p>
  {/if}
  <div class="field">
    <label class="field__label" for="danger-zone-confirm-name">
      Type {agentName} to confirm
    </label>
    <input
      id="danger-zone-confirm-name"
      class="field__input"
      type="text"
      autocomplete="off"
      spellcheck="false"
      bind:value={typedName}
    />
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
    color: var(--ink);
    font-size: 0.8rem;
  }

  .danger-zone__action {
    margin-left: auto;
  }

  .danger-zone__copy {
    margin: 0;
    color: var(--ink);
    font-size: 0.9rem;
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
