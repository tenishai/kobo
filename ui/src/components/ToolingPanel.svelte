<script lang="ts">
  // "Always on": the checks the room runs on every task, at the foot of an
  // agent's Guardrails (Kyle, 2026-11-20: "Checks the room runs" asked an owner
  // to upload Python, and nobody would). A check is a file in the agent's
  // `bundle/tools/` with a pre_task or post_draft hook (the 2026-08-13
  // addendum); the runtime runs every one around each task and reply.
  //
  // Each row says what the check does in one sentence (`description`, from the
  // server: the room's own words for its baseline pack, else the check's
  // docstring) and one state: on, starting at the next restart, paused by a
  // broken check, or not confirmed. The file's name sits beside it for
  // whoever wrote it.
  // There is no upload here: a check is code, and an engineer adds one from
  // their own assistant (`kobo_add_agent_tool`). Remove shows only when the
  // server says this caller may (`can_remove`): the room's own checks are a
  // room operator's to remove, any other the agent's owners'. It asks in the
  // room's own Modal first.
  //
  // The banner is the server's enforcement envelope, probed from the running
  // agent's card and never guessed here: `locked` (a check failed to load, and
  // the agent answers with a refusal until it is fixed or removed) wins over
  // everything and names the file.
  //
  // Delivery chip after a removal: the same writable/derived pair MemoryPanel
  // and SkillsPanel use (see MemoryPanel's doc comment for why it is a store
  // pair, not $state).
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { CheckCircle, Clock } from "phosphor-svelte";
  import ErrorState from "./ErrorState.svelte";
  import Modal from "./Modal.svelte";
  import Skeleton from "./Skeleton.svelte";
  import { errorMessage, type ToolingFile } from "../lib/api";
  import { deleteToolingMutation, deliveryQuery, toolingQuery } from "../lib/queries";

  let { agentName, frozen = false }: { agentName: string; frozen?: boolean } = $props();

  const queryClient = useQueryClient();
  // `agentName` is read once: AgentDetail remounts this component per agent.
  // svelte-ignore state_referenced_locally
  const tooling = createQuery(() => toolingQuery(agentName));
  // svelte-ignore state_referenced_locally
  const deleteMutation = createMutation(() => deleteToolingMutation(queryClient, agentName));

  const locked = $derived(tooling.data?.enforcement?.locked ? tooling.data.enforcement : null);

  type RowState = { on: boolean; text: string };

  /** What a check's row says. "On" only when the server says it is enforced. A
   * lock stops every check, and the banner says which one failed; a delivered
   * check the room can't confirm (the agent was out of reach, or its runtime
   * doesn't say) says just that, rather than guess why. */
  function rowState(file: ToolingFile): RowState {
    if (file.status === "enforced") return { on: true, text: "On" };
    if (locked) {
      return {
        on: false,
        text: file.name === locked.locked_file ? "Failed to load" : "Paused while a check is broken",
      };
    }
    if (!file.delivered) return { on: false, text: "Starts at its next restart" };
    return { on: false, text: "Can't confirm it's on right now" };
  }

  // --- Delivery chip (after a removal) -------------------------------------

  const DELIVERED_CLEAR_MS = 5000;

  let deliverySha = $state<string | null>(null);
  const delivery = createQuery(() => deliveryQuery(agentName, deliverySha));

  let clearTimer: ReturnType<typeof setTimeout> | undefined;

  function beginDelivery(sha: string): void {
    if (clearTimer !== undefined) {
      clearTimeout(clearTimer);
      clearTimer = undefined;
    }
    deliverySha = sha;
  }

  $effect(() => {
    if (delivery.data?.state !== "delivered") {
      return;
    }
    clearTimer = setTimeout(() => {
      deliverySha = null;
      clearTimer = undefined;
    }, DELIVERED_CLEAR_MS);
    return () => {
      if (clearTimer !== undefined) {
        clearTimeout(clearTimer);
        clearTimer = undefined;
      }
    };
  });

  type DeliveryView = { tone: "pending" | "ok" | "fail"; text: string };

  const deliveryView = $derived.by((): DeliveryView => {
    if (delivery.isError) {
      return { tone: "fail", text: errorMessage(delivery.error, "Couldn't check on your changes.") };
    }
    const data = delivery.data;
    if (data === undefined) {
      return { tone: "pending", text: "Delivering changes…" };
    }
    if (data.state === "delivered") {
      return { tone: "ok", text: "Changes are in place" };
    }
    if (data.state === "failed") {
      return {
        tone: "fail",
        text: errorMessage(
          data.detail !== null ? new Error(data.detail) : undefined,
          "Something went wrong. Try again.",
        ),
      };
    }
    return { tone: "pending", text: "Delivering changes…" };
  });

  // --- Remove: confirmed in the room's own dialog ------------------------

  let removeError = $state<string | null>(null);
  let removeTarget = $state<ToolingFile | null>(null);

  function openRemove(file: ToolingFile): void {
    removeError = null;
    removeTarget = file;
  }

  function closeRemove(): void {
    removeTarget = null;
  }

  function handleRemove(): void {
    const file = removeTarget;
    if (file === null || deleteMutation.isPending) {
      return;
    }
    removeError = null;
    deleteMutation.mutate(file.name, {
      onSuccess: (result) => {
        removeTarget = null;
        beginDelivery(result.sha);
      },
      onError: (err: unknown) => {
        removeError = errorMessage(err, "Couldn't remove it. Try again.");
      },
    });
  }
