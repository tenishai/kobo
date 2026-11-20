# Skills, guardrails and steps: structure an agent can see

Status: accepted for build, 2026-11-19. Epic tenishai/kobo#524. First change:
tenishai/daedalus#266. Started from a proposal to let a room add gateway lanes
from its org config; this document takes the lane question (step 5) and the
larger one behind it: where an agent's know-how, rules and order of work live.

## The decision in one paragraph

An agent's structure sits in three places a person can see on its page.
**Skills** say how it does each job; a skill can carry **steps** that run the
same way every time, as one tool call. **Guardrails** hold one plain list of
rules the agent follows on every task, line by line, plus the checks the room
runs in code, plus (last) "ask a person before". **Access** says what it can
reach, and the tools follow the permissions with no list for anyone to manage.
Rules leave the persona and the "guardrail skill"; a persona keeps who the agent
is and what it does. Memory recalls a skill's card, not its body. Lanes come from
the gateway's handler cards, not from tables compiled into the room. Together
these replace most of the orchestrator a team writes for an agent today; the
part they do not replace is waiting for a person in the middle of a task, which
is the last step.

## Why: what is true today

Read from main on 2026-11-18 and 2026-11-19 unless a measurement says otherwise.

- **Recall pastes whole skills.** The boot seeder stores each `SKILL.md` as one
  row and recall re-fetches full rows into the prompt, beside the skills index
  and `skills.load`. Replayed in the room's runtime image on a seven-skill audit
  agent's bundle: 14,000 to 59,000 characters recalled per question, 67 to 98%
  of it skill text, skills in up to three of the five rows (daedalus#266).
- **Rules live in four places.** The persona's `## Rules` list (always loaded,
  one line each since the 2026-11-17 house shape); the "New guardrail skill"
  button, which writes a skill and so loads only when the model asks for it;
  rules written inside skills (the same audit agent's seven skills hold 134
  lines with always, never or must, its persona 15); and checks in Tooling.
  Nobody can see in one place what an agent must always do.
- **Tooling was meant to be an orchestrator.** `tooling.py` says a tooling file
  is "an orchestrator or guardrail"; only the guardrail half shipped:
  `pre_task` and `post_draft` hooks that see text, have two seconds and cannot
  call a tool.
