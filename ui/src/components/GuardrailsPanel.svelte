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
  // The menu holds Edit, the four lists below, It broke this and Remove.
  // Clicking the words edits them too. A long list gets a box to find a rule in.
  //
  // What a rule is about (Kyle, same day: "other things we want rules based
  // off of, like emoji types"): people, emoji, Slack channels and
  // repositories. They ride on the rule's own line
  // (`kobo_api.guardrails.compose_line`); the agent is told who sent each Slack
  // message and where, and which emoji woke it, and repositories are the ones
  // its tools act on. A new rule takes its lists from the ⋯ beside Add before
  // it is added, so a rule like "Only answer these people" is never live for a
  // moment without them.
  //
  // `agentName` is read once, as in the other Harness panels: AgentDetail
  // remounts this component per agent.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { Check, DotsThree, GitBranch, Hash, Smiley, Users } from "phosphor-svelte";
  import ErrorState from "./ErrorState.svelte";
  import PeoplePicker from "./PeoplePicker.svelte";
  import TokenPicker from "./TokenPicker.svelte";
  import {
    errorMessage,
    type GuardrailRule,
    type GuardrailRuleIn,
    type Person,
  } from "../lib/api";
  import {
    githubRepositoriesQuery,
    guardrailsQuery,
    reportGuardrailBrokenMutation,
    saveGuardrailsMutation,
  } from "../lib/queries";
  import { channelName, COMMON_EMOJI, emojiName, repositoryName } from "../lib/ruleLists";
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

  // What a rule can name, in the order its line carries them.
  type Kind = "people" | "emoji" | "channels" | "repositories";
  type Lists = { people: Person[]; emoji: string[]; channels: string[]; repositories: string[] };
  const KINDS: Array<{ kind: Kind; label: string; hint: string }> = [
    {
      kind: "people",
      label: "People",
      hint: "Who this rule is about. It is told who sent each Slack message and matches them by email.",
    },
    {
      kind: "emoji",
      label: "Emoji",
      hint: "Emoji this rule is about. It is told which emoji a reaction that woke it used.",
    },
    {
      kind: "channels",
      label: "Slack channels",
      hint: "Slack channels this rule is about. It is told which channel each message came from.",
    },
    {
      kind: "repositories",
      label: "Repositories",
      hint: "Repositories this rule is about, as owner/name. It names the repository in all it does there.",
    },
  ];
  const noLists = (): Lists => ({ people: [], emoji: [], channels: [], repositories: [] });

  let draft = $state("");
  let draftLists = $state<Lists>(noLists());
  // The lists open for the new rule, in KINDS order.
  let draftOpen = $state<Kind[]>([]);
  let addMenuOpen = $state(false);
  // An open editor names its rule by key, not by place: a save refused because the
  // list changed underneath reloads it, and a place could then be another rule.
  let editingKey = $state<string | null>(null);
  let editText = $state("");
  let listKey = $state<string | null>(null);
  let listKind = $state<Kind>("people");
  let listDraft = $state<Lists>(noLists());
  // Pickers holding text they can't add (a search not picked, a name that isn't
  // one): Add or Save waits, so typed text is never dropped without a word.
  let draftPending = $state<Kind[]>([]);
  let editPending = $state(false);
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
  // The box shows from FIND_FROM rules; below that nothing filters, so a removal
  // that hides the box can't leave rules hidden behind it.
  const findTerm = $derived(rules.length >= FIND_FROM ? find.trim().toLowerCase() : "");
  // A list open for the new rule with nothing in it: Add waits, so a rule like
  // "Only answer these people" is never live without them.
  const addNeedsLists = $derived(draftOpen.some((kind) => draftLists[kind].length === 0));
  const addPending = $derived(draftPending.some((kind) => draftOpen.includes(kind)));

  function setDraftPending(kind: Kind, pending: boolean): void {
    // Only on a change: a new array each time would hand every picker a fresh
    // callback and have it report again, without end.
    if (draftPending.includes(kind) === pending) return;
    draftPending = pending ? [...draftPending, kind] : draftPending.filter((k) => k !== kind);
  }
  // The room's repositories, offered as someone types, read only once a
  // Repositories list is open.
  const reposWanted = $derived(
    draftOpen.includes("repositories") || (listKey !== null && listKind === "repositories"),
  );
  // svelte-ignore state_referenced_locally
  const repositories = createQuery(() => ({
    ...githubRepositoriesQuery(agentName),
    enabled: reposWanted,
  }));
  const repoSuggestions = $derived.by((): string[] => {
    const data = repositories.data;
    if (!data?.available) return [];
    const selected = new Set(data.selected);
    const names = data.repositories.map((repo) => repo.full_name);
    return [...names.filter((n) => selected.has(n)), ...names.filter((n) => !selected.has(n))];
  });

  function matches(rule: GuardrailRule): boolean {
    if (!findTerm) return true;
    if (rule.text.toLowerCase().includes(findTerm)) return true;
    if (
      rule.people.some(
        (person) =>
          person.name.toLowerCase().includes(findTerm) ||
          person.email.toLowerCase().includes(findTerm),
      )
    ) {
      return true;
    }
    return [...rule.emoji, ...rule.channels, ...rule.repositories].some((value) =>
      value.toLowerCase().includes(findTerm),
    );
  }

  function listsOf(rule: GuardrailRule): Lists {
    return {
      people: rule.people,
      emoji: rule.emoji,
      channels: rule.channels,
      repositories: rule.repositories,
    };
  }

  function asInput(rule: GuardrailRule): GuardrailRuleIn {
    return { id: rule.id, text: rule.text, ...listsOf(rule) };
  }

  /** A rule's key: its row's id, or for a line written in the repository, the
   * line itself. */
  function keyOf(rule: GuardrailRule): string {
    return rule.id !== null ? `id:${rule.id}` : `line:${rule.line}`;
  }

  /** Where the rule an editor names sits in the list now, or -1. */
  function placeOf(key: string | null): number {
    return key === null ? -1 : rules.findIndex((rule) => keyOf(rule) === key);
  }

  function person(email: string | null): string {
    if (!email || email === "room") return "the room";
    return email.split("@", 1)[0];
  }

  function names(people: Person[]): string {
    return people.map((p) => p.name || p.email).join(", ");
  }

  function countOf(rule: GuardrailRule, kind: Kind): number {
    return rule[kind].length;
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
    if (!text || busy || addNeedsLists || addPending) return;
    const added: GuardrailRuleIn = { text, ...noLists() };
    for (const kind of draftOpen) {
      (added as Record<Kind, unknown>)[kind] = draftLists[kind];
    }
    if (await commit([...rules.map(asInput), added], "Added.")) {
      draft = "";
      draftLists = noLists();
      draftOpen = [];
      draftPending = [];
    }
  }

  function toggleDraftKind(kind: Kind): void {
    addMenuOpen = false;
    if (draftOpen.includes(kind)) {
      draftOpen = draftOpen.filter((open) => open !== kind);
      setDraftPending(kind, false);
    } else {
      draftOpen = KINDS.map((k) => k.kind).filter((k) => k === kind || draftOpen.includes(k));
    }
  }

  function closeMenu() {
    menuIndex = null;
  }

  function startEdit(rule: GuardrailRule) {
    closeMenu();
    listKey = null;
    editingKey = keyOf(rule);
    editText = rule.text;
  }

  function gone(): void {
    errorText = "That rule changed or went while you had it open. Here is the list as it stands.";
    editingKey = null;
    listKey = null;
  }

  async function saveEdit() {
    const text = editText.trim();
    if (!text || busy) return;
    const index = placeOf(editingKey);
    if (index < 0) return gone();
    const next = rules.map(asInput);
    next[index] = { ...next[index], text };
    if (await commit(next, "Saved.")) editingKey = null;
  }

  function startList(rule: GuardrailRule, kind: Kind) {
    closeMenu();
    editingKey = null;
    listKey = keyOf(rule);
    listKind = kind;
    listDraft = { ...listsOf(rule), [kind]: [...rule[kind]] };
    editPending = false;
  }

  async function saveList() {
    if (busy || editPending) return;
    const index = placeOf(listKey);
    if (index < 0) return gone();
    const next = rules.map(asInput);
    next[index] = { ...next[index], [listKind]: listDraft[listKind] };
    if (await commit(next, "Saved.")) listKey = null;
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

  async function remove(rule: GuardrailRule) {
    closeMenu();
    if (busy) return;
    const index = placeOf(keyOf(rule));
    if (index < 0) return gone();
    await commit(
      rules.filter((_, i) => i !== index).map(asInput),
      "Removed.",
    );
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key !== "Escape") return;
    if (menuIndex !== null) closeMenu();
    addMenuOpen = false;
  }
