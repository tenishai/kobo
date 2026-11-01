<script lang="ts">
  // Convert to connected: one of the two changes there is no way back
  // from. A row at the bottom of Edit details (issue #368; a Settings row
  // before that), offered off the same operator flag the page has always
  // read (`destroy_visible`, whose server-side gate is the same one the
  // convert route enforces). It asks no run-mode question and says exactly
  // what stops working before it will do it.
  import { createMutation, useQueryClient } from "@tanstack/svelte-query";
  import Modal from "./Modal.svelte";
  import { errorMessage, RUN_MODE_HTTP } from "../lib/api";
  import { convertToConnectedMutation } from "../lib/queries";
  import { toast } from "../lib/toast.svelte";

  let { agentName }: { agentName: string } = $props();

  const queryClient = useQueryClient();
  // Read once, like every other panel here: AgentDetail remounts this
  // component via `{#key route.name}` when the routed agent changes.
  // svelte-ignore state_referenced_locally
  const convert = createMutation(() => convertToConnectedMutation(queryClient, agentName));

  let confirming = $state(false);
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
        confirming = false;
        toast.success("Converted", `The room no longer edits ${agentName}.`);
      },
      onError: (err: unknown) => {
        convertError = errorMessage(err, "Couldn't convert this agent. Try again.");
      },
    });
  }
</script>

<div class="convert-row">
  <div class="convert-row__text">
    <span class="convert-row__name">Convert to connected</span>
    <p class="convert-row__desc">
      For an agent whose repository no longer runs the room's runtime.
    </p>
  </div>
  <div class="convert-row__action">
    <button
      type="button"
      class="btn"
      onclick={() => {
        convertError = null;
        confirming = true;
      }}
    >
      Convert
    </button>
  </div>
</div>

{#snippet convertFooter()}
  <div class="modal__actions">
    <button
      type="button"
      class="btn btn--quiet"
      disabled={convert.isPending}
      onclick={() => {
        confirming = false;
        convertError = null;
      }}
    >
      Cancel
    </button>
    <button
      type="button"
      class="btn btn--primary"
      disabled={convert.isPending}
      onclick={handleConvert}
    >
      {convert.isPending ? "Converting…" : "Convert this agent"}
    </button>
  </div>
{/snippet}

<Modal
  open={confirming}
  title="Convert to connected"
  size="narrow"
  onclose={() => (confirming = false)}
  footer={convertFooter}
>
  <p class="convert-row__copy">
    The room stops editing this agent: no chat, memory, learnings, skills, tooling or Slack from here, and other agents can no longer reach it. Its repository, deployment and cloud identity stay as they are. This cannot be undone from the room.
  </p>
  {#if convertError}
    <p class="field__status field__status--error" role="alert">{convertError}</p>
  {/if}
</Modal>

<style>
  .convert-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-sm);
  }

  .convert-row__text {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 0.1rem;
  }

  .convert-row__name {
    color: var(--ink);
    font-size: 0.9rem;
    font-weight: 600;
  }

  .convert-row__desc {
    margin: 0;
    color: var(--ink);
    font-size: 0.8rem;
  }

  .convert-row__action {
    margin-left: auto;
  }

  .convert-row__copy {
    margin: 0;
    color: var(--ink);
    font-size: 0.9rem;
  }
</style>
