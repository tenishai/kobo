<script lang="ts">
  // Static orientation page. No fetching, no state: the room's vocabulary
  // and the shortest possible path to a first agent.
</script>

<article class="getting-started">
  <h1>Getting started</h1>

  <section>
    <h2>What lives here</h2>
    <p>
      An agent is a coworker you assemble: it gets a name, a persona, and a
      place to work. Once it is ready, you talk to it here or in Slack, and
      it remembers what you teach it.
    </p>
  </section>

  <section>
    <h2>Creating one</h2>
    <p>
      Press Create agent and answer one question: what should it do for you?
      The room drafts its persona, the agent picks its own name from five
      choices, and you hand it skills to draw on. The checklist shows every
      step as it comes alive.
    </p>
  </section>

  <section>
    <h2>The vocabulary</h2>
    <ul>
      <li><strong>Memory learns.</strong> Files you teach it (md, txt) are read once and remembered.</li>
      <li><strong>Tooling governs.</strong> Rules it always follows, checked on every reply.</li>
      <li><strong>Skills inform.</strong> Guidance it draws on when the work calls for it.</li>
    </ul>
  </section>

  <section>
    <h2>Slack</h2>
    <p>
      Agents can join your Slack. Ask an operator and the room wires it: the
      agent appears as an app you can message directly.
    </p>
  </section>

  <section>
    <h2>Operators</h2>
    <p>
      A few things are operator-only: wiring Slack and destroying an agent.
      If a button is missing for you, that is why.
    </p>
  </section>
</article>

<style>
  .getting-started {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
    max-width: 40rem;
  }

  .getting-started h1 {
    margin: 0;
    color: var(--ink);
  }

  .getting-started h2 {
    margin: 0 0 var(--space-xs);
    color: var(--ink);
    font-size: 1rem;
  }

  .getting-started p,
  .getting-started ul {
    margin: 0;
    color: var(--muted);
    line-height: 1.6;
  }

  .getting-started ul {
    padding-left: 1.2rem;
  }
</style>
