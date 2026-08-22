<script lang="ts">
  // Static orientation page. No fetching, no state: the room's vocabulary
  // and the shortest possible path to a first agent, told as the five-step
  // journey a first-time visitor actually takes (create, talk and teach,
  // connect agents to each other, wire up Slack, understand who can do
  // what) rather than a features list. Plain language throughout: no
  // repos, images, pods, or CI, the same house rule the rest of this
  // room's own copy follows.
</script>

<article class="getting-started">
  <h1>Getting started</h1>

  <section>
    <h2>Make an agent</h2>
    <p>
      Press Create agent. Describe the job in a sentence, or paste or upload
      a full persona document if your team already has one written, and the
      details fill themselves in. Pick a name from the five cards and skills
      from the catalog, then choose a group: your team's agents are visible
      to your teammates, shared agents are visible to everyone, and if your
      group is not listed you can request it right from the menu. Create it,
      then watch the checklist: the hammer in the corner glows while Kobo
      builds.
    </p>
  </section>

  <section>
    <h2>Talk to it, teach it</h2>
    <p>
      Open Chat. When the agent says something worth keeping, or you tell it
      something it should remember, press Teach this and the fact is in its
      memory in seconds. Corrections work too: teach the new fact and the
      old one retires on its own. You can also drop files on the Memory tab
      for one-shot learning.
    </p>
  </section>

  <section>
    <h2>Give it colleagues</h2>
    <p>
      Agents on the same team already know each other. To connect across
      teams, open Access and request a colleague from another team's list;
      their owner accepts, and the two can consult each other from then on.
      Watch any agent's conversations in the terminal view under Chat.
    </p>
  </section>

  <section>
    <h2>Put it in Slack</h2>
    <p>
      For agents you own, the Slack tab walks you through creating the app
      with a ready-made manifest, pasting two tokens, inviting the bot to a
      channel, and saving the channel id. Long answers arrive whole.
    </p>
  </section>

  <section>
    <h2>Who can do what</h2>
    <p>
      You sign in with your company account, and your email shows bottom
      left. You own what you create: only you and the room's admins can
      change your agent, though everyone on your team can see it and chat
      with it. Grants on the Access tab let you add specific people from
      other teams.
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

  .getting-started p {
    margin: 0;
    color: var(--muted);
    line-height: 1.6;
  }
</style>