</script>

<svelte:window onkeydown={onKeydown} />

<!-- One kind's picker. Given the lists and how to change them, so the new
     rule and a rule being edited share it. -->
{#snippet picker(
  kind: Kind,
  lists: Lists,
  set: (kind: Kind, values: Person[] | string[]) => void,
  pend: (kind: Kind, pending: boolean) => void,
  id: string,
)}
  {#if kind === "people"}
    <PeoplePicker
      {id}
      people={lists.people}
      onchange={(v) => set("people", v)}
      onpending={(p) => pend("people", p)}
      disabled={busy}
    />
  {:else if kind === "emoji"}
    <TokenPicker
      {id}
      label="Emoji"
      values={lists.emoji}
      onchange={(v) => set("emoji", v)}
      onpending={(p) => pend("emoji", p)}
      normalize={emojiName}
      show={(v) => `:${v}:`}
      placeholder="An emoji name, like ticket or +1"
      invalid="That isn't an emoji name. Use the name Slack shows for it, like ticket."
      suggestions={COMMON_EMOJI}
      disabled={busy}
    />
  {:else if kind === "channels"}
    <TokenPicker
      {id}
      label="Slack channels"
      values={lists.channels}
      onchange={(v) => set("channels", v)}
      onpending={(p) => pend("channels", p)}
      normalize={channelName}
      show={(v) => `#${v}`}
      placeholder="A channel name, like sre-requests"
      invalid="That isn't a Slack channel name. Use the name Slack shows, like sre-requests."
      disabled={busy}
    />
  {:else}
    <TokenPicker
      {id}
      label="Repositories"
      values={lists.repositories}
      onchange={(v) => set("repositories", v)}
      onpending={(p) => pend("repositories", p)}
      normalize={repositoryName}
      placeholder="owner/name"
      invalid="That isn't a repository. Use owner/name, like the Access tab shows."
      suggestions={repoSuggestions}
      disabled={busy}
    />
  {/if}
{/snippet}

<!-- What a rule names, on one wrapped line under its words. -->
{#snippet named(lists: Lists)}
  {#if lists.people.length + lists.emoji.length + lists.channels.length + lists.repositories.length > 0}
    <p class="guardrail__lists">
      {#if lists.people.length > 0}
        <span class="guardrail__list"><Users size={14} aria-hidden="true" />These people: {names(lists.people)}</span>
      {/if}
      {#if lists.emoji.length > 0}
        <span class="guardrail__list"><Smiley size={14} aria-hidden="true" />These emoji: {lists.emoji.map((e) => `:${e}:`).join(" ")}</span>
      {/if}
      {#if lists.channels.length > 0}
        <span class="guardrail__list"><Hash size={14} aria-hidden="true" />These channels: {lists.channels.map((c) => `#${c}`).join(", ")}</span>
      {/if}
      {#if lists.repositories.length > 0}
        <span class="guardrail__list"><GitBranch size={14} aria-hidden="true" />These repositories: {lists.repositories.join(", ")}</span>
      {/if}
    </p>
  {/if}
{/snippet}

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
          <div class="guardrail__more-wrap">
            <button
              type="button"
              class="guardrail__more"
              aria-haspopup="menu"
              aria-expanded={addMenuOpen}
              aria-label="Add people, emoji, channels or repositories to the new rule"
              title="People, emoji, channels or repositories"
              disabled={busy}
              onclick={() => (addMenuOpen = !addMenuOpen)}
            >
              <DotsThree size={18} weight="bold" aria-hidden="true" />
            </button>
            {#if addMenuOpen}
              <button
                type="button"
                class="guardrail__scrim"
                aria-label="Close the menu"
                onclick={() => (addMenuOpen = false)}
              ></button>
              <div class="guardrail__menu" role="menu" aria-label="The new rule">
                {#each KINDS as item (item.kind)}
                  <button
                    type="button"
                    role="menuitemcheckbox"
                    aria-checked={draftOpen.includes(item.kind)}
                    class="guardrail__item"
                    onclick={() => toggleDraftKind(item.kind)}
                  >
                    <span class="guardrail__tick" aria-hidden="true">
                      {#if draftOpen.includes(item.kind)}<Check size={14} weight="bold" />{/if}
                    </span>
                    {item.label}
                  </button>
                {/each}
              </div>
            {/if}
          </div>
          <button
            class="btn btn--primary"
            type="submit"
            disabled={busy || !draft.trim() || addNeedsLists || addPending}
          >
            Add
          </button>
        </div>
        {#each KINDS.filter((k) => draftOpen.includes(k.kind)) as item (item.kind)}
          <div class="guardrails__people-box">
            <p class="guardrails__hint">
              <strong>{item.label}.</strong> {item.hint}{draftPending.includes(item.kind)
                ? " Finish or clear what's typed here, to add the rule."
                : draftLists[item.kind].length === 0
                  ? ` Add at least one, or untick ${item.label} in the ⋯ menu, to add the rule.`
                  : ""}
            </p>
            {@render picker(
              item.kind,
              draftLists,
              (kind, values) => (draftLists = { ...draftLists, [kind]: values }),
              setDraftPending,
              `guardrails-new-${item.kind}`,
            )}
          </div>
        {/each}
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
                {#if editingKey === keyOf(rule)}
                  <div class="guardrail__edit">
                    <input
                      class="guardrails__input"
                      type="text"
                      bind:value={editText}
                      maxlength={RULE_MAX_CHARS}
                      aria-label="Edit the rule"
                      disabled={busy}
                      onkeydown={(event) => {
                        if (event.key === "Escape") editingKey = null;
                        if (event.key === "Enter") saveEdit();
                      }}
                    />
                    <button class="btn btn--quiet" type="button" onclick={() => (editingKey = null)}>
                      Cancel
                    </button>
                    <button
                      class="btn btn--primary"
                      type="button"
                      disabled={busy || !editText.trim()}
                      onclick={() => saveEdit()}
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
                    onclick={() => startEdit(rule)}
                  >
                    {rule.text}
                  </button>
                {:else}
                  <p class="guardrail__text">{rule.text}</p>
                {/if}

                {#if listKey === keyOf(rule)}
                  {@const item = KINDS.find((k) => k.kind === listKind) ?? KINDS[0]}
                  <div class="guardrails__people-box">
                    <p class="guardrails__hint"><strong>{item.label}.</strong> {item.hint}</p>
                    {@render picker(
                      listKind,
                      listDraft,
                      (kind, values) => (listDraft = { ...listDraft, [kind]: values }),
                      (_kind, pending) => (editPending = pending),
                      `guardrail-${listKind}-${index}`,
                    )}
                    <div class="guardrail__edit-actions">
                      {#if editPending}
                        <span class="guardrail__pending">Finish or clear what's typed, to save.</span>
                      {/if}
                      <button class="btn btn--quiet" type="button" onclick={() => (listKey = null)}>
                        Cancel
                      </button>
                      <button
                        class="btn btn--primary"
                        type="button"
                        disabled={busy || editPending}
                        onclick={() => saveList()}
                      >
                        Save {item.label.toLowerCase()}
                      </button>
                    </div>
                  </div>
                {:else}
                  {@render named(listsOf(rule))}
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
                            onclick={() => startEdit(rule)}
                          >
                            Edit
                          </button>
                          <span class="guardrail__menu-sep" role="separator"></span>
                          {#each KINDS as item (item.kind)}
                            <button
                              type="button"
                              role="menuitem"
                              class="guardrail__item"
                              disabled={busy}
                              onclick={() => startList(rule, item.kind)}
                            >
                              {item.label}{countOf(rule, item.kind) > 0 ? ` (${countOf(rule, item.kind)})` : ""}
                            </button>
                          {/each}
                          <span class="guardrail__menu-sep" role="separator"></span>
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
                            onclick={() => remove(rule)}
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

  .guardrail__pending {
    margin-right: auto;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .guardrail__edit-actions {
    display: flex;
    gap: var(--space-xs);
    justify-content: flex-end;
  }

  .guardrail__lists {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2xs) var(--space-md);
    margin: 0;
    color: var(--ink);
    font-size: 0.85rem;
  }

  .guardrail__list {
    display: inline-flex;
    gap: var(--space-2xs);
    align-items: center;
  }

  .guardrail__tick {
    display: inline-flex;
    width: 1rem;
    margin-right: var(--space-2xs);
    vertical-align: -0.125rem;
  }

  .guardrail__menu-sep {
    height: 1px;
    margin: var(--space-2xs) 0;
    background: var(--border);
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
