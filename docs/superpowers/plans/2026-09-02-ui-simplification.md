# UI Simplification (Wave 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire the Overview tab into an always-visible identity header, split the remaining tabs into primary (Chat, Slack, Access) and a collapsible Advanced group (Learnings, Memory, Skills, Tooling), remove the skills station from the create modal (its recommended-strip + combobox moves into the Skills tab), and rewrite Getting Started as a linear walkthrough.

**Architecture:** All kobo UI (Svelte 5 runes + @tanstack/svelte-query v6), no API changes. The tab model in `tabs.ts` is restructured first (pure, unit-tested), then `AgentDetail.svelte` consumes it; the create modal and skills panel swap ownership of the catalog-pick control; Getting Started is a prose rewrite.

**Tech Stack:** Svelte 5, @tanstack/svelte-query v6, vitest + @testing-library/svelte.

**Spec:** `docs/superpowers/specs/2026-09-01-learnings-ui-vision-design.md` (Wave 2 section).

## Global Constraints

- The org/vendor name (acme) NEVER appears in source, comments, or string literals.
- NO EM DASHES in `.svelte` files (`ui/src/lib/copy.test.ts` fails the suite on them); avoid them in `.ts` too (use `--` in comments).
- All UI commands run from `ui/`: tests `npx vitest run <file>` (full suite `npx vitest run`), type check `npm run check`.
- kobo work happens on branch `feat/ui-simplification` in `~/Documents/GitHub/kobo`, branched from current `main`.
- Tab model exacts: primary order `chat, slack, access` (access conditional, appended last, same probe as today); advanced order `learnings, memory, skills, tooling`; the `"overview"` TabId is retired everywhere.
- The Advanced group defaults expanded when `data.can_decide_learnings` is true, collapsed otherwise; it force-expands whenever `activeTab` is an advanced tab; when collapsed with `pending_learnings > 0`, the pending badge renders on the Advanced toggle itself.
- Provisioning story: `StepChecklist` renders between header and tabs only while `data.status !== "ready" && !isFrozen(data.status)`; `BuildLogCard` (already a collapsed toggle) renders there always; the failed-banner + retry stays with the checklist block.
- `FirstReply` moves into the Chat tabpanel (above `ChatPanel`, still gated on `data.first_reply`).
- `DangerZone` renders below the tabpanels, still gated on `data.destroy_visible`.
- Ruled deviations from the spec's sketch (controller decisions, flag any conflict rather than silently resolving): `defaultTab(status)` returns `"chat"` for ready and `null` otherwise (the spec's `"slack"` would land on a disabled tab -- pre-ready, no tab is selected and the header owns the story); Getting Started uses prose + a link to the dashboard, no per-tab deep links (the router has no tab segment; adding one is out of scope).

---

## File map

| File | Change |
|---|---|
| `ui/src/lib/tabs.ts` | `primaryTabs`/`advancedTabs` replace `visibleTabs`; TabId drops `"overview"`; `defaultTab` returns `TabId \| null` |
| `ui/src/lib/tabs.test.ts` | full rewrite of pins |
| `ui/src/pages/AgentDetail.svelte` | header absorbs identity fields; checklist block above tabs; Advanced group tab bar; FirstReply -> chat; DangerZone -> bottom; effect simplification |
| `ui/src/pages/AgentDetail.test.ts` | rewrite the `panel-overview`-pinned tests |
| `ui/src/components/CreateModal.svelte` | skills station removed (script 349-436, markup 920-1052, skills-only styles) |
| `ui/src/components/CreateModal.test.ts` | fixture + payload assertions updated |
| `ui/src/components/SkillsPanel.svelte` | catalog section gains recommended strip + combobox (immediate-add) |
| `ui/src/components/SkillsPanel.test.ts` | new strip/combobox tests |
| `ui/src/components/ToolingPanel.svelte` | empty-state explainer paragraph |
| `ui/src/pages/GettingStarted.svelte` | full rewrite, five steps in living order |

---

### Task 1: Tab model restructure (tabs.ts)

**Files:**
- Modify: `ui/src/lib/tabs.ts`
- Test: `ui/src/lib/tabs.test.ts` (full rewrite)

