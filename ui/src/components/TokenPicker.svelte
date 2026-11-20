<script lang="ts">
  // A list of short names a guardrail rule is about (2026-11-20): emoji,
  // Slack channels or repositories. The names picked show as chips; one box
  // takes a name typed in full, on Enter, a comma or when it loses focus (so
  // a name typed and never entered still goes with Save or Add), and offers
  // the `suggestions` that match what is typed while it has focus. `normalize`
  // is the kind's own rule (lib/ruleLists.ts, mirroring the server); a name it
  // refuses on Enter stays in the box with `invalid` under it. Leaving the box
  // never shows that line: it would move the page under the pointer between
  // the press and the click.
  //
  // `values` is bound, or read and given back through `onchange`: the panel
  // that holds the rule saves it.
  import { X } from "phosphor-svelte";

  let {
    id,
    label,
    values = $bindable([]),
    onchange,
    normalize,
    show = (value: string) => value,
    placeholder,
    invalid,
    suggestions = [],
    disabled = false,
  }: {
    id: string;
    label: string;
    values?: string[];
    onchange?: (values: string[]) => void;
    normalize: (raw: string) => string | null;
    show?: (value: string) => string;
    placeholder: string;
    invalid: string;
    suggestions?: string[];
    disabled?: boolean;
  } = $props();

  let term = $state("");
  let refused = $state(false);
  let focused = $state(false);

  const picked = $derived(new Set(values.map((value) => value.toLowerCase())));
  const matches = $derived.by((): string[] => {
    const typed = term.trim().replace(/^[:#]+|:+$/g, "").toLowerCase();
    if (!typed) return [];
    return suggestions
      .filter((value) => !picked.has(value.toLowerCase()) && value.toLowerCase().includes(typed))
      .slice(0, 8);
  });

  function add(value: string): void {
    if (!picked.has(value.toLowerCase())) {
      values = [...values, value];
      onchange?.(values);
    }
    term = "";
    refused = false;
  }

  function remove(value: string): void {
    values = values.filter((other) => other !== value);
    onchange?.(values);
  }

  /** What is typed, added when it is a name of this kind. */
  function addTyped(): boolean {
    if (!term.trim()) return false;
    const value = normalize(term);
    if (value === null) return false;
    add(value);
    return true;
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key !== "Enter" && event.key !== ",") return;
    event.preventDefault();
    if (addTyped()) return;
    if (event.key === "Enter" && matches.length === 1) {
      add(matches[0]);
    } else if (term.trim()) {
      refused = true;
    }
  }

  function onBlur(): void {
    focused = false;
    addTyped();
  }
</script>

<div class="tokens">
  {#if values.length > 0}
    <ul class="tokens__chips" aria-label={label}>
      {#each values as value (value)}
        <li class="tokens__chip">
          <span>{show(value)}</span>
          <button
            type="button"
            class="tokens__remove"
            aria-label={`Take ${show(value)} off this rule`}
            {disabled}
            onclick={() => remove(value)}
          >
            <X size={12} weight="bold" aria-hidden="true" />
          </button>
        </li>
      {/each}
    </ul>
  {/if}
  <div class="tokens__search">
    <label class="panel__sr" for={id}>{label}</label>
    <input
      {id}
      class="field__input"
      type="text"
      autocomplete="off"
      spellcheck="false"
      {placeholder}
      bind:value={term}
      oninput={() => (refused = false)}
      onkeydown={onKeydown}
      onfocus={() => (focused = true)}
      onblur={onBlur}
      {disabled}
    />
    {#if focused && matches.length > 0}
      <!-- Pressing a match keeps the focus in the box, so the list is still
           there for the click. -->
      <ul
        class="tokens__suggestions"
        role="listbox"
        aria-label={`${label} found`}
        onpointerdown={(event) => event.preventDefault()}
      >
        {#each matches as value (value)}
          <li role="option" aria-selected="false">
            <button type="button" class="tokens__suggestion" {disabled} onclick={() => add(value)}>
              {show(value)}
            </button>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
  {#if refused}
    <p class="panel__note panel__note--fail" role="alert">{invalid}</p>
  {/if}
</div>

<style>
  .tokens {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
  }

  .tokens__chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .tokens__chip {
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

  .tokens__remove {
    display: inline-flex;
    padding: 0.15rem;
    border: none;
    border-radius: 50%;
    background: none;
    color: var(--ink);
    cursor: pointer;
  }

  .tokens__remove:hover:not(:disabled) {
    background: var(--accent-soft);
  }

  .tokens__search {
    position: relative;
  }

  .tokens__search input {
    width: 100%;
  }

  .tokens__suggestions {
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
    box-shadow: 0 4px 12px rgb(0 0 0 / 0.12);
    list-style: none;
  }

  .tokens__suggestion {
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

  .tokens__suggestion:hover:not(:disabled) {
    background: var(--accent-soft);
  }
</style>