</script>

<section class="panel" aria-labelledby="tooling-panel-heading">
  <div class="panel__header">
    <h2 id="tooling-panel-heading" class="panel__heading">Always on</h2>
    <p class="panel__lead">
      Checks the room runs on every task. A check can stop a request or change a reply,
      which a rule can't.
    </p>
  </div>

  {#if locked}
    <div class="tooling-panel__notice" role="alert">
      {locked.locked_file} failed to load. {agentName} answers every message with a refusal
      until it is fixed or removed.
    </div>
  {/if}

  {#if tooling.isPending}
    <Skeleton rows={2} />
  {:else if tooling.isError}
    <ErrorState
      message={errorMessage(tooling.error, "Couldn't load this agent's checks.")}
      onretry={() => tooling.refetch()}
    />
  {:else}
    {#if removeError && removeTarget === null}
      <p class="panel__note panel__note--fail" role="alert">{removeError}</p>
    {/if}
    {#if tooling.data.files.length === 0}
      <p class="panel__empty">No checks run on {agentName}.</p>
    {:else}
      <ul class="checks">
        {#each tooling.data.files as file (file.name)}
          {@const state = rowState(file)}
          <li class="check">
            <span class="check__mark check__mark--{state.on ? 'on' : 'wait'}" aria-hidden="true">
              {#if state.on}
                <CheckCircle size={18} weight="fill" />
              {:else}
                <Clock size={18} />
              {/if}
            </span>
            <div class="check__body">
              <p class="check__what">{file.description ?? file.name}</p>
              <p class="check__meta">
                <span>{state.text}</span>
                {#if file.description}
                  <code class="check__file">{file.name}</code>
                {/if}
              </p>
            </div>
            {#if file.can_remove}
              <button
                type="button"
                class="btn btn--quiet btn--danger"
                disabled={frozen || deleteMutation.isPending}
                aria-label={`Remove ${file.name}`}
                onclick={() => openRemove(file)}
              >
                Remove
              </button>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
    <p class="tooling-panel__how">
      A check is a small program. An engineer adds one from their own assistant, with
      <strong>add agent tool</strong>.
    </p>
  {/if}

  {#if deliverySha !== null}
    <div
      class="tooling-panel__delivery tooling-panel__delivery--{deliveryView.tone}"
      role="status"
      aria-live="polite"
    >
      {#if deliveryView.tone === "pending"}
        <span class="tooling-panel__delivery-dot pulse" aria-hidden="true"></span>
      {/if}
      <span>{deliveryView.text}</span>
    </div>
  {/if}
</section>

<Modal
  open={removeTarget !== null}
  title={removeTarget ? `Remove ${removeTarget.name}` : undefined}
  onclose={closeRemove}
>
  {#if removeTarget}
    <p class="tooling-modal__confirm-text">
      Remove {removeTarget.name}? This can't be undone.
    </p>
    {#if removeError}
      <p class="panel__note panel__note--fail" role="alert">{removeError}</p>
    {/if}
    <div class="tooling-modal__actions">
      <button type="button" class="btn btn--quiet" onclick={closeRemove}>Cancel</button>
      <button
        type="button"
        class="btn btn--danger"
        disabled={deleteMutation.isPending}
        onclick={handleRemove}
      >
        {deleteMutation.isPending ? "Removing…" : "Remove"}
      </button>
    </div>
  {/if}
</Modal>

<style>
  .tooling-panel__notice {
    padding: var(--space-sm) var(--space-md);
    border: 1px solid var(--fail);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--fail) 10%, transparent);
    color: var(--ink);
    font-size: 0.85rem;
  }

  .checks {
    margin: 0;
    padding: 0;
    list-style: none;
    border-top: 1px solid var(--border);
  }

  .check {
    display: flex;
    gap: var(--space-sm);
    align-items: flex-start;
    padding: var(--space-xs) 0;
    border-bottom: 1px solid var(--border);
  }

  .check__mark {
    display: inline-flex;
    flex-shrink: 0;
    padding-top: 0.1rem;
  }

  .check__mark--on {
    color: var(--ok);
  }

  .check__mark--wait {
    color: var(--muted);
  }

  .check__body {
    flex: 1;
    min-width: 0;
  }

  .check__what {
    margin: 0;
    color: var(--ink);
  }

  .check__meta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2xs) var(--space-sm);
    margin: var(--space-2xs) 0 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .check__file {
    font-size: 0.8rem;
  }

  .tooling-panel__how {
    margin: 0;
    font-size: 0.85rem;
  }

  .tooling-modal__confirm-text {
    margin: 0 0 var(--space-md);
    color: var(--ink);
  }

  .tooling-modal__actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-xs);
    margin-top: var(--space-md);
  }

  .tooling-panel__delivery {
    display: inline-flex;
    align-self: flex-start;
    align-items: center;
    gap: var(--space-xs);
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-pill);
    color: var(--muted);
    font-size: 0.85rem;
  }

  .tooling-panel__delivery--ok {
    border-color: var(--ok);
    color: var(--ok);
  }

  .tooling-panel__delivery--fail {
    border-color: var(--fail);
    color: var(--fail);
  }

  .tooling-panel__delivery-dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: var(--pulse);
  }
</style>
