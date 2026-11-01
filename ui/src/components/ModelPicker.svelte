<script lang="ts">
  // The model list, shared by New agent and the Harness's Change model.
  // Grouped by the cloud that serves each model when the room's model
  // gateway is on (`provider_label`), by family when it is not; a model
  // whose cloud is not connected, or that the gateway cannot reach, stays
  // in the list but cannot be picked: its name in ink, the reason in grey.
  // A list of radios rather than a select, because a select cannot say why
  // a row is off.
  import type { CatalogModel } from "../lib/api";

  let {
    models,
    value,
    onchange,
    disabled = false,
    name,
    label = "Model",
  }: {
    models: CatalogModel[];
    value: string;
    onchange: (id: string) => void;
    disabled?: boolean;
    /** The radio group's name; one picker per form. */
    name: string;
    label?: string;
  } = $props();

  const TIER_ORDER = ["best", "balanced", "fast"];
  const TIER_LABELS: Record<string, string> = {
    best: "Best (most expensive)",
    balanced: "Balanced",
    fast: "Fast (cheapest)",
  };
  function tierLabel(tier: string | null | undefined): string {
    return tier ? (TIER_LABELS[tier] ?? tier) : "";
  }

  const byProvider = $derived(models.some((m) => m.provider_label));
  // Tiers (2026-09-29): when the room gives any model a tier, only tiered
  // models are offered, in tier order within each group.
  const tiered = $derived(models.some((m) => m.family && m.tier));
  const offered = $derived(tiered ? models.filter((m) => m.family && m.tier) : models);

  type Group = { name: string; models: CatalogModel[] };
  const groups = $derived.by((): Group[] => {
    const out: Group[] = [];
    for (const model of offered) {
      const key = byProvider ? (model.provider_label ?? "Other") : tiered ? (model.family as string) : "";
      let group = out.find((g) => g.name === key);
      if (!group) {
        group = { name: key, models: [] };
        out.push(group);
      }
      group.models.push(model);
    }
    if (tiered) {
      for (const group of out) {
        group.models.sort(
          (a, b) =>
            (a.family ?? "").localeCompare(b.family ?? "") ||
            TIER_ORDER.indexOf(a.tier as string) - TIER_ORDER.indexOf(b.tier as string),
        );
      }
    }
    return out;
  });

  function rowLabel(model: CatalogModel): string {
    if (!tiered) {
      return model.label;
    }
    return byProvider ? `${model.family}, ${tierLabel(model.tier)}` : tierLabel(model.tier);
  }
</script>

<div class="models" role="radiogroup" aria-label={label}>
  {#each groups as group (group.name)}
    {#if group.name}
      <p class="models__group">{group.name}</p>
    {/if}
    {#each group.models as model (model.id)}
      {@const off = model.available === false}
      <label class="model-row" class:model-row--off={off} aria-disabled={off || undefined}>
        <input
          type="radio"
          {name}
          value={model.id}
          checked={value === model.id}
          disabled={disabled || off}
          onchange={() => onchange(model.id)}
        />
        <span class="model-row__text">
          <span class="model-row__name">{rowLabel(model)}</span>
          {#if off && model.reason}
            <span class="model-row__reason">{model.reason}</span>
          {/if}
        </span>
      </label>
    {/each}
  {/each}
</div>

<style>
  /* Drawn like the persona list beside it: hairline-divided rows, a group
     label in small caps grey over each run, the name in ink and the reason
     a row is off in grey on the same line. */
  .models {
    display: flex;
    flex-direction: column;
    max-height: 13rem;
    min-width: 0;
    overflow-y: auto;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
  }
  .models__group {
    margin: 0;
    padding: var(--space-xs) var(--space-sm) var(--space-2xs);
    border-bottom: 1px solid var(--border);
    color: var(--muted);
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .model-row {
    display: flex;
    min-height: 2.5rem;
    align-items: center;
    gap: var(--space-xs);
    padding: var(--space-2xs) var(--space-sm);
    border-bottom: 1px solid var(--border);
    color: var(--ink);
    cursor: pointer;
  }
  .model-row:last-child {
    border-bottom: none;
  }
  .model-row:hover {
    background: var(--accent-soft);
  }
  /* Dimmed, still readable: the name stays ink, the reason says why. */
  .model-row--off {
    opacity: 0.7;
    cursor: default;
  }
  .model-row--off:hover {
    background: none;
  }
  .model-row input {
    flex: none;
    margin: 0;
  }
  .model-row__text {
    display: flex;
    flex: 1;
    min-width: 0;
    flex-wrap: wrap;
    align-items: baseline;
    column-gap: var(--space-xs);
    font-size: 0.875rem;
  }
  .model-row__name {
    color: var(--ink);
  }
  .model-row__reason {
    color: var(--muted);
    font-size: 0.8rem;
  }
</style>