**Interfaces:**
- Consumes: nothing.
- Produces: `type TabId = "chat" | "learnings" | "memory" | "skills" | "tooling" | "slack" | "access"`; `TAB_LABELS: Record<TabId, string>`; `primaryTabs(showAccess?: boolean): TabId[]`; `advancedTabs(): TabId[]`; `ADVANCED_TABS: readonly TabId[]` (the same list as a constant for membership checks); `defaultTab(status: string): TabId | null`. Task 2 consumes all of these; `visibleTabs` is deleted.

- [ ] **Step 1: Create the branch**

```bash
git -C ~/Documents/GitHub/kobo checkout main
git -C ~/Documents/GitHub/kobo pull --ff-only
git -C ~/Documents/GitHub/kobo checkout -b feat/ui-simplification
```

- [ ] **Step 2: Rewrite the failing tests**

Replace `ui/src/lib/tabs.test.ts` entirely:

```ts
import { describe, expect, it } from "vitest";

import {
  ADVANCED_TABS,
  TAB_LABELS,
  advancedTabs,
  defaultTab,
  primaryTabs,
} from "./tabs";

describe("primaryTabs", () => {
  it("is chat and slack with no argument", () => {
    expect(primaryTabs()).toEqual(["chat", "slack"]);
  });

  it("appends access last when the invites probe succeeded", () => {
    expect(primaryTabs(true)).toEqual(["chat", "slack", "access"]);
  });
});

describe("advancedTabs", () => {
  it("is the four grow-it sections in order", () => {
    expect(advancedTabs()).toEqual(["learnings", "memory", "skills", "tooling"]);
    expect(ADVANCED_TABS).toEqual(advancedTabs());
  });
});

describe("TAB_LABELS", () => {
  it("labels every tab and has no overview entry", () => {
    expect(TAB_LABELS).toEqual({
      chat: "Chat",
      learnings: "Learnings",
      memory: "Memory",
      skills: "Skills",
      tooling: "Tooling",
      slack: "Slack",
      access: "Access",
    });
  });
});

describe("defaultTab", () => {
  it("is chat when ready", () => {
    expect(defaultTab("ready")).toBe("chat");
  });

  it.each(["pending", "provisioning", "failed", "destroying", "destroy_failed"])(
    "is null (no tab selected, the header owns the story) for %s",
    (status) => {
      expect(defaultTab(status)).toBeNull();
    },
  );
});
```

- [ ] **Step 3: Run to verify failure**

Run: `cd ~/Documents/GitHub/kobo/ui && npx vitest run src/lib/tabs.test.ts`
Expected: FAIL (`primaryTabs` not exported)

- [ ] **Step 4: Rewrite tabs.ts**

