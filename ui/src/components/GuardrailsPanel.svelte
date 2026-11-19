<script lang="ts">
  // Guardrails (issue #524 step 2): one plain list of rules the agent follows on
  // every task, line by line, each saying where it came from. The list is the
  // agent's bundle/guardrails.md; every change saves the whole list, and the
  // running agent uses it from its next task, or from its next restart when
  // the room could not reach it. Rules guide the model; Access, the checks
  // below and approvals are what enforce, and the panel says so, because a
  // rule that must never fail needs one of those behind it.
  //
  // `agentName` is read once, as in the other Harness panels: AgentDetail
  // remounts this component per agent.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import ErrorState from "./ErrorState.svelte";
  import { errorMessage, type GuardrailRule, type GuardrailRuleIn } from "../lib/api";
  import {
    guardrailsQuery,
    reportGuardrailBrokenMutation,
    saveGuardrailsMutation,
  } from "../lib/queries";
  import { whenDay } from "../lib/time";

  let { agentName, frozen = false }: { agentName: string; frozen?: boolean } = $props();

  const queryClient = useQueryClient();
  // svelte-ignore state_referenced_locally
  const guardrails = createQuery(() => guardrailsQuery(agentName));
  // svelte-ignore state_referenced_locally
  const save = createMutation(() => saveGuardrailsMutation(queryClient, agentName));
  // svelte-ignore state_referenced_locally
  const report = createMutation(() => reportGuardrailBrokenMutation(queryClient, agentName));

  const RULE_MAX_CHARS = 300;
  // A typical rule's share of the limit, for "room for about N more" before
  // there are rules of the agent's own to average.
  const TYPICAL_RULE_CHARS = 80;

  let draft = $state("");
  let editingIndex = $state<number | null>(null);
  let editText = $state("");
  let note = $state("");
  let errorText = $state("");

  const rules = $derived(guardrails.data?.rules ?? []);
  const canEdit = $derived(Boolean(guardrails.data?.can_edit) && !frozen);
  const canReport = $derived(Boolean(guardrails.data?.can_report) && !frozen);
  const breaksBeforeACheck = $derived(guardrails.data?.breaks_before_a_check ?? 2);
  const busy = $derived(save.isPending);
  const roomLeft = $derived.by(() => {
    const data = guardrails.data;
    if (!data) return 0;
    const average = rules.length > 0 ? data.used_chars / rules.length : TYPICAL_RULE_CHARS;
    return Math.max(0, Math.floor((data.limit_chars - data.used_chars) / average));
  });

  function asInput(rule: GuardrailRule): GuardrailRuleIn {
    return { id: rule.id, text: rule.text };
  }

  function person(email: string | null): string {
    if (!email || email === "room") return "the room";
    return email.split("@", 1)[0];
  }

  function sourceLabel(rule: GuardrailRule): string {
    const { kind, ref } = rule.source;
    let label: string;
    if (kind === "persona") label = ref ? `From the persona ${ref}` : "From the persona";
    else if (kind === "skill") label = ref ? `From the skill ${ref}` : "From a skill";
    else if (kind === "correction") label = `From a correction, ${whenDay(rule.added_at)}`;
    else if (kind === "repository") label = "Written in the repository";
    else label = `Added by ${person(rule.added_by)}, ${whenDay(rule.added_at)}`;
    if (rule.edited_by) label += `; edited by ${person(rule.edited_by)}`;
    return label;
  }

  async function commit(next: GuardrailRuleIn[], done: string): Promise<boolean> {
    errorText = "";
    note = "";
    try {
      const result = await save.mutateAsync({
        rules: next,
        baseSha: guardrails.data?.sha ?? null,
      });
      note =
        result.mode === "instant"
          ? `${done} ${agentName} follows the list from its next task.`
          : `${done} It reaches ${agentName} at its next restart.`;
      return true;
    } catch (error) {
      errorText = errorMessage(error, "Couldn't save the guardrails. Try again.");
      return false;
    }
  }

  async function add(event: SubmitEvent) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || busy) return;
    if (await commit([...rules.map(asInput), { text }], "Added.")) draft = "";
  }

  function startEdit(index: number) {
    editingIndex = index;
    editText = rules[index].text;
  }

  async function saveEdit(index: number) {
    const text = editText.trim();
    if (!text || busy) return;
    const next = rules.map(asInput);
    next[index] = { ...next[index], text };
    if (await commit(next, "Saved.")) editingIndex = null;
  }

  async function reportBroken(rule: GuardrailRule) {
    if (rule.id === null || report.isPending) return;
    errorText = "";
    note = "";
    try {
      await report.mutateAsync(rule.id);
      note = "Noted. The count is on the rule.";
    } catch (error) {
      errorText = errorMessage(error, "Couldn't record that. Try again.");
    }
  }

  function brokenLabel(rule: GuardrailRule): string {
    const times = rule.breaks === 1 ? "once" : `${rule.breaks} times`;
    return `Broken ${times}, last ${whenDay(rule.last_broken_at)}`;
  }

  async function remove(index: number) {
    if (busy) return;
    await commit(
      rules.filter((_, i) => i !== index).map(asInput),
      "Removed.",
    );
  }
</script>

