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
  // models are offered, in tier order within each group. A self-hosted
  // model (kobo#390) has no tier, since nobody priced it; it is offered
  // under its row's name whatever the room's own list does.
  const SELF_HOSTED = "self-hosted";
  const tiered = $derived(models.some((m) => m.family && m.tier));
  const offered = $derived(tiered ? models.filter((m) => (m.family && m.tier) || m.provider === SELF_HOSTED) : models);

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
    if (!tiered || !model.tier) {
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
  .models {
    display: flex;
    flex-direction: column;
    gap: 2px;
    max-height: 13rem;
    overflow-y: auto;
    padding: var(--space-xs);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
  }
  .models__group {
    margin: var(--space-xs) 0 2px;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .models__group:first-child {
    margin-top: 0;
  }
  .model-row {
    display: flex;
    align-items: flex-start;
    gap: var(--space-xs);
    padding: 4px var(--space-xs);
    border-radius: var(--radius-sm);
    color: var(--ink);
    cursor: pointer;
  }
  .model-row:hover {
    background: var(--bg);
  }
  .model-row--off {
    cursor: default;
  }
  .model-row input {
    margin-top: 3px;
  }
  .model-row__text {
    display: flex;
    flex-direction: column;
    font-size: 0.9rem;
  }
  .model-row__name {
    color: var(--ink);
  }
  .model-row__reason {
    font-size: 0.78rem;
    color: var(--muted);
  }
</style>