Replace the whole file body (keep the file's doc-comment voice):

```ts
// The agent detail tab model. Pure so the visibility and default-selection
// rules are unit-tested; the component only maps over what these return.
export type TabId =
  | "chat"
  | "learnings"
  | "memory"
  | "skills"
  | "tooling"
  | "slack"
  | "access";

export const TAB_LABELS: Record<TabId, string> = {
  chat: "Chat",
  learnings: "Learnings",
  memory: "Memory",
  skills: "Skills",
  tooling: "Tooling",
  slack: "Slack",
  access: "Access",
};

/**
 * The always-visible tabs, in display order (UI-simplification wave,
 * 2026-09-02: the old flat seven-tab row split into primary + Advanced,
 * and the "overview" tab retired -- its content is the page header now).
 *
 * Access stays the one conditional tab, and deliberately NOT on anything
 * the client can be trusted to assert (never `whoami.operator`) --
 * `showAccess` is the caller's own probe of whether
 * `GET /api/agents/{name}/invites` answered 200 for them. Appended last,
 * same as before the split.
 */
export function primaryTabs(showAccess = false): TabId[] {
  const base: TabId[] = ["chat", "slack"];
  return showAccess ? [...base, "access"] : base;
}

/** The Advanced group: the grow-it sections most visitors never need on
 * day one. Collapsed or expanded by the component (owner-signal +
 * active-tab membership), never reordered. */
export const ADVANCED_TABS = ["learnings", "memory", "skills", "tooling"] as const;

export function advancedTabs(): TabId[] {
  return [...ADVANCED_TABS];
}

/**
 * Which tab a fresh mount should land on: Chat when the agent is ready to
 * talk to (the whole point of the page at that point), and NO tab at all
 * for every other status -- pre-ready the header carries the checklist and
 * every tab is disabled, so selecting one would light up an empty panel.
 */
export function defaultTab(status: string): TabId | null {
  return status === "ready" ? "chat" : null;
}
```

- [ ] **Step 5: Run to verify pass**

Run: `cd ~/Documents/GitHub/kobo/ui && npx vitest run src/lib/tabs.test.ts`
Expected: PASS (AgentDetail.svelte still imports `visibleTabs`, so `npm run check` will fail until Task 2 -- that is expected; do NOT run the full suite yet)

- [ ] **Step 6: Commit**

```bash
git -C ~/Documents/GitHub/kobo add ui/src/lib/tabs.ts ui/src/lib/tabs.test.ts
git -C ~/Documents/GitHub/kobo commit -m "feat(ui): tab model splits primary from advanced, overview retired"
```

---

### Task 2: AgentDetail restructure

**Files:**
- Modify: `ui/src/pages/AgentDetail.svelte`
- Test: `ui/src/pages/AgentDetail.test.ts`

**Interfaces:**
- Consumes: Task 1's `primaryTabs`, `advancedTabs`, `ADVANCED_TABS`, `defaultTab`, `TAB_LABELS`, `TabId`.
- Produces: the restructured page. Nothing downstream consumes it.

Reference anchors (current line numbers, drift expected -- locate by content):
header block 280-304; overview tabpanel branch 361-491 (composition card 366-463, failed banner 465-479, StepChecklist 481, BuildLogCard 483, FirstReply 485-487, DangerZone 489-491); tab bar 316-350 (badge 344-347); tabpanel loop 352-523; activeTab effect 122-135.

- [ ] **Step 1: Rewrite the failing tests**

In `ui/src/pages/AgentDetail.test.ts`, rewrite every test that references `panel-overview` or the `"Overview"` tab name. The new pins (adapt fixture/harness construction to the file's existing style; these are the assertions):

```ts
it("defaults to Chat when ready and shows no checklist", async () => {
  // READY_AGENT fixture
  const chatTab = await screen.findByRole("tab", { name: "Chat" });
  expect(chatTab.getAttribute("aria-selected")).toBe("true");
  expect(screen.queryByText("Clone the repo")).toBeNull(); // checklist hidden when ready
});

it("selects no tab while provisioning and shows the checklist above the tabs", async () => {
  // fixture with status: "provisioning"
  const chatTab = await screen.findByRole("tab", { name: "Chat" });
  expect(chatTab.getAttribute("aria-selected")).toBe("false");
  expect(chatTab.hasAttribute("disabled")).toBe(true);
  expect(screen.getByText("Clone the repo")).not.toBeNull(); // StepChecklist step label from the fixture
  expect(screen.getByRole("button", { name: /build log/i })).not.toBeNull(); // BuildLogCard toggle
});

it("keeps identity fields in the always-visible header", async () => {
  // READY_AGENT: header carries persona line, owner, capability -- none live in a tabpanel
  await screen.findByRole("tab", { name: "Chat" });
  const header = document.querySelector(".agent-detail__header");
  expect(header?.textContent).toContain(READY_AGENT.capability);
  expect(header?.textContent).toContain(`Made by ${READY_AGENT.created_by}`);
});

it("renders first words inside the Chat tabpanel", async () => {
  await screen.findByRole("tab", { name: "Chat" });
  const chatPanel = document.getElementById("panel-chat");
  expect(chatPanel?.textContent).toContain("Hello there."); // READY_AGENT.first_reply
});

it("collapses the Advanced group for non-deciders and expands it on toggle", async () => {
  // READY_AGENT with can_decide_learnings: false
  await screen.findByRole("tab", { name: "Chat" });
  expect(screen.queryByRole("tab", { name: "Memory" })).toBeNull();
  await fireEvent.click(screen.getByRole("button", { name: /advanced/i }));
  expect(screen.getByRole("tab", { name: "Memory" })).not.toBeNull();
});

it("expands the Advanced group by default for deciders", async () => {
  // fixture with can_decide_learnings: true
  await screen.findByRole("tab", { name: "Chat" });
  expect(screen.getByRole("tab", { name: "Learnings" })).not.toBeNull();
});

it("shows the pending badge on the Advanced toggle while collapsed", async () => {
  // fixture with can_decide_learnings: false, pending_learnings: 3
  await screen.findByRole("tab", { name: "Chat" });
  const toggle = screen.getByRole("button", { name: /advanced/i });
  expect(toggle.textContent).toContain("3");
});

it("renders the danger zone below the tabs while destroying", async () => {
  // fixture with status: "destroying", destroy_visible: true
  await screen.findByRole("tab", { name: "Chat" });
  expect(screen.getByText("Danger zone")).not.toBeNull();
});
```

Keep every existing test that does not touch overview/tab-default mechanics (frozen notices, avatar, RequestAccessCard, learnings badge-on-tab) -- update those minimally (the learnings badge test now needs the Advanced group expanded first when can_decide_learnings is false, or a decider fixture).

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/Documents/GitHub/kobo/ui && npx vitest run src/pages/AgentDetail.test.ts`
Expected: FAIL (new pins don't match the current structure)

- [ ] **Step 3: Restructure the component**

All in `ui/src/pages/AgentDetail.svelte`:

1. **Imports:** replace `visibleTabs` with `primaryTabs, advancedTabs, ADVANCED_TABS`; keep `defaultTab`, `TAB_LABELS`, `TabId`.

2. **State/effect:** replace the activeTab effect (current 122-135) with:

```ts
let activeTab = $state<TabId | null>(null);
let advancedOpen = $state(false);
let seededAdvanced = $state(false);

$effect(() => {
  const data = agent.data;
  if (!data) {
    return;
  }
  if (activeTab === null) {
    activeTab = defaultTab(data.status);
  }
  // Deciders (owner/operator/grantee, the server-computed signal) start
  // with the Advanced group open; everyone else starts collapsed. Seeded
  // once so a later toggle is never fought by this effect.
  if (!seededAdvanced) {
    advancedOpen = data.can_decide_learnings;
    seededAdvanced = true;
  }
});

// An advanced tab can become active (badge click, future deep link) --
// the group must never hide the selected tab.
$effect(() => {
  if (activeTab !== null && (ADVANCED_TABS as readonly string[]).includes(activeTab)) {
    advancedOpen = true;
  }
});
```

(The old `wasFrozen` force-to-overview block is deleted along with `wasFrozen` itself; the frozen notice and DangerZone carry the destroy story now.)

3. **Header block** (current 280-304): extend `.agent-detail__heading` so after the existing `.agent-detail__meta` row it also renders (move the derivations `personaMatch`/`personaLine` up out of the old overview branch):

```svelte
        <p class="agent-detail__persona">
          {personaLine}
          {#if data.team && data.team !== "shared"}
            <span class="agent-detail__chip">{data.team}</span>
          {/if}
        </p>
        {#if data.purpose_tags.length > 0}
          <p class="agent-detail__tags">
            {#each data.purpose_tags as tag (tag)}
              <span class="agent-detail__chip">{tag}</span>
            {/each}
          </p>
        {/if}
        <p class="agent-detail__capability">
          {data.capability ?? "Not written yet. It arrives after first contact."}
        </p>
        {#if data.created_by}
          <p class="agent-detail__owner">Made by {data.created_by}</p>
        {/if}
```

The avatar action block (generate/upload/download, current 404-452) moves under the avatar inside the header; `MetadataDetails` (current 454-462) renders directly below the header block, unchanged props. The skill chips (377-383) and the duplicate Model line (384-387) are DELETED -- the Skills tab owns the skill list, and the header meta already shows the model.

4. **Provisioning block** (new, between `RequestAccessCard` and the tabs):

```svelte
    {#if data.status === "failed"}
      <!-- failed banner + retry: moved verbatim from the old overview branch -->
    {/if}
    {#if data.status !== "ready" && !isFrozen(data.status)}
      <StepChecklist steps={data.steps} />
    {/if}
    <BuildLogCard agentName={data.name} />
```

5. **Tab bar** (replaces current 316-350):

```svelte
    {@const primary = primaryTabs(invites.isSuccess)}
    {@const advanced = advancedTabs()}
    {@const tabs = advancedOpen ? [...primary, ...advanced] : primary}
    <div class="detail-tabs">
      <div class="detail-tabs__bar" role="tablist" aria-label="Agent sections">
        {#each primary as tab (tab)}
          <!-- existing tab-button markup, tabDisabled now:
               data.status !== "ready" && !isFrozen(data.status) -->
        {/each}
        <button
          type="button"
          class="detail-tabs__advanced-toggle"
          aria-expanded={advancedOpen}
          onclick={() => (advancedOpen = !advancedOpen)}
        >
          Advanced
          {#if !advancedOpen && data.pending_learnings > 0}
            <span class="detail-tabs__badge">{data.pending_learnings}</span>
          {/if}
        </button>
        {#if advancedOpen}
          {#each advanced as tab (tab)}
            <!-- same tab-button markup; the learnings badge stays on its
                 own tab exactly as today -->
          {/each}
        {/if}
      </div>
      <!-- tabpanel loop now iterates [...primary, ...advanced] ALWAYS (all
           panels stay mounted regardless of the group's collapse -- the
           hidden={} discipline is unchanged); the overview branch is
           deleted; the chat branch gains FirstReply:
             {#if data.first_reply}<FirstReply reply={data.first_reply} />{/if}
           above ChatPanel. -->
    </div>
    {#if data.destroy_visible}
      <DangerZone agentName={data.name} status={data.status} error={data.error} />
    {/if}
```

IMPORTANT: the tabpanel `{#each}` iterates `[...primary, ...advanced]` unconditionally (NOT the collapsed `tabs` const) so panels never unmount when the group collapses; only the BUTTONS collapse.

6. **Styles:** add `.agent-detail__persona`, `.agent-detail__tags`, `.agent-detail__capability`, `.agent-detail__owner`, `.agent-detail__chip` (reuse the old `.composition__*` chip styling values), `.detail-tabs__advanced-toggle` (visually a quieter sibling of `.detail-tabs__tab` with a divider margin before it). Delete the now-orphaned `.composition*` rules for elements that moved or died; keep any still referenced.

- [ ] **Step 4: Run the page tests, then the whole suite + check**

Run: `cd ~/Documents/GitHub/kobo/ui && npx vitest run src/pages/AgentDetail.test.ts && npx vitest run && npm run check`
Expected: all pass (copy.test.ts included -- no em dashes in the new copy)

- [ ] **Step 5: Commit**

```bash
git -C ~/Documents/GitHub/kobo add ui/src/pages/AgentDetail.svelte ui/src/pages/AgentDetail.test.ts
git -C ~/Documents/GitHub/kobo commit -m "feat(ui): identity header + advanced group, overview tab retired"
```

---

### Task 3: Create modal loses the skills station

**Files:**
- Modify: `ui/src/components/CreateModal.svelte` (script 349-436, markup 920-1052, styles `.skill-combobox`/`.skill-dropdown` 1853-1871; anchors drift -- locate by content)
- Test: `ui/src/components/CreateModal.test.ts`

**Interfaces:**
- Consumes: nothing new.
- Produces: the create POST body always carries `skills: []`. Task 4 does NOT depend on this task's code (the panel gets fresh markup), only on the product decision.

- [ ] **Step 1: Write the failing test**

Add to `CreateModal.test.ts` (mirror the existing "submits with no team key" test's setup at lines 64-124):

```ts
it("submits with an empty skills list and offers no skills station", async () => {
  // same stubs/fill-in flow as the no-team-key test, then:
  expect(screen.queryByText("Skills")).toBeNull(); // station heading gone
  // after submit:
  expect(sentBody.skills).toEqual([]);
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/Documents/GitHub/kobo/ui && npx vitest run src/components/CreateModal.test.ts`
Expected: FAIL (the Skills station heading renders)

- [ ] **Step 3: Remove the station**

Delete from `CreateModal.svelte`:
- Script: the skills search/recommended/selection block (current 349-436: `skillQ`, `skillTeamFilter`, `skillSearchParams`, `skills` query, `recommendedSkills` query, `commitSkillSearch`, `skillDropdownOpen`, `closeSkillDropdownOnFocusout`, the `$effect` driving the debounce, `SelectedSkill`, `selectedSkills`, `skillKey`, `toggleSkill`, `removeSkill`) plus `SKILLS_SHOWN_CAP` (line 407) if nothing else references it.
- Markup: the whole Skills station div (current 920-1052).
- `handleSubmit`: the `skills:` mapping (658-669) becomes `skills: [],` with a one-line comment: skills are added post-create in the Skills tab (UI-simplification wave).
- Styles: `.skill-combobox`/`.skill-dropdown` (1853-1871). Leave `.skill-list`/`.skill-item*`/`.chip*`/`.catalog-badge*`/`.catalog-search__row` untouched if the Persona station still uses them (check each with a grep before deleting; delete only the truly orphaned).
- Imports now unused: remove `recommendedSkillsQuery`, `skillsSearchQuery` (keep `teamsQuery` ONLY if the persona station still uses it -- grep first), and `CatalogEntry` if orphaned.
- Update the station-count copy: the bench doc comment (lines 1-74) and any "step N of M" indicators the modal renders.
- Update `stubCreateModalCatalogs` in the test file: drop the `/api/skills/search` stub (the modal no longer fetches it); if any existing test still passes it, adjust.

- [ ] **Step 4: Run the modal tests, then the whole suite + check**

Run: `cd ~/Documents/GitHub/kobo/ui && npx vitest run src/components/CreateModal.test.ts && npx vitest run && npm run check`
Expected: all pass, no unused-import warnings from check

- [ ] **Step 5: Commit**

```bash
git -C ~/Documents/GitHub/kobo add ui/src/components/CreateModal.svelte ui/src/components/CreateModal.test.ts
git -C ~/Documents/GitHub/kobo commit -m "feat(ui): create modal sheds the skills station -- skills live post-create"
```

---

### Task 4: Skills tab gains the recommended strip + combobox

**Files:**
- Modify: `ui/src/components/SkillsPanel.svelte`, `ui/src/pages/AgentDetail.svelte` (two new props at the SkillsPanel call site)
- Test: `ui/src/components/SkillsPanel.test.ts` (READ IT FIRST -- this plan was written without its contents; mirror its harness exactly)

**Interfaces:**
- Consumes: existing `recommendedSkillsQuery(who, what)`, `skillsSearchQuery(q, team)`, `teamsQuery()`, `addCatalogSkillMutation(queryClient, name)`, `debounced`.
- Produces: `SkillsPanel` props widen to `{ agentName: string; frozen?: boolean; recommendWho?: string; recommendWhat?: string }`. AgentDetail passes `recommendWho={data.capability ?? ""}` and `recommendWhat={data.purpose_tags.join(" ")}`.

- [ ] **Step 1: Read the panel and its tests**

Read `ui/src/components/SkillsPanel.svelte` (the current "Add from catalog" toggled section, lines ~432-564, and `handleAddFromCatalog` ~186-205) and ALL of `SkillsPanel.test.ts`. The new control REPLACES the existing inline search section inside the same toggle; adds stay immediate-per-click (the panel's existing semantics), never staged chips.

- [ ] **Step 2: Write the failing tests**

Add to `SkillsPanel.test.ts` (constructions mirroring the file's harness):

```ts
it("shows recommended skills for the agent and adds one on click", async () => {
  // stub /api/catalog/skills/recommended -> one entry {name: "cost-report", team: "sre", ...}
  // render with recommendWho="watches billing", recommendWhat="cost billing"
  // open the catalog section, click the recommended row's Add button,
  // assert the add-catalog POST fired with {skill: "cost-report", team: "sre"}
  // (match the exact body key names the existing add test asserts)
});

it("searches the catalog from the combobox and adds a result", async () => {
  // type into the search input, advance the debounce (vi.useFakeTimers or
  // the file's existing pattern), assert /api/skills/search was fetched
  // with the typed q, click a result row's Add, assert the POST
});

it("omits the recommended strip when the agent has no capability or tags", async () => {
  // recommendWho="" recommendWhat="" -> recommendedSkillsQuery disabled;
  // open the catalog section, assert no "Recommended" heading renders
});
```

- [ ] **Step 3: Run to verify failure**

Run: `cd ~/Documents/GitHub/kobo/ui && npx vitest run src/components/SkillsPanel.test.ts`
Expected: FAIL (no recommended strip, no combobox)

- [ ] **Step 4: Implement**

In `SkillsPanel.svelte`:
1. Widen props: `let { agentName, frozen = false, recommendWho = "", recommendWhat = "" } = $props();` (typed).
2. Inside the existing catalog section (keep the "Add from catalog"/"Hide catalog" toggle), render FIRST a recommended strip:

```svelte
{#if recommended.data && recommended.data.length > 0}
  <div class="skills-panel__recommended">
    <h4 class="skills-panel__subheading">Recommended for this agent</h4>
    {#each recommended.data as entry (entry.team + "/" + entry.name)}
      <!-- row: name, team badge, description one-liner, Add button ->
           handleAddFromCatalog(entry), disabled={frozen || alreadyAdded(entry)} -->
    {/each}
  </div>
{/if}
```

backed by `const recommended = createQuery(() => (recommendedSkillsQuery(recommendWho, recommendWhat)));` (thunk + svelte-ignore comment, the house idiom). `alreadyAdded(entry)` checks the current skills list by name.

3. Below it, replace the section's existing search UI with the combobox shape ported from the old CreateModal station (state: `skillQ`, `skillTeamFilter`, debounced `skillSearchParams`, `skillsSearchQuery` createQuery, `skillDropdownOpen`, focusout close handler; markup: search input + team `<select>` from `teamsQuery` + dropdown listbox). Each dropdown row has an immediate Add button calling the existing `handleAddFromCatalog(entry)` -- NO staged selection state (`selectedSkills`/`toggleSkill` do not come over).
4. Styles: port `.skill-combobox`/`.skill-dropdown` rules (from the deleted CreateModal block) into this panel's scoped style block, renamed under `.skills-panel__*` if that matches the file's BEM root.
5. In `AgentDetail.svelte`, the skills tabpanel branch becomes:

```svelte
<SkillsPanel
  agentName={data.name}
  frozen={isFrozen(data.status)}
  recommendWho={data.capability ?? ""}
  recommendWhat={data.purpose_tags.join(" ")}
/>
```

- [ ] **Step 5: Run the panel tests, then the whole suite + check**

Run: `cd ~/Documents/GitHub/kobo/ui && npx vitest run src/components/SkillsPanel.test.ts && npx vitest run && npm run check`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git -C ~/Documents/GitHub/kobo add ui/src/components/SkillsPanel.svelte ui/src/pages/AgentDetail.svelte ui/src/components/SkillsPanel.test.ts
git -C ~/Documents/GitHub/kobo commit -m "feat(ui): skills tab gains recommended strip + catalog combobox"
```

---

### Task 5: Advanced empty states (tooling explainer)

**Files:**
- Modify: `ui/src/components/ToolingPanel.svelte`
- Test: `ui/src/components/ToolingPanel.test.ts` (read first; add one test)

**Interfaces:** none new.

The spec's ruling: all three auto-assigned tool lanes stay; the Tooling tab's empty state explains what each is for. Memory and Learnings already carry explainer paragraphs (MemoryPanel's how-learning-works text, LearningsPanel's nightly-review text); Skills' heading copy ("What your agent can do, on top of what it knows.") already serves. Only Tooling lacks one.

- [ ] **Step 1: Write the failing test**

```ts
it("explains the built-in tool lanes near the top of the panel", () => {
  // render with the file's harness
  expect(
    screen.getByText(
      /Every agent ships with colleague tools.*list and consult the room.*share a fact with quorum review.*repo tools stay dormant until an operator approves a repository\./s,
    ),
  ).not.toBeNull();
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/Documents/GitHub/kobo/ui && npx vitest run src/components/ToolingPanel.test.ts`
Expected: FAIL

- [ ] **Step 3: Add the explainer**

Near the top of `ToolingPanel.svelte`'s rendered section (mirroring MemoryPanel's explainer placement and class conventions):

```svelte
<p class="tooling-panel__explainer">
  Every agent ships with colleague tools: it can list and consult the room,
  and share a fact with quorum review. Its repo tools stay dormant until an
  operator approves a repository on the Access tab. Tools added here are
  extras on top of that baseline.
</p>
```

(Adjust the class root to the file's actual BEM root. NO em dashes.)

- [ ] **Step 4: Run the panel tests, then the whole suite**

Run: `cd ~/Documents/GitHub/kobo/ui && npx vitest run src/components/ToolingPanel.test.ts && npx vitest run`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git -C ~/Documents/GitHub/kobo add ui/src/components/ToolingPanel.svelte ui/src/components/ToolingPanel.test.ts
git -C ~/Documents/GitHub/kobo commit -m "feat(ui): tooling tab explains its built-in lanes"
```

---

### Task 6: Getting Started rewrite

**Files:**
- Modify: `ui/src/pages/GettingStarted.svelte` (full rewrite; currently a static five-section page, no imports, no fetching)
- Test: covered by `copy.test.ts` automatically; add one smoke test if a `GettingStarted.test.ts` exists (read the directory first)

**Interfaces:** none. The router has no tab deep-linking (ruled out of scope); the one live link is the dashboard (`#/`), where Create lives.

- [ ] **Step 1: Rewrite the page**

Replace the article body with the five steps in the order a new user actually lives them. Keep the existing `.getting-started` class conventions and heading levels. The content (write it in the room's plain second-person voice, complete sentences, NO em dashes):

1. **"1. Create your agent"** -- the Create button on the dashboard (link `#/`), describe: pick or describe a persona, accept a name, pick the group. Note the build checklist now lives at the top of the agent's page and disappears once everything is green.
2. **"2. Say hello"** -- the Chat tab is where you meet it. Mention "Teach this" on any message worth keeping permanently.
3. **"3. Put it in Slack"** -- the Slack tab walks the whole setup; in channels the agent answers only when @-mentioned, DMs always reach it.
4. **"4. Bring in teammates"** -- same-group members already see it; the Access tab grants individual visibility and colleague requests connect it to other agents.
5. **"5. Grow it (Advanced)"** -- the Advanced group on the agent page: Learnings (each night it proposes memory snippets from its own conversations; nothing is kept until you approve it), Memory (teach notes and knowledge files), Skills (add from the catalog, recommendations included), Tooling (the built-in lanes and extras). One paragraph total per section, one sentence each.

- [ ] **Step 2: Run the copy test + full suite**

Run: `cd ~/Documents/GitHub/kobo/ui && npx vitest run src/lib/copy.test.ts && npx vitest run && npm run check`
Expected: all pass

- [ ] **Step 3: Commit**

```bash
git -C ~/Documents/GitHub/kobo add ui/src/pages/GettingStarted.svelte
git -C ~/Documents/GitHub/kobo commit -m "feat(ui): getting started rewritten as the five-step journey"
```

---

## Self-review notes

- Spec coverage: identity header (Task 2), checklist collapse (Task 2), primary/advanced split with owner-default expansion + toggle badge (Tasks 1-2), create modal skills removal (Task 3), combobox to Skills tab with a recommendation signal adapted to existing agents (Task 4), tooling ruling's empty-state explainer (Task 5), getting-started rewrite (Task 6).
- Ruled deviations (also in Global Constraints): defaultTab null pre-ready instead of "slack"; no tab deep-links in getting-started; FirstReply to Chat tab; DangerZone below the tabpanels. The spec's "create flow becomes name, persona, model, Slack" was stale -- the modal's real stations are Persona/Naming/Skills/Create, and only Skills is removed.
- Known adaptation points (flagged inline): SkillsPanel.test.ts and ToolingPanel.test.ts contents were not read during planning -- both tasks start by reading them; AgentDetail line anchors drift.
