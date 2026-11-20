<script lang="ts">
  // Guardrails (issue #524 step 2): one plain list of rules the agent follows on
  // every task, line by line, each saying where it came from. The list is the
  // agent's bundle/guardrails.md; every change saves the whole list, and the
  // running agent uses it from its next task, or from its next restart when
  // the room could not reach it. Rules guide the model; Access, the checks
  // the room runs and approvals are what enforce, and the panel says so,
  // because a rule that must never fail needs one of those behind it.
  //
  // One line per rule (Kyle, 2026-11-20: the list was too busy to scroll):
  // the rule's words, a short tag for where it came from (the whole sentence
  // on hover), and one ⋯ button that is always on screen, not only on hover.
  // The menu holds Edit, People, It broke this and Remove. Clicking the words
  // edits them too. A long list gets a box to find a rule in.
  //
  // People: a rule can name who it is about ("Only answer these people").
  // They ride on the rule's own line (`kobo_api.guardrails.compose_line`); the
  // agent is told who sent each message and matches them by email. A new
  // rule can take its people before it is added, so a rule like that is never
  // live for a moment without them.
  //
  // `agentName` is read once, as in the other Harness panels: AgentDetail
  // remounts this component per agent.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { DotsThree, Users } from "phosphor-svelte";
  import ErrorState from "./ErrorState.svelte";
  import PeoplePicker from "./PeoplePicker.svelte";
  import {
    errorMessage,
    type GuardrailRule,
    type GuardrailRuleIn,
    type Person,
  } from "../lib/api";
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
  // From this many rules on, a box to find one.
  const FIND_FROM = 12;

  let draft = $state("");
  let draftPeople = $state<Person[]>([]);
  let draftPeopleOpen = $state(false);
  let editingIndex = $state<number | null>(null);
  let editText = $state("");
  let peopleIndex = $state<number | null>(null);
  let peopleDraft = $state<Person[]>([]);
  let menuIndex = $state<number | null>(null);
  let find = $state("");
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
  const findTerm = $derived(find.trim().toLowerCase());

  function matches(rule: GuardrailRule): boolean {
    if (!findTerm) return true;
    if (rule.text.toLowerCase().includes(findTerm)) return true;
    return rule.people.some(
      (person) =>
        person.name.toLowerCase().includes(findTerm) ||
        person.email.toLowerCase().includes(findTerm),
    );
  }

  function asInput(rule: GuardrailRule): GuardrailRuleIn {
    return { id: rule.id, text: rule.text, people: rule.people };
  }

  function person(email: string | null): string {
    if (!email || email === "room") return "the room";
    return email.split("@", 1)[0];
  }

  function names(people: Person[]): string {
    return people.map((p) => p.name || p.email).join(", ");
  }

  /** The whole sentence on where a rule came from, shown on hover. */
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

  /** The short tag on the row. */
  function sourceTag(rule: GuardrailRule): string {
    const { kind } = rule.source;
    if (kind === "person") return person(rule.added_by);
    return kind;
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
    const added = { text, people: draftPeopleOpen ? draftPeople : [] };
    if (await commit([...rules.map(asInput), added], "Added.")) {
      draft = "";
      draftPeople = [];
      draftPeopleOpen = false;
    }
  }

  function closeMenu() {
    menuIndex = null;
  }

  function startEdit(index: number) {
    closeMenu();
    peopleIndex = null;
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

  function startPeople(index: number) {
    closeMenu();
    editingIndex = null;
    peopleIndex = index;
    peopleDraft = [...rules[index].people];
  }

  async function savePeople(index: number) {
    if (busy) return;
    const next = rules.map(asInput);
    next[index] = { ...next[index], people: peopleDraft };
    if (await commit(next, "Saved.")) peopleIndex = null;
  }

  async function reportBroken(rule: GuardrailRule) {
    closeMenu();
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
    closeMenu();
    if (busy) return;
    await commit(
      rules.filter((_, i) => i !== index).map(asInput),
      "Removed.",
    );
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key === "Escape" && menuIndex !== null) closeMenu();
  }
</script>

<svelte:window onkeydown={onKeydown} />

