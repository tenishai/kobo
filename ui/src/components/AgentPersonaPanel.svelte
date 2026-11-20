<script lang="ts">
  // The persona an agent runs, read and edited in place (2026-11-20). A
  // teammate tuning an agent could not find where to change its persona:
  // Edit details' team field was labelled as the persona's source, and this
  // section only linked to `bundle/persona.md` on GitHub. Now it shows the
  // persona the agent runs, read from that file (`GET .../persona`), and
  // **Edit persona** rewrites it (`PUT .../persona/text`). The agent restarts
  // with the new text; its memory, skills and Guardrails stay.
  //
  // An edit changes this agent alone. When the caller may also edit the
  // catalog persona it was made from, a line after the save offers **Save to
  // {persona} too** (the persona form's own `PUT /api/personas/{id}`, with that
  // persona's rules left as they are), so the next agent made from it starts
  // from the new text as well. Nothing else changes any other agent. Text that
  // names this agent keeps the offer off: every later agent would carry the
  // name.
  //
  // A persona exactly in the room's house shape is edited in its parts; the
  // room says so only when writing the parts back gives the same file, so the
  // form never drops a line it does not show. Its rules are offered only while
  // they live in the persona (a room without guardrails, or an agent from before
  // them); otherwise they are the agent's Guardrails, and changed there. Any
  // other persona is shown and edited as one text.
  //
  // A save names the version it was opened at (`base_sha`). When the file moved
  // since, the room refuses it, and the form takes the text as it stands so the
  // change is made again on top of it, the way Guardrails does.
  //
  // A save rebuilds the agent, so its delivery is reported to the page the
  // way the Skills panel's is (`lib/delivery.svelte`): the header says
  // Updating and the page rests until the new persona is in place.
  //
  // `agentName` is read once: AgentDetail remounts this per agent.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { untrack } from "svelte";
  import ErrorState from "./ErrorState.svelte";
  import Modal from "./Modal.svelte";
  import Skeleton from "./Skeleton.svelte";
  import {
    ApiError,
    errorMessage,
    type AgentPersona,
    type AgentPersonaEdit,
  } from "../lib/api";
  import {
    beginDelivery as noteDeliveryBegan,
    reportDelivery as noteDeliveryState,
  } from "../lib/delivery.svelte";
  import {
    agentPersonaQuery,
    deliveryQuery,
    editAgentPersonaMutation,
    personaFullQuery,
    updatePersonaMutation,
  } from "../lib/queries";
  import { personaMissing, splitLines } from "../lib/validate";

  let {
    agentName,
    personaName,
    frozen = false,
  }: {
    agentName: string;
    /** The catalog persona's name for this agent's persona id, or the id
     * itself when the catalog no longer lists it. */
    personaName: string;
    frozen?: boolean;
  } = $props();

  const queryClient = useQueryClient();
  // svelte-ignore state_referenced_locally
  const persona = createQuery(() => agentPersonaQuery(agentName));
  // svelte-ignore state_referenced_locally
  const editMut = createMutation(() => editAgentPersonaMutation(queryClient, agentName));
  const sourceMut = createMutation(() => updatePersonaMutation(queryClient));

  // What they do starts folded past this many lines.
  const FOLD_LINES = 12;

  let expanded = $state(false);
  const whatLines = $derived((persona.data?.what_they_do ?? persona.data?.text ?? "").split("\n"));
  const folded = $derived(!expanded && whatLines.length > FOLD_LINES);
  const shownWhat = $derived(folded ? whatLines.slice(0, FOLD_LINES).join("\n") : whatLines.join("\n"));

  const notReady = $derived(
    persona.error instanceof ApiError && persona.error.status === 409,
  );

  // --- The editor -------------------------------------------------------

  let open = $state(false);
  let who = $state("");
  let what = $state("");
  let rulesText = $state("");
  let wholeText = $state("");
  /** The version the form was filled from, sent as `base_sha`. */
  let baseSha = $state<string | null>(null);
  let saveError = $state<string | null>(null);

  const house = $derived(persona.data?.house_shape ?? true);
  const rulesHere = $derived(Boolean(persona.data?.rules_in_persona));
  const fileRules = $derived(persona.data?.rules ?? []);

  /** Fill the form from what the room read, and remember its version. */
  function seed(data: AgentPersona): void {
    who = data.who_they_are ?? "";
    what = data.what_they_do ?? "";
    rulesText = (data.rules ?? []).join("\n");
    wholeText = data.text ?? "";
    baseSha = data.sha ?? null;
  }

  function openEditor(): void {
    const data = persona.data;
    if (!data || frozen) {
      return;
    }
    seed(data);
    saveError = null;
    open = true;
  }

  /** What is still missing, in one sentence, or null when Save may go: the
   * persona form's own floor (`personaMissing`), with the name not edited
   * here and no floor on rules, which are this agent's. The server is still
   * the check. */
  const missing = $derived.by((): string | null => {
    if (!house) {
      return wholeText.trim().length === 0 ? "The persona can't be empty." : null;
    }
    return personaMissing(
      { name: personaName, whoTheyAre: who, whatTheyDo: what, rules: rulesText },
      "shared",
      true,
      false,
    );
  });

  /** The last saved parts, for Save to {persona} too. */
  let saved = $state<{ who: string; what: string } | null>(null);
  let offerSource = $state(false);
  let sourceNote = $state<{ ok: boolean; message: string } | null>(null);

  /** Whether the saved text names this agent as a whole word, in any case.
   * Saved to the catalog, every agent made from the persona later would carry
   * this one's name. */
  const namesAgent = $derived.by((): boolean => {
    if (saved === null) {
      return false;
    }
    const escaped = agentName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const pattern = new RegExp(`\\b${escaped}\\b`, "i");
    return pattern.test(saved.who) || pattern.test(saved.what);
  });

  function handleSave(): void {
    if (missing !== null || editMut.isPending || frozen) {
      return;
    }
    saveError = null;
    const body: AgentPersonaEdit = house
      ? {
          who_they_are: who,
          what_they_do: what,
          ...(rulesHere ? { rules: splitLines(rulesText) } : {}),
          base_sha: baseSha,
        }
      : { text: wholeText, base_sha: baseSha };
    const offer = house && Boolean(persona.data?.can_edit_source);
    editMut.mutate(body, {
      onSuccess: (result) => {
        open = false;
        saved = house ? { who, what } : null;
        offerSource = offer;
        sourceNote = null;
        beginDelivery(result.sha);
      },
      onError: async (error: unknown) => {
        saveError = errorMessage(error, "Couldn't save the persona. Try again.");
        if (error instanceof ApiError && error.status === 409) {
          // Changed since the dialog opened: the form takes the text as it
          // stands, and the change is made again on top of it.
          try {
            seed(await queryClient.fetchQuery(agentPersonaQuery(agentName)));
          } catch {
            // The line above already says what happened.
          }
        }
      },
    });
  }

  async function saveToSource(): Promise<void> {
    const data = persona.data;
    if (!data || saved === null || sourceMut.isPending) {
      return;
    }
    sourceNote = null;
    try {
      // Its own rules go back as they are: this only carries the new text.
      const stored = await queryClient.fetchQuery(personaFullQuery(data.persona));
      await sourceMut.mutateAsync({
        id: data.persona,
        body: { who_they_are: saved.who, what_they_do: saved.what, rules: stored.rules },
      });
      offerSource = false;
      sourceNote = {
        ok: true,
        message: `Saved to ${personaName}. Agents made from it from now on start from this text.`,
      };
    } catch (error) {
      sourceNote = {
        ok: false,
        message: errorMessage(error, `Couldn't save it to ${personaName}. Try again.`),
      };
    }
  }

  // --- Delivery: the restart this save started ---------------------------

  let deliverySha = $state<string | null>(null);
  // svelte-ignore state_referenced_locally
  const delivery = createQuery(() => deliveryQuery(agentName, deliverySha));

  function beginDelivery(sha: string): void {
    deliverySha = sha;
    // The page reads this to say Updating and rest its controls while the
    // rebuild this save started is on its way (issues #268, #290).
    noteDeliveryBegan(agentName, sha);
  }

  $effect(() => {
    const sha = deliverySha;
    const state = delivery.data?.state;
    if (sha === null || state === undefined) {
      return;
    }
    untrack(() => noteDeliveryState(agentName, sha, state));
  });

  type DeliveryView = { tone: "pending" | "ok" | "fail"; text: string };

  const deliveryView = $derived.by((): DeliveryView => {
    if (delivery.isError) {
      return { tone: "fail", text: errorMessage(delivery.error, "Couldn't check on the restart.") };
    }
    const data = delivery.data;
    if (data?.state === "delivered") {
      return { tone: "ok", text: `${agentName} runs the new persona.` };
    }
    if (data?.state === "failed") {
      return {
        tone: "fail",
        text: errorMessage(
          data.detail !== null ? new Error(data.detail) : undefined,
          "The restart didn't go through. Save again to retry.",
        ),
      };
    }
    return { tone: "pending", text: `Restarting ${agentName} with the new persona…` };
  });
