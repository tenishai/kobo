<script lang="ts">
  // The provisioning checklist: one row per `steps[]` entry, in the order
  // the API returns them. `stepTone` (status.ts) is the single source of
  // truth for done/failed/active/skipped -- pending and running both come
  // back "active" on purpose, so a queued step reads exactly like one
  // already in flight (the whole remaining queue looks like "working", not
  // idle).
  //
  // Skipped gets its own tone, not folded into "ok": a skipped step is a
  // real thing that didn't happen (no picture, say), and rendering it as a
  // plain green check would tell the viewer something happened when it
  // didn't. It gets a neutral marker and, like a failed step, its own
  // reason shown -- the reason text is already sanitized, safe copy (e.g.
  // "picture generation is not available").
  //
  // `doneCount` (the sr-only "N of M steps done" line below) only counts
  // the "ok" tone -- a skipped step is honestly not done, so it is
  // excluded from N; M stays every step, skipped included.
  //
  // Accessibility: the row list itself is plain, static markup -- no
  // aria-live here, since announcing all seven rows on every ~3.5s poll
  // would be noisy. Instead a single visually-hidden line above the list
  // carries `role="status" aria-live="polite"` and only ever says how many
  // steps are done out of the total, so a screen reader hears one short,
  // meaningful update ("4 of 7 steps done") each time that count changes,
  // and stays silent on polls where nothing did.
  import { stepTone } from "../lib/status";
  import type { StepView } from "../lib/api";

  let { steps }: { steps: StepView[] } = $props();

  const doneCount = $derived(
    steps.filter((step) => stepTone(step.status) === "ok").length,
  );

  function toneWord(tone: "ok" | "fail" | "active" | "skipped"): string {
    switch (tone) {
      case "ok":
        return "done";
      case "fail":
        return "failed";
      case "skipped":
        return "skipped";
      default:
        return "in progress";
    }
  }
</script>

<div class="step-checklist">
  <p class="step-checklist__sr-only" role="status" aria-live="polite">
    {doneCount} of {steps.length} steps done
  </p>
  <ul class="step-checklist__list">
    {#each steps as step (step.name)}
      {@const tone = stepTone(step.status)}
      <li class="step-row">
        <span class="step-row__line">
          <span
            class="step-row__icon step-row__icon--{tone}"
            aria-hidden="true"
          >
            {#if tone === "active"}
              <span class="step-row__dot pulse"></span>
            {:else if tone === "ok"}
              ✓
            {:else if tone === "skipped"}
              ○
            {:else}
              ✕
            {/if}
          </span>
          <span class="step-row__label">
            {step.label}
            <span class="step-checklist__sr-only">, {toneWord(tone)}</span>
          </span>
        </span>
        {#if tone === "fail" && step.error}
          <details class="step-row__details">
            <summary>Why it failed</summary>
            <p class="step-row__error">{step.error}</p>
          </details>
        {:else if tone === "skipped" && step.error}
          <details class="step-row__details step-row__details--skipped">
            <summary>Why it was skipped</summary>
            <p class="step-row__error">{step.error}</p>
          </details>
        {/if}
      </li>
    {/each}
  </ul>
</div>

<style>
  .step-checklist {
    display: flex;
    flex-direction: column;
  }

  .step-checklist__sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  .step-checklist__list {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .step-row {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .step-row__line {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
  }

  .step-row__icon {
    display: inline-flex;
    flex-shrink: 0;
    align-items: center;
    justify-content: center;
    width: 1.5rem;
    height: 1.5rem;
    border-radius: 50%;
    font-size: 0.8rem;
    font-weight: 700;
  }

  .step-row__icon--ok {
    background: color-mix(in srgb, var(--ok) 15%, transparent);
    color: var(--ok);
  }

  .step-row__icon--fail {
    background: color-mix(in srgb, var(--fail) 15%, transparent);
    color: var(--fail);
  }

  .step-row__icon--skipped {
    background: color-mix(in srgb, var(--muted) 15%, transparent);
    color: var(--muted);
  }

  .step-row__dot {
    width: 0.55rem;
    height: 0.55rem;
    border-radius: 50%;
    background: var(--pulse);
  }

  .step-row__label {
    color: var(--ink);
    font-size: 0.95rem;
  }

  .step-row__details {
    margin-left: calc(1.5rem + var(--space-sm));
    color: var(--muted);
    font-size: 0.85rem;
  }

  .step-row__details summary {
    color: var(--fail);
    cursor: pointer;
  }

  .step-row__details--skipped summary {
    color: var(--muted);
  }

  .step-row__error {
    margin: var(--space-2xs) 0 0;
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    font-size: 0.8rem;
    white-space: pre-wrap;
  }
</style>
