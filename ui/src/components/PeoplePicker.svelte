<script lang="ts">
  // Who a guardrail rule is about (Kyle, 2026-11-20: "only respond to these
  // people"). The people picked show as chips; one box searches the room's
  // directory the way Owners does, and takes a full email typed in when the
  // directory can't be searched. The rule's own words say what to do about
  // them; the agent is told who sent each message and matches by email.
  //
  // `people` is bound: the panel that holds the rule saves it.
  import { createQuery } from "@tanstack/svelte-query";
  import { X } from "phosphor-svelte";
  import type { Person } from "../lib/api";
  import { debounced } from "../lib/debounce";
  import { peopleSearchQuery } from "../lib/queries";

  let {
    id,
    people = $bindable([]),
    disabled = false,
  }: { id: string; people?: Person[]; disabled?: boolean } = $props();

  // The same email shape the server takes (kobo_api.guardrails._EMAIL).
  const EMAIL = /^[^@\s<>;()]+@[^@\s<>;()]+\.[^@\s<>;()]+$/;

  let term = $state("");
  let searchTerm = $state("");
  const results = createQuery(() => peopleSearchQuery(searchTerm));
  const commitSearch = debounced((value: string) => {
    searchTerm = value;
  }, 250);

  $effect(() => {
    const value = term;
    commitSearch(value);
    return () => {
      commitSearch.cancel();
    };
  });

  const picked = $derived(new Set(people.map((person) => person.email.toLowerCase())));
  const suggestions = $derived(
    (results.data?.people ?? []).filter((person) => !picked.has(person.email.toLowerCase())),
  );
  const unavailable = $derived(results.data?.unavailable === true);
  const showSuggestions = $derived(term.trim().length >= 2 && suggestions.length > 0);

  function add(person: Person): void {
    if (!picked.has(person.email.toLowerCase())) {
      people = [...people, { name: person.name, email: person.email }];
    }
    term = "";
    searchTerm = "";
  }

  function remove(email: string): void {
    people = people.filter((person) => person.email !== email);
  }

  /** Enter adds a full email as typed, or the one person the search found. */
  function onKeydown(event: KeyboardEvent): void {
    if (event.key !== "Enter") {
      return;
    }
    event.preventDefault();
    const typed = term.trim();
    if (EMAIL.test(typed)) {
      const known = suggestions.find((person) => person.email.toLowerCase() === typed.toLowerCase());
      add(known ?? { name: "", email: typed });
    } else if (suggestions.length === 1) {
      add(suggestions[0]);
    }
  }
</script>

<div class="people">
  {#if people.length > 0}
    <ul class="people__chips" aria-label="People this rule is about">
      {#each people as person (person.email)}
        <li class="people__chip" title={person.email}>
          <span>{person.name || person.email}</span>
          <button
            type="button"
            class="people__remove"
            aria-label={`Take ${person.name || person.email} off this rule`}
            {disabled}
            onclick={() => remove(person.email)}
          >
            <X size={12} weight="bold" aria-hidden="true" />
          </button>
        </li>
      {/each}
    </ul>
  {/if}
  <div class="people__search">
    <label class="panel__sr" for={id}>Add a person</label>
    <input
      {id}
      class="field__input"
      type="text"
      autocomplete="off"
      spellcheck="false"
      placeholder="Add a person by name or email"
      bind:value={term}
      onkeydown={onKeydown}
      {disabled}
    />
    {#if showSuggestions}
      <ul class="people__suggestions" role="listbox" aria-label="People found">
        {#each suggestions as person (person.email)}
          <li role="option" aria-selected="false">
            <button
              type="button"
              class="people__suggestion"
              {disabled}
              onclick={() => add(person)}
            >
              {person.name}
              <span class="people__email">{person.email}</span>
            </button>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
  {#if unavailable}
    <p class="panel__note">
      The directory can't be searched right now. Type a full email and press Enter.
    </p>
  {/if}
</div>

<style>
  .people {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
  }

  .people__chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .people__chip {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2xs);
    padding: 0.15rem 0.25rem 0.15rem var(--space-xs);
    border: 1px solid var(--border);
    border-radius: var(--radius-pill);
    background: var(--panel);
    color: var(--ink);
    font-size: 0.85rem;
  }

  .people__remove {
    display: inline-flex;
    padding: 0.15rem;
    border: none;
    border-radius: 50%;
    background: none;
    color: var(--ink);
    cursor: pointer;
  }

  .people__remove:hover:not(:disabled) {
    background: var(--accent-soft);
  }

  .people__search {
    position: relative;
  }

  .people__search input {
    width: 100%;
  }

  .people__suggestions {
    position: absolute;
    z-index: 2;
    top: calc(100% + var(--space-2xs));
    left: 0;
    right: 0;
    display: flex;
    flex-direction: column;
    margin: 0;
    padding: var(--space-2xs);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    box-shadow: var(--shadow-md, 0 4px 12px rgb(0 0 0 / 0.12));
    list-style: none;
  }

  .people__suggestion {
    display: flex;
    gap: var(--space-xs);
    align-items: baseline;
    width: 100%;
    padding: var(--space-2xs) var(--space-xs);
    border: none;
    border-radius: var(--radius-sm);
    background: none;
    color: var(--ink);
    font: inherit;
    text-align: left;
    cursor: pointer;
  }

  .people__suggestion:hover:not(:disabled) {
    background: var(--accent-soft);
  }

  .people__email {
    color: var(--muted);
    font-size: 0.8rem;
  }
</style>
