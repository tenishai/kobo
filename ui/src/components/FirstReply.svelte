<script lang="ts">
  // The payoff moment: once an agent has actually spoken, this is where
  // that first reply lands. Rendered by the caller only once `first_reply`
  // is non-null (see AgentDetail) — this component just gives the text room
  // to breathe rather than deciding whether to show up.
  let { reply }: { reply: string } = $props();
</script>

<section class="first-reply" aria-labelledby="first-reply-heading">
  <h2 id="first-reply-heading" class="first-reply__heading">First words</h2>
  <p class="first-reply__text">{reply}</p>
</section>

<style>
  .first-reply {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-xl) var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .first-reply__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .first-reply__text {
    margin: 0;
    max-width: 42rem;
    color: var(--ink);
    font-size: 1.05rem;
    line-height: 1.7;
    white-space: pre-wrap;
  }
</style>
