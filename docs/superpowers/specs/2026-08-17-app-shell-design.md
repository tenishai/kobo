# App shell — sidebar, tabs, plain punctuation (design)

Approved in brainstorm 2026-08-17. Operator decisions: agent detail keeps
chat pinned with four tabs below; the shell gains a left sidebar with a
logo and a Getting Started page; panels render read-only while an agent
is destroying (restoring the destruction spec's letter, which the first
implementation replaced with unmounting); destroy-repo's fail-honest
behavior is ratified and the destruction spec amended to match.

## Shell structure

`App.svelte` gains a left sidebar:

- Logo mark at top (see Logo), acting as a home link.
- Nav items: **Agents** (the existing dashboard) and **Getting Started**.
- The existing hand-rolled `navigate({page})` state gains one page id,
  `getting-started`. No router library; two pages do not justify one.
- Narrow viewports collapse the sidebar to an icon rail (logo + two
  icons, labels in tooltips/aria-label). Breakpoint follows the app's
  existing responsive convention.

The current top header ("kobo" wordmark line) folds into the sidebar;
the page area keeps its max-width column.

## Logo

Three or four SVG mark candidates, monochrome-first so they work in both
themes, offered to the operator as a pick-one set. Until one is chosen,
a plain wordmark placeholder ships in the sidebar slot so nothing blocks
on the choice. The winner lands in the sidebar and as the favicon.

## Agent detail page

(Amended 2026-08-17 after the operator saw the first cut live: the
pinned-chat layout still read as one long page. Ruling: everything
tabbed, smart default.)

- The header stays: back link, avatar, name, status badge, model,
  source link.
- One tab bar with real visual weight: **Overview, Chat, Memory,
  Skills, Tooling, Slack**. The Slack tab renders only when
  `slack_visible`. Panels keep their internals.
- **Overview** holds the persona/composition card (with the avatar
  action), the step checklist, the failed banner, and First words.
- **Smart default**: on mount, land on Chat when the agent is ready;
  land on Overview in any other status. When status transitions into a
  destroy state while the page is open, select Overview once (the
  reversed checklist is the destruction story); the operator can still
  click away.
- **Danger zone lives at the bottom of the Overview tab** (amended
  2026-08-17, operator ruling: destruction is part of the agent's story,
  and the destroy auto-select already lands on Overview), rendered only
  when `destroy_visible`.

## Read-only while destroying

While the agent's status is `destroying` or `destroy_failed`:

- Panels stay rendered inside their tabs and receive a `frozen` prop.
- Each panel disables its interactive controls (file inputs, buttons,
  text inputs, the chat composer) while leaving content visible, so an
  operator diagnosing a failed destroy keeps the transcript and panel
  state in view.
- The one-line destroying notice renders above the tab bar (single
  aria-live region; the redundant second announcement inside the danger
  zone goes away).
- The Danger zone stays live; it owns Destroy again.

A page-level `inert` overlay was considered and rejected: it risks
breaking scroll inside long transcripts and gives worse screen reader
semantics than explicit disabled controls.

## Plain punctuation sweep

Every em dash in user-facing copy is replaced with plain punctuation
(comma, colon, or a restructured sentence; never a lookalike dash).
Inventory from the destruction arc's final review, by file:

- `ui/src/App.svelte` (header line)
- `ui/src/components/StepChecklist.svelte` (screen-reader text)
- `ui/src/components/PersonaBuilder.svelte` (6)
- `ui/src/components/MemoryPanel.svelte` (6)
- `ui/src/components/SkillsPanel.svelte` (2)
- `ui/src/components/SlackPanel.svelte` (2)
- `ui/src/components/AgentCard.svelte` (1)
- `ui/src/components/CreateModal.svelte` (3)
- `ui/src/pages/AgentDetail.svelte` (1)
- `ui/src/pages/Dashboard.svelte` (1)
- `ui/src/components/ToolingPanel.svelte` (3)

Line numbers drift; the acceptance check is a repo-wide scan of rendered
string literals in `ui/src` showing zero em dashes outside code comments
and test files.

## Getting Started page

A static page in the room's voice, no infrastructure vocabulary:

1. What an agent is here, in two sentences.
2. Creating one: the four stations, three sentences total.
3. The vocabulary: **Memory learns** (md/txt files it reads once),
   **Tooling governs** (rules it always follows), **Skills inform**
   (guidance it draws on).
4. How Slack arrives (ask an operator; the room wires it).
5. What operators can do (Slack wiring, destruction), one short note.

Copy is part of the arc and reviewed like any copy: plain punctuation,
no team-internal jargon.

## Destruction spec amendments (same commit)

- Step 3 tolerance "repo missing = done" is replaced by the shipped,
  ratified behavior: a bare not-found is never proof (the hosting API
  renders lost access identically), the step corroborates via the
  archived name, and when both names are missing the step fails with
  plain copy pointing at repo access.
- The read-only-while-destroying requirement stands; this arc implements
  it (the interim implementation unmounted panels behind a notice).

## Testing

- Tab state: unit tests for the tab model (visible tabs given
  `slack_visible`, default selection, selection persistence within a
  mount).
- Frozen matrix: per-panel factory tests that `frozen` disables each
  interactive control; chat composer included.
- Nav: page-state tests for the new page id; sidebar render states.
- Copy: the em-dash scan as an automated check in the test suite (scan
  `ui/src` rendered literals, ignore comments/tests), so the rule cannot
  silently regress.
- `svelte-check` and production build stay green; UI suite grows from
  253.

## Non-goals

- Slack automation Tier B (own arc).
- Agent avatar art; only the room logo.
- API changes (none expected; `frozen` is derived from status the UI
  already has).
- Theming beyond what the sidebar needs; the room's existing skin stays.