- **What a team writes instead.** A connected access agent's orchestrator is
  7,610 lines. Split roughly by function name: Slack conversation and routing
  ~940 and model sessions and prompts ~1,260 (the harness already carries both
  for every agent); lookups before the model ~570 and actions per request type
  ~2,880 (skills, rules and steps over the room's tools); waiting for an
  approval and resuming ~1,020, request state kept for days ~400 and scheduled
  sweeps ~80 (nothing in the harness today).
- **Skills say what they need, and nobody shows it.** The runtime reads
  `requires:` from a skill's frontmatter and logs a warning when the allow list
  lacks a tool. The room never reads it. An agent with a skill and a grant and
  no tool behind them looks ready until someone asks it.
- **techne has an always-loaded band nobody uses.** `lessons.select_principles`
  renders operator-written charters and grants, then the agent's own
  principles, under a character budget, and says what did not fit. Neither the
  runtime nor the room calls it; recall is one search of five rows.
- **Lane tables name one install's handlers.** `gateway_lane.py` names handlers
  (`gcp`, `okta`, `datadog`, `github-write`, `github-clone`, `jira-write`) that
  the gateway does not ship; an install's handler package does. A lane removed
  from the tables leaves its scope, handler and allow pattern on every agent,
  because what the room withdraws is computed from the same tables (the
  `RETIRED_*` lists are the hand-kept patch). A permission lane replaces its
  vendor's lane instead of adding to it.
- **Nothing can approve in a deployed agent.** The runtime's approvers are
  `DenyAll` and the CLI's, so a gated tool is hidden, never asked about; the
  composer writes no gates for that reason. There are no agent schedules.

## What a person sees

### Skills

Each row says whether the agent can use the skill: **Ready**, or **Needs Okta
(read)** with **Request**. One button requests everything a skill is missing.
A skill with steps says it runs them the same way every time. Tool names appear
only in Activity, where a call happened.

### Guardrails (renamed from Tooling)

```
Guardrails                                  28 rules · room for ~10 more
  + Add a rule

  Every access change goes through the approval flow.       persona
  Never state who has access without checking first.        persona
  Only answer in the team's request channel.                Dana, 11/19
  When searching tickets, use the person's full email.      skill access-audit
  Never paste raw JSON into Slack.                          correction, 11/12

  ▸ Inside its skills (41, load only with that skill)

  Checks the room runs                             + From catalog
    Leak guard   Probe guard   Audit provenance
```

- **One list, plain text, one rule per line.** Add, edit in place, delete.
  Each line names its source: the persona, a skill, a correction, or a person.
  The owner may change any line, including the ones the persona started.
- **A size limit, on purpose.** About 4,000 characters (30 to 40 rules). Past
  it a list stops being readable for a person too, and situational rules belong
  in skills. The section says how much room is left and names a rule that did
  not fit. The editor nudges toward rules that make sense on their own ("When
  searching tickets, use the person's full email", not "Use the full email").
- **Where rules come from, so nobody starts from a blank page.** The persona's
  rules arrive as starter guardrails when the agent is made. A skill added to
  the agent offers its agent-wide always and never lines (Keep, Skip). A
  correction in Learnings asks "Should it always follow this?". The section
  itself asks two questions: what should it always do, and what should it
  never do. One line teaches the difference everywhere it matters: "Always or
  never: a guardrail. How to: a skill."
- **Nothing hidden.** Rules that stay inside a skill (they only hold within its
  procedure) are listed read-only under "Inside its skills", each with **Make
  it a guardrail**.
- **Checks** are today's hooks: the baseline leak guard and probe guard, plus
  the catalog's. Owners add them; they do not write them.

### Personas

A persona is the opening line and **What you do**. The builder's rules field
becomes **Starter guardrails**, copied into each new agent's list. Swapping an
agent's persona leaves its rules alone.

## How it works

### 1. Recall a skill's card, not its body (daedalus#266)

When `tools.allow` admits `skills.load`, the seeder stores a skill as its card:
the id, its one-line summary (`when_to_use`, else `description`, else the first
line of prose) and how to load it. The card mode is part of the skill file's
digest, so a store seeded the old way re-seeds each skill once at its next
boot. A bundle without `skills.load` keeps the whole body. The skills index
falls back to `description` before the name. Measured after: 2,000 to 5,000
characters per question on the same bundle and questions.

### 2. Guardrail rules

- **The record** is one file in the agent's repository, `bundle/guardrails.md`,
  one rule per line. Every change is a commit; the room records who made it.
  The room keeps provenance per line in its own rows and renders the file.
- **The runtime** reads the file and puts it right after the persona as one
  `## Rules` block, cut from the bottom at 4,000 characters with a note that
  says so (tenishai/daedalus#267). A live edit arrives whole on a system task,
  `guardrails`, is kept on the agent's own volume, and applies on the next
  task with no restart; the boot seeder's staleness protocol decides between
  that copy and the bundle after a restart. (Changed 2026-11-19, while
  building: an earlier draft stored rules as techne charters. A file keeps the
  owners' order, works without techne, and never comes back through recall
  beside the block that carries it.)
- **The room** renames Tooling to Guardrails, adds the list editor, moves the
  persona's rules into starter guardrails (`parse_persona_body` already finds
  the section), relabels the builder's field, and retires "New guardrail skill".
- **Migration.** One commit per existing agent moves its persona's `## Rules`
  lines into the file. A persona written before the house shape gets a drafted
  split its owner confirms. A local guardrail skill's lines become rules once
  its owner confirms, and the skill row goes. Catalog skills are not changed:
  other agents and people use them, and many of their rules only hold inside
  the procedure.
- **Connected agents** get the file on their repository like the room's other
  files; following it is their code's job.

### 3. Each skill shows whether the agent can use it

The room reads `requires:` from each skill in the agent's bundle, maps each
tool pattern to its vendor and permission through the lanes, and compares with
the agent's grants. For a skill with steps the tools come from the steps file
itself. "Request" opens the permission requests already filled in.

### 4. Steps in skills

- A skill may carry `skills/<id>/steps.py` with declared inputs and
  `async def run(call, **inputs)`. The runtime offers it as one tool,
  `steps.<id>`, whose schema is the declared inputs; the room adds `steps.*`
  to the allow list when such a skill is added.
- `call(tool, arguments)` goes through the agent's own call path: the allow
  list, then the gates, then the provider. The runtime's audit trace calls the
  provider directly today; that is the pattern this replaces. Every call shows
  in Activity. A run has a time limit and a cap on calls.
- A steps file is code that runs as the agent. The room's check on bundle
  Python is a lint, not a boundary (daedalus#195); the boundary is the
  agent's gateway grant. So steps come from the catalog or a reviewed change,
  never from an owner's text box.
- First: the runtime's audit trace moves into its audit skill as steps. The
  chain is the install's own process and belongs in its catalog.

### 5. Lanes from handler cards

- **Target lanes** (`datadog-mcp`, `github-mcp`, `github-mcp-write`,
  `atlassian`) stay in the room: the room creates those targets.
- **Handler lanes** come from the cards. liminal's capability cards and tool
  entries already accept extra fields and `labels`, so a handler declares its
  vendor and each tool's permission with no schema change. liminal adds one
  admin route listing the loaded cards. The room derives lanes and `TOOLED`
  from them and drops the hand-kept tables.
- **The room remembers what it put on each grant and bundle**, so a lane that
  disappears is still withdrawn, and revoking keeps working.
- **A failed card read keeps the last known lanes.** Read as "no lanes", one
  gateway restart would strip every agent's tools on the next sync.
- A room-config lane block is not needed once this lands.

As built (liminal 0.10.0, `GET /admin/cards`): a card says `labels.vendor`, and
each tool its `permissions` (`["*"]` for any of the vendor's) and `risk`. One
more label covers `github-clone`: `labels.audience: own-code` keeps a handler
to agents that run their team's own code, and the room skips a card with an
audience it does not know. The room keeps one record for every agent rather
than one per grant: every scope, handler and allow pattern a card's lane has
ever named (`gateway_managed`). It withdraws those like the targets' own, so
a handler that leaves the cards is taken off every agent that held it. The
last read lives in `gateway_cards` and is used across the room's own restarts.
A room that has never read the cards manages its targets alone.

### 6. Ask a person before

A guardrail names actions that wait for a person ("changing access"). The
runtime gets an approver that works in a deployed agent (a reaction in the
thread, a named channel, or the room's Review), keeps the held call across a
restart, and resumes when it is decided. The composer writes the gates from
the guardrail. This replaces the approval, request-state and sweep code a
team writes today.

## Order and who merges

| Step | Repositories | Merged by |
|---|---|---|
| 1. Cards in recall | daedalus | daedalus maintainers, then a runtime image and room pin |
| 2. Guardrail rules | daedalus, kobo | daedalus maintainers; kobo |
| 3. Can it use this skill | kobo | kobo |
| 4. Steps in skills | daedalus, kobo, the install's catalog | each repository's owners |
| 5. Lanes from cards | liminal, the install's handler package, kobo | each repository's owners |
| 6. Ask a person before | daedalus, kobo | daedalus maintainers; kobo |

After steps 2 and 4: when a rule keeps failing while it is loaded, the room
offers to make it a check.

## Not in scope

- Agent schedules (sweeps, a daily post).
- An admin view of every agent's rules. Easy once rules are one list; later.

## Contract tests

- Step 2: a rule saved in the room is in the agent's prompt on the next task
  with no restart; a removed rule is gone; a list over budget names what did
  not fit; making an agent copies its persona's starter guardrails, and
  swapping the persona leaves the list alone.
- Step 3: a skill whose `requires:` no grant opens reads Needs, and Request
  opens those permissions; a granted one reads Ready.
- Step 4: a steps run cannot call a tool the allow list refuses; a gated tool
  inside a run waits on its gate; every call appears in Activity.
- Step 5: a handler removed from the cards is withdrawn from every agent that
  held it; a failed card read changes no grant.
- Step 6: a held call survives a restart and runs once after approval; a
  refusal is reported to the person who asked.
