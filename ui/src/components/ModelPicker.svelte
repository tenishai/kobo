<script lang="ts">
  // The model list, shared by New agent and the Harness's Change model.
  // One list across the clouds (kobo#405): each row names the model and
  // carries a small grey tag saying where it comes from (`provider_tag`:
  // GCP, AWS, Azure, Self-hosted) when the room's model gateway is on.
  // Tiered models sort by family, then tier, so a family's tiers sit
  // together; a model whose cloud is not connected, or that the gateway
  // cannot reach, stays in the list but cannot be picked: its name in ink,
  // the reason in grey. A list of radios rather than a select, because a
  // select cannot say why a row is off.
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

  const tagged = $derived(models.some((m) => m.provider_tag));
  // Tiers (2026-09-29): when the room gives any model a tier, only tiered
  // models are offered, a family's tiers together and in order. A model an
  // admin ticked from a cloud's own list, or a self-hosted one (kobo#390),
  // has no tier, since nobody priced it; with the gateway on it is offered
  // too, by its own name, after the tiered ones.
  const tiered = $derived(models.some((m) => m.family && m.tier));
  const offered = $derived.by((): CatalogModel[] => {
    const rows = tiered ? models.filter((m) => (m.family && m.tier) || tagged) : models.slice();
    if (!tiered) return rows;
    const rank = (m: CatalogModel): number => (m.family && m.tier ? 0 : 1);
    return rows.sort(
      (a, b) =>
        rank(a) - rank(b) ||
        (a.family ?? "").localeCompare(b.family ?? "") ||
        TIER_ORDER.indexOf(a.tier as string) - TIER_ORDER.indexOf(b.tier as string),
    );
  });

  function rowLabel(model: CatalogModel): string {
    if (!tiered || !model.tier || !model.family) {
      return model.label;
    }
    return `${model.family}, ${tierLabel(model.tier)}`;
  }
</script>

<div class="models" role="radiogroup" aria-label={label}>
  {#each offered as model (model.id)}
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
        {#if model.provider_tag}
          <span class="model-row__tag">{model.provider_tag}</span>
        {/if}
        {#if off && model.reason}
          <span class="model-row__reason">{model.reason}</span>
        {/if}
      </span>
    </label>
  {/each}
</div>

<style>
  /* Drawn like the persona list beside it: hairline-divided rows, the name
     in ink, the cloud's tag as a small grey chip after it (the room's
     metadata style) and the reason a row is off in grey on the same line. */
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
  .model-row__tag {
    padding: 0 6px;
    border: 1px solid var(--border);
    border-radius: var(--radius-pill);
    color: var(--muted);
    font-size: 0.68rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    line-height: 1.5;
    text-transform: uppercase;
  }
  .model-row__reason {
    color: var(--muted);
    font-size: 0.8rem;
  }
</style>