<section class="panel" aria-labelledby="guardrails-panel-heading">
  <div class="panel__header">
    <h2 id="guardrails-panel-heading" class="panel__heading">Guardrails</h2>
    <p class="panel__lead">
      Rules {agentName} follows on every task. They guide it; they don't lock anything.
      To make sure something never happens, take the permission away on the Access tab, or
      add a check below.
    </p>
  </div>

  {#if guardrails.isPending}
    <p class="panel__note">Loading...</p>
  {:else if guardrails.isError}
    <ErrorState
      message={errorMessage(guardrails.error, "Couldn't load this agent's guardrails.")}
      onretry={() => guardrails.refetch()}
    />
  {:else}
    {#if guardrails.data && !guardrails.data.enabled}
      <p class="panel__note" role="status">
        Guardrails turn on when this room's runtime reads them. Until then, {agentName}'s
        rules are in its persona.
      </p>
    {/if}

    <p class="guardrails__count">
      {rules.length}
      {rules.length === 1 ? "rule" : "rules"} ·
      {roomLeft > 0 ? `room for about ${roomLeft} more` : "the list is full"}
    </p>

    {#if canEdit}
      <form class="guardrails__add" onsubmit={add}>
        <input
          class="guardrails__input"
          type="text"
          bind:value={draft}
          maxlength={RULE_MAX_CHARS}
          placeholder={`Something ${agentName} should always do, or never do`}
          aria-label="New rule"
          disabled={busy}
        />
        <button class="btn btn--primary" type="submit" disabled={busy || !draft.trim()}>
          Add a rule
        </button>
      </form>
      <p class="guardrails__hint">
        Write each rule so it makes sense on its own: "When searching tickets, use the
        person's full email", not "Use the full email". Always or never: a guardrail. How to
        do a job: a skill.
      </p>
    {/if}

    {#if errorText}
      <p class="panel__note panel__note--fail" role="alert">{errorText}</p>
    {:else if note}
      <p class="panel__note" role="status">{note}</p>
    {/if}

    {#if rules.length === 0}
      <p class="panel__empty guardrails__empty">
        No rules yet. Add what {agentName} should always do, and what it should never do.
      </p>
    {:else}
      <ol class="guardrails__list">
        {#each rules as rule, index (`${rule.id ?? "line"}-${index}`)}
          <li class="guardrail">
            {#if editingIndex === index}
              <input
                class="guardrails__input"
                type="text"
                bind:value={editText}
                maxlength={RULE_MAX_CHARS}
                aria-label="Edit the rule"
                disabled={busy}
                onkeydown={(event) => {
                  if (event.key === "Escape") editingIndex = null;
                  if (event.key === "Enter") saveEdit(index);
                }}
              />
              <span class="guardrail__actions">
                <button class="btn btn--quiet" type="button" onclick={() => (editingIndex = null)}>
                  Cancel
                </button>
                <button
                  class="btn btn--primary"
                  type="button"
                  disabled={busy || !editText.trim()}
                  onclick={() => saveEdit(index)}
                >
                  Save
                </button>
              </span>
            {:else}
              <div class="guardrail__body">
                <p class="guardrail__text">{rule.text}</p>
                <p class="guardrail__source">
                  {sourceLabel(rule)}{rule.breaks > 0 ? `. ${brokenLabel(rule)}` : ""}
                </p>
                {#if rule.breaks >= breaksBeforeACheck}
                  <p class="guardrail__nudge">
                    This keeps breaking. A rule only guides; to make it hold, take the permission
                    away on the Access tab, or add a check below.
                  </p>
                {/if}
              </div>
              {#if canEdit || (canReport && rule.id !== null)}
                <span class="guardrail__actions">
                  {#if canReport && rule.id !== null}
                    <button
                      class="btn btn--quiet"
                      type="button"
                      disabled={report.isPending}
                      aria-label={`It broke the rule: ${rule.text}`}
                      onclick={() => reportBroken(rule)}
                    >
                      It broke this
                    </button>
                  {/if}
                  {#if canEdit}
                    <button
                      class="btn btn--quiet"
                      type="button"
                      disabled={busy}
                      aria-label={`Edit the rule: ${rule.text}`}
                      onclick={() => startEdit(index)}
                    >
                      Edit
                    </button>
                    <button
                      class="btn btn--quiet"
                      type="button"
                      disabled={busy}
                      aria-label={`Remove the rule: ${rule.text}`}
                      onclick={() => remove(index)}
                    >
                      Remove
                    </button>
                  {/if}
                </span>
              {/if}
            {/if}
          </li>
        {/each}
      </ol>
    {/if}
  {/if}
</section>

<style>
  .guardrails__count {
    margin: 0 0 var(--space-sm);
    color: var(--muted);
    font-size: 0.85rem;
  }

  .guardrails__add {
    display: flex;
    gap: var(--space-sm);
  }

  .guardrails__input {
    flex: 1;
    min-width: 0;
  }

  .guardrails__hint {
    margin: var(--space-2xs) 0 var(--space-sm);
    font-size: 0.85rem;
  }

  .guardrails__list {
    margin: var(--space-sm) 0 0;
    padding: 0;
    list-style: none;
    border-top: 1px solid var(--border);
  }

  .guardrail {
    display: flex;
    gap: var(--space-sm);
    align-items: flex-start;
    justify-content: space-between;
    padding: var(--space-sm) 0;
    border-bottom: 1px solid var(--border);
  }

  .guardrail__body {
    min-width: 0;
  }

  .guardrail__text {
    margin: 0;
    overflow-wrap: anywhere;
  }

  .guardrail__source {
    margin: var(--space-2xs) 0 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .guardrail__nudge {
    margin: var(--space-2xs) 0 0;
    font-size: 0.85rem;
  }

  .guardrail__actions {
    display: flex;
    flex-shrink: 0;
    gap: var(--space-2xs);
  }
</style>
