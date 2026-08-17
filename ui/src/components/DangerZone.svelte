<script lang="ts">
  // The Danger zone: destroys the running agent, its platform identity, and
  // its Slack presence (the repo is archived, not deleted; memories die
  // with the namespace, per the confirm copy below, verbatim from the
  // approved spec). Rendered by AgentDetail only when `destroy_visible` is
  // true (the caller's own gate check, see AgentDetail.svelte and
  // `SlackPanel`'s `slack_visible` for the same discipline); the route
  // itself is the real enforcement boundary, this is UX only.
  //
  // Three states, driven entirely by `status` (the agent's own detail,
  // refetched by AgentDetail's polling, same "no local state machine of my
  // own" idiom SlackPanel documents for its own three states):
  //  - anything else (ready/pending/provisioning/failed/...): the confirm
  //    form. Typing the agent's exact name arms the "Destroy this agent"
  //    button, a plain equality check with no partial-match leniency, so a
  //    typo can't accidentally arm it.
  //  - "destroying": no form, no button, just a plain in-progress line.
  //    The live checklist itself (StepChecklist, fed the same steps[] this
  //    agent's provisioning used) already renders progress; this card would
  //    only be repeating it.
  //  - "destroy_failed": the failure reason plus a single "Destroy again"
  //    button, no re-typing the name (resume is entirely server-side, same
  //    as a provisioning "Retry").
  //
  // Styling matches the room's established card idiom (border/background,
  // same as `.slack-panel`/`.memory-panel`) rather than a new visual
  // language; the one danger-specific touch is the heading and the destroy
  // buttons picking up `var(--fail)`, the same token `.failed-banner` and
  // `.memory-modal__danger` already use elsewhere: restrained, not a
  // red-bordered box.
  import { createMutation, useQueryClient } from "@tanstack/svelte-query";
  import { errorMessage } from "../lib/api";
  import { destroyAgentMutation } from "../lib/queries";

  let {
    agentName,
    status,
    error,
  }: { agentName: string; status: string; error: string | null } = $props();

  const queryClient = useQueryClient();
  // agentName is read once here rather than tracked reactively: AgentDetail
  // (the only caller) remounts via `{#key route.name}` whenever the routed
  // agent changes, so this prop is fixed for the component's entire
  // lifetime, same reasoning as ChatPanel/SlackPanel's own mutations.
  // svelte-ignore state_referenced_locally
  const destroy = createMutation(destroyAgentMutation(queryClient, agentName));

  let typedName = $state("");
  let destroyError = $state<string | null>(null);

  const armed = $derived(typedName === agentName);

  function handleDestroy(): void {
    if (!armed || $destroy.isPending) {
      return;
    }
    destroyError = null;
    $destroy.mutate(undefined, {
      onError: (err: unknown) => {
        destroyError = errorMessage(err, "Couldn't destroy this agent. Try again.");
      },
    });
  }

  function handleDestroyAgain(): void {
    if ($destroy.isPending) {
      return;
    }
    destroyError = null;
    $destroy.mutate(undefined, {
      onError: (err: unknown) => {
        destroyError = errorMessage(err, "Couldn't destroy this agent. Try again.");
      },
    });
  }
</script>

<section class="danger-zone" aria-labelledby="danger-zone-heading">
  <h2 id="danger-zone-heading" class="danger-zone__heading">Danger zone</h2>

  {#if status === "destroying"}
    <p class="danger-zone__notice">
      Destroying this agent…
    </p>
  {:else if status === "destroy_failed"}
    <p class="danger-zone__notice danger-zone__notice--fail" role="alert">
      {error ?? "Something went wrong during destruction."}
    </p>
    <button
      type="button"
      class="danger-zone__destroy"
      disabled={$destroy.isPending}
      onclick={handleDestroyAgain}
    >
      {$destroy.isPending ? "Destroying…" : "Destroy again"}
    </button>
  {:else}
    <p class="danger-zone__copy">
      This destroys the running agent, its platform identity, and its Slack presence. The repo is archived, not deleted. Memories die with the namespace.
    </p>
    <div class="danger-zone__confirm">
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
    <button
      type="button"
      class="danger-zone__destroy"
      disabled={!armed || $destroy.isPending}
      onclick={handleDestroy}
    >
      {$destroy.isPending ? "Destroying…" : "Destroy this agent"}
    </button>
  {/if}

  {#if destroyError}
    <p class="danger-zone__error" role="alert">{destroyError}</p>
  {/if}
</section>

<style>
  .danger-zone {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .danger-zone__heading {
    margin: 0;
    color: var(--fail);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .danger-zone__copy {
    margin: 0;
    color: var(--muted);
    font-size: 0.9rem;
  }

  .danger-zone__confirm {
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

  .danger-zone__destroy {
    align-self: flex-start;
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--fail);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .danger-zone__destroy:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .danger-zone__destroy:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .danger-zone__notice {
    margin: 0;
    color: var(--muted);
    font-size: 0.9rem;
  }

  .danger-zone__notice--fail {
    color: var(--fail);
  }

  .danger-zone__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }
</style>