<section class="panel" aria-labelledby="guardrails-panel-heading">
  <div class="panel__header">
    <div class="guardrails__title">
      <h2 id="guardrails-panel-heading" class="panel__heading">Guardrails</h2>
      {#if guardrails.data}
        <p class="guardrails__count">
          {rules.length}
          {rules.length === 1 ? "rule" : "rules"} ·
          {roomLeft > 0 ? `room for about ${roomLeft} more` : "the list is full"}
        </p>
      {/if}
    </div>
    <p class="panel__lead">
      Rules {agentName} follows on every task. A rule guides it; to make sure something
      can't happen, take the permission away on the Access tab.
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

    {#if canEdit}
      <form class="guardrails__add" onsubmit={add}>
        <div class="guardrails__add-row">
          <input
            class="guardrails__input"
            type="text"
            bind:value={draft}
            maxlength={RULE_MAX_CHARS}
            placeholder={`Something ${agentName} should always do, or never do`}
            aria-label="New rule"
            disabled={busy}
          />
          <button
            class="btn"
            type="button"
            aria-expanded={draftPeopleOpen}
            disabled={busy}
            onclick={() => (draftPeopleOpen = !draftPeopleOpen)}
          >
            <Users size={16} aria-hidden="true" />
            People{draftPeopleOpen && draftPeople.length > 0 ? ` (${draftPeople.length})` : ""}
          </button>
          <button class="btn btn--primary" type="submit" disabled={busy || !draft.trim()}>
            Add
          </button>
        </div>
        {#if draftPeopleOpen}
          <div class="guardrails__people-box">
            <p class="guardrails__hint">
              Who this rule is about. Write the rule around them, like "Only answer these
              people".
            </p>
            <PeoplePicker id="guardrails-new-people" bind:people={draftPeople} disabled={busy} />
          </div>
        {/if}
        <p class="guardrails__hint">
          Write each rule so it makes sense on its own: "When searching tickets, use the
          person's full email", not "Use the full email". How to do a job belongs in a skill.
        </p>
      </form>
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
      {#if rules.length >= FIND_FROM}
        <input
          class="guardrails__find"
          type="search"
          bind:value={find}
          placeholder="Find a rule"
          aria-label="Find a rule"
        />
      {/if}
      <ol class="guardrails__list">
        {#each rules as rule, index (`${rule.id ?? "line"}-${index}`)}
          {#if matches(rule)}
            <li class="guardrail">
              <div class="guardrail__body">
                {#if editingIndex === index}
                  <div class="guardrail__edit">
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
                  </div>
                {:else if canEdit}
                  <button
                    type="button"
                    class="guardrail__text guardrail__text--button"
                    aria-label={`Edit the rule: ${rule.text}`}
                    disabled={busy}
                    onclick={() => startEdit(index)}
                  >
                    {rule.text}
                  </button>
                {:else}
                  <p class="guardrail__text">{rule.text}</p>
                {/if}

                {#if peopleIndex === index}
                  <div class="guardrails__people-box">
                    <p class="guardrails__hint">
                      Who this rule is about. {agentName} is told who sent each message and
                      matches them by email.
                    </p>
                    <PeoplePicker
                      id={`guardrail-people-${index}`}
                      bind:people={peopleDraft}
                      disabled={busy}
                    />
                    <div class="guardrail__edit-actions">
                      <button class="btn btn--quiet" type="button" onclick={() => (peopleIndex = null)}>
                        Cancel
                      </button>
                      <button
                        class="btn btn--primary"
                        type="button"
                        disabled={busy}
                        onclick={() => savePeople(index)}
                      >
                        Save people
                      </button>
                    </div>
                  </div>
                {:else if rule.people.length > 0}
                  <p class="guardrail__people">
                    <Users size={14} aria-hidden="true" />
                    <span>These people: {names(rule.people)}</span>
                  </p>
                {/if}

                {#if rule.breaks >= breaksBeforeACheck}
                  <p class="guardrail__nudge">
                    This keeps breaking. A rule only guides; to make it hold, take the permission
                    away on the Access tab.
                  </p>
                {/if}
              </div>

              <div class="guardrail__meta">
                {#if rule.breaks > 0}
                  <span class="guardrail__broke" title={brokenLabel(rule)}>broke {rule.breaks}×</span>
                {/if}
                <span class="guardrail__source" title={sourceLabel(rule)}>{sourceTag(rule)}</span>
                {#if canEdit || (canReport && rule.id !== null)}
                  <div class="guardrail__more-wrap">
                    <button
                      type="button"
                      class="guardrail__more"
                      aria-haspopup="menu"
                      aria-expanded={menuIndex === index}
                      aria-label={`More for the rule: ${rule.text}`}
                      onclick={() => (menuIndex = menuIndex === index ? null : index)}
                    >
                      <DotsThree size={18} weight="bold" aria-hidden="true" />
                    </button>
                    {#if menuIndex === index}
                      <button
                        type="button"
                        class="guardrail__scrim"
                        aria-label="Close the menu"
                        onclick={closeMenu}
                      ></button>
                      <div class="guardrail__menu" role="menu" aria-label="This rule">
                        {#if canEdit}
                          <button
                            type="button"
                            role="menuitem"
                            class="guardrail__item"
                            disabled={busy}
                            onclick={() => startEdit(index)}
                          >
                            Edit
                          </button>
                          <button
                            type="button"
                            role="menuitem"
                            class="guardrail__item"
                            disabled={busy}
                            onclick={() => startPeople(index)}
                          >
                            People{rule.people.length > 0 ? ` (${rule.people.length})` : ""}
                          </button>
                        {/if}
                        {#if canReport && rule.id !== null}
                          <button
                            type="button"
                            role="menuitem"
                            class="guardrail__item"
                            disabled={report.isPending}
                            onclick={() => reportBroken(rule)}
                          >
                            It broke this
                          </button>
                        {/if}
                        {#if canEdit}
                          <button
                            type="button"
                            role="menuitem"
                            class="guardrail__item guardrail__item--danger"
                            disabled={busy}
                            onclick={() => remove(index)}
                          >
                            Remove
                          </button>
                        {/if}
                      </div>
                    {/if}
                  </div>
                {/if}
              </div>
            </li>
          {/if}
        {/each}
      </ol>
      {#if findTerm && !rules.some(matches)}
        <p class="panel__empty">No rule matches "{find.trim()}".</p>
      {/if}
    {/if}
  {/if}
</section>

<style>
  .guardrails__title {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-xs) var(--space-md);
    align-items: baseline;
    justify-content: space-between;
  }

  .guardrails__count {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .guardrails__add {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
  }

  .guardrails__add-row {
    display: flex;
    gap: var(--space-xs);
  }

  .guardrails__add-row .btn {
    display: inline-flex;
    flex-shrink: 0;
    gap: var(--space-2xs);
    align-items: center;
  }

  .guardrails__input {
    flex: 1;
    min-width: 0;
  }

  .guardrails__hint {
    margin: 0;
    color: var(--ink);
    font-size: 0.85rem;
  }

  .guardrails__people-box {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }

  .guardrails__find {
    width: 100%;
    max-width: 22rem;
    margin-top: var(--space-sm);
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
    padding: var(--space-xs) 0;
    border-bottom: 1px solid var(--border);
  }

  .guardrail__body {
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: var(--space-2xs);
    min-width: 0;
  }

  .guardrail__text {
    margin: 0;
    padding: var(--space-2xs) 0;
    color: var(--ink);
    line-height: 1.4;
    overflow-wrap: anywhere;
  }

  .guardrail__text--button {
    border: none;
    border-radius: var(--radius-sm);
    background: none;
    font: inherit;
    text-align: left;
    cursor: text;
  }

  .guardrail__text--button:hover:not(:disabled) {
    background: var(--accent-soft);
  }

  .guardrail__edit {
    display: flex;
    gap: var(--space-xs);
    align-items: center;
  }

  .guardrail__edit-actions {
    display: flex;
    gap: var(--space-xs);
    justify-content: flex-end;
  }

  .guardrail__people {
    display: flex;
    gap: var(--space-2xs);
    align-items: center;
    margin: 0;
    color: var(--ink);
    font-size: 0.85rem;
  }

  .guardrail__nudge {
    margin: 0;
    font-size: 0.85rem;
  }

  .guardrail__meta {
    display: flex;
    flex-shrink: 0;
    gap: var(--space-xs);
    align-items: center;
    padding-top: var(--space-2xs);
  }

  .guardrail__source {
    color: var(--muted);
    font-size: 0.8rem;
    white-space: nowrap;
  }

  .guardrail__broke {
    padding: 0 var(--space-xs);
    border: 1px solid var(--fail);
    border-radius: var(--radius-pill);
    color: var(--fail);
    font-size: 0.75rem;
    white-space: nowrap;
  }

  .guardrail__more-wrap {
    position: relative;
  }

  /* Always on screen, and drawn as a button, so nobody has to hover to find
     it (Kyle, 2026-11-20: "make sure its easy to see"). */
  .guardrail__more {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 2rem;
    height: 2rem;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    cursor: pointer;
  }

  .guardrail__more:hover,
  .guardrail__more[aria-expanded="true"] {
    border-color: var(--ink);
  }

  .guardrail__scrim {
    position: fixed;
    z-index: 3;
    inset: 0;
    border: none;
    background: transparent;
    cursor: default;
  }

  .guardrail__menu {
    position: absolute;
    z-index: 4;
    top: calc(100% + var(--space-2xs));
    right: 0;
    display: flex;
    flex-direction: column;
    min-width: 11rem;
    padding: var(--space-2xs);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    box-shadow: 0 4px 12px rgb(0 0 0 / 0.12);
  }

  .guardrail__item {
    padding: var(--space-xs) var(--space-sm);
    border: none;
    border-radius: var(--radius-sm);
    background: none;
    color: var(--ink);
    font: inherit;
    text-align: left;
    cursor: pointer;
  }

  .guardrail__item:hover:not(:disabled) {
    background: var(--accent-soft);
  }

  .guardrail__item--danger {
    color: var(--fail);
  }
</style>