</script>

<section class="panel" aria-labelledby="persona-panel-heading">
  <div class="persona-panel__head">
    <div class="panel__header">
      <h2 id="persona-panel-heading" class="panel__heading">Persona</h2>
      <p class="panel__lead">
        Who {agentName} is and how it works. It started from the persona {personaName}.
      </p>
    </div>
    {#if persona.data?.can_edit}
      <button type="button" class="btn" disabled={frozen} onclick={openEditor}>
        Edit persona
      </button>
    {/if}
  </div>

  {#if persona.isPending}
    <Skeleton rows={4} />
  {:else if notReady}
    <p class="panel__note">{agentName} isn't ready yet. Its persona shows here once it is.</p>
  {:else if persona.isError}
    <ErrorState
      message={errorMessage(persona.error, "Couldn't read this agent's persona.")}
      onretry={() => persona.refetch()}
    />
  {:else if persona.data}
    <dl class="persona-panel__text">
      {#if house}
        <dt>Who they are</dt>
        <dd>You are {agentName}, {persona.data.who_they_are}.</dd>
        <dt>What they do</dt>
      {:else}
        <dt>Persona</dt>
      {/if}
      <dd>
        <p class="persona-panel__body">{shownWhat}</p>
        {#if whatLines.length > FOLD_LINES}
          <button type="button" class="btn--link" onclick={() => (expanded = !expanded)}>
            {expanded ? "Show less" : `Show all ${whatLines.length} lines`}
          </button>
        {/if}
      </dd>
      {#if house && fileRules.length > 0}
        <dt>Rules</dt>
        <dd>
          <ul class="persona-panel__rules">
            {#each fileRules as rule, index (index)}
              <li>{rule}</li>
            {/each}
          </ul>
          {#if !persona.data.rules_in_persona}
            <span class="persona-panel__help">
              {agentName} reads these with its persona. New rules go on its Guardrails.
            </span>
          {/if}
        </dd>
      {/if}
    </dl>

    {#if deliverySha !== null}
      <div
        class="persona-panel__delivery persona-panel__delivery--{deliveryView.tone}"
        role="status"
        aria-live="polite"
      >
        {#if deliveryView.tone === "pending"}
          <span class="persona-panel__delivery-dot pulse" aria-hidden="true"></span>
        {/if}
        <span>{deliveryView.text}</span>
      </div>
    {/if}

    {#if offerSource}
      <div class="persona-panel__offer">
        <p class="panel__note">
          {#if namesAgent}
            The text names {agentName}. Agents made from {personaName} later would carry that
            name, so take it out first, or keep this change on {agentName} only.
          {:else}
            Agents made from {personaName} later still start from the old text.
          {/if}
        </p>
        <button
          type="button"
          class="btn"
          disabled={sourceMut.isPending || namesAgent}
          onclick={saveToSource}
        >
          {sourceMut.isPending ? "Saving…" : `Save to ${personaName} too`}
        </button>
      </div>
    {/if}
    {#if sourceNote}
      <p
        class="panel__note"
        class:panel__note--fail={!sourceNote.ok}
        role={sourceNote.ok ? "status" : "alert"}
      >
        {sourceNote.message}
      </p>
    {/if}
  {/if}
</section>

{#snippet editorFooter()}
  {#if saveError}
    <p class="field__status field__status--error persona-panel__state" role="alert">{saveError}</p>
  {:else if missing}
    <p class="field__help persona-panel__state">{missing}</p>
  {/if}
  <div class="modal__actions">
    <button type="button" class="btn btn--quiet" onclick={() => (open = false)}>Cancel</button>
    <button
      type="button"
      class="btn btn--primary"
      disabled={missing !== null || editMut.isPending || frozen}
      onclick={handleSave}
    >
      {editMut.isPending ? "Saving…" : "Save and restart"}
    </button>
  </div>
{/snippet}

<Modal
  {open}
  title="Edit persona"
  description={`Saving restarts ${agentName} with the new persona. Its memory, skills and guardrails stay as they are.`}
  size="wide"
  onclose={() => (open = false)}
  footer={editorFooter}
>
  <form
    class="modal__form"
    onsubmit={(event) => {
      event.preventDefault();
      handleSave();
    }}
  >
    {#if house}
      <div class="field">
        <label class="field__label" for="agent-persona-who">Who they are</label>
        <input
          id="agent-persona-who"
          class="field__input"
          type="text"
          bind:value={who}
          maxlength={200}
          autocomplete="off"
        />
        <p class="field__help">
          One line. It finishes the sentence "You are {agentName}, ..."
        </p>
      </div>
      <div class="field">
        <label class="field__label" for="agent-persona-what">What they do</label>
        <textarea id="agent-persona-what" class="field__input" bind:value={what} rows="14"
        ></textarea>
        <p class="field__help">
          The work, the ground it owns, each kind of request and how to handle it, and what to
          do when unsure. Most personas run 25 to 45 lines.
        </p>
      </div>
      {#if rulesHere}
        <div class="field">
          <label class="field__label" for="agent-persona-rules">Rules</label>
          <textarea id="agent-persona-rules" class="field__input" bind:value={rulesText} rows="6"
          ></textarea>
          <p class="field__help">One per line: things it always or never does.</p>
        </div>
      {/if}
    {:else}
      <div class="field">
        <label class="field__label" for="agent-persona-text">Persona</label>
        <textarea id="agent-persona-text" class="field__input" bind:value={wholeText} rows="18"
        ></textarea>
        <p class="field__help">
          This persona wasn't written with the persona form, so it is edited as one text.
        </p>
      </div>
    {/if}
  </form>
</Modal>

<style>
  .persona-panel__head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-md);
  }

  .persona-panel__text {
    display: grid;
    grid-template-columns: 11rem minmax(0, 1fr);
    margin: 0;
    border-top: 1px solid var(--border);
  }

  .persona-panel__text dt,
  .persona-panel__text dd {
    padding: var(--space-md) 0;
    border-bottom: 1px solid var(--border);
  }

  .persona-panel__text dt {
    padding-right: var(--space-md);
    color: var(--muted);
    font-size: 0.85rem;
  }

  .persona-panel__text dd {
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--ink);
    font-size: 0.875rem;
    line-height: 1.6;
  }

  .persona-panel__body {
    margin: 0 0 var(--space-2xs);
    white-space: pre-wrap;
  }

  .persona-panel__rules {
    margin: 0;
    padding-left: 1.1rem;
  }

  .persona-panel__help {
    display: block;
    margin-top: var(--space-2xs);
    color: var(--muted);
    font-size: 0.8rem;
  }

  .persona-panel__offer {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-sm);
  }

  .persona-panel__state {
    margin: 0;
  }

  .persona-panel__delivery {
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

  .persona-panel__delivery--ok {
    border-color: var(--ok);
    color: var(--ok);
  }

  .persona-panel__delivery--fail {
    border-color: var(--fail);
    color: var(--fail);
  }

  .persona-panel__delivery-dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: var(--pulse);
  }

  @media (max-width: 40rem) {
    .persona-panel__head {
      flex-direction: column;
    }

    .persona-panel__text {
      grid-template-columns: minmax(0, 1fr);
    }

    .persona-panel__text dt {
      padding-bottom: 0;
      border-bottom: 0;
    }
  }
</style>
