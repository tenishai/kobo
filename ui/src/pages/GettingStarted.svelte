<script lang="ts">
  // Static orientation page. No fetching, no state (just the one plain
  // import below, for the dashboard link): the room's vocabulary and the
  // shortest possible path to a first agent, told as the five-step journey a
  // first-time visitor actually takes, in the order they actually take it
  // (create, meet it, wire it into Slack, bring in teammates, grow it)
  // rather than a features list. Plain language throughout: no repos,
  // images, pods, or CI, the same house rule the rest of this room's own
  // copy follows.
  //
  // Rewritten for the UI-simplification wave: the old five sections (make
  // an agent, talk to it, give it colleagues, put it in Slack, who can do
  // what) described a room that no longer exists -- the build checklist has
  // moved to the top of the agent's page, the create flow no longer asks
  // about skills, and Slack's channel/DM answering rule and the nightly
  // learnings approval gate are both new enough that the old copy never
  // mentioned them. This version walks the five steps in the order a new
  // user actually lives them, ending with the grow-it sections as the
  // "later" step they actually are.
  //
  // Page-shell wave (2026-09-23): the row is Chat/Slack/Access/Settings and
  // the grow-it sections are behind a More menu at the end of it, so the
  // copy below says More rather than Advanced.
  import { formatHash } from "../lib/router.svelte";
</script>

<article class="getting-started">
  <h1>Getting started</h1>

  <section>
    <h2>1. Create your agent</h2>
    <p>
      On the <a href={formatHash({ page: "dashboard" })}>dashboard</a>, press
      Create agent. Pick a persona from the catalog, or describe the job
      yourself and build one from scratch. Accept one of the suggested names,
      or write your own, and choose the group that will see it. A checklist
      appears at the top of the agent's page while it builds, and it
      disappears on its own once every step turns green.
    </p>
  </section>

  <section>
    <h2>2. Say hello</h2>
    <p>
      The Chat tab is where you meet your agent, so open it and say hello.
      Whenever it says something worth keeping, or you tell it something it
      should remember, press Teach this and the fact joins its memory for
      good.
    </p>
  </section>

  <section>
    <h2>3. Put it in Slack</h2>
    <p>
      The Slack tab walks you through the whole setup: creating the app from
      a ready-made manifest, pasting the two tokens it gives you, and
      inviting the bot to a channel. Slack hands you that channel's ID when
      you invite it, so paste that back into the Slack tab too, and the
      agent is connected there. In a channel it only answers when you
      @-mention it, but a direct message always reaches it.
    </p>
  </section>

  <section>
    <h2>4. Bring in teammates</h2>
    <p>
      Anyone already in the same group can see and chat with your agent, with
      no extra setup needed. The Access tab lets you grant an individual person
      outside the group by email, and colleague requests connect your agent
      to agents in other groups so the two can consult each other.
    </p>
  </section>

  <section>
    <h2>5. Grow it (More)</h2>
    <p>
      Once the basics are working, the More menu at the end of the agent's tab
      row has the rest. Learnings shows what your agent proposes from its own
      conversations each night, and nothing is kept until you approve it.
      Memory holds the notes you teach it and any files you upload for it to
      learn from. Skills lets you add abilities from the catalog, including
      ones recommended for this agent. Tooling shows the lanes every agent
      starts with and any extras you add on top.
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

  .getting-started a {
    color: var(--accent);
  }
</style>
