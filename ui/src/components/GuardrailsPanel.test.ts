// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";

import GuardrailsPanel from "./GuardrailsPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const RULES = {
  rules: [
    {
      id: 4,
      text: "Never change access without an approved ticket.",
      people: [],
      emoji: [],
      channels: [],
      repositories: [],
      line: "Never change access without an approved ticket.",
      source: { kind: "persona", ref: "access-agent" },
      added_by: "room",
      added_at: "2026-11-19T10:00:00",
      edited_by: null,
      edited_at: null,
      breaks: 0,
      last_broken_at: null,
    },
    {
      id: 5,
      text: "Answer only in the request thread.",
      people: [],
      emoji: [],
      channels: [],
      repositories: [],
      line: "Answer only in the request thread.",
      source: { kind: "person", ref: null },
      added_by: "dana@example.test",
      added_at: "2026-11-19T11:00:00",
      edited_by: null,
      edited_at: null,
      breaks: 0,
      last_broken_at: null,
    },
  ],
  limit_chars: 4000,
  used_chars: 87,
  sha: "a".repeat(64),
  enabled: true,
  can_edit: true,
  can_report: true,
  breaks_before_a_check: 2,
};

type SavedRule = {
  id?: number | null;
  text: string;
  people?: Array<{ name: string; email: string }>;
  emoji?: string[];
  channels?: string[];
  repositories?: string[];
};

const NONE = { people: [], emoji: [], channels: [], repositories: [] };

const ADA = { name: "Ada Lovelace", email: "ada@example.test" };

function savedBody(): {
  rules: SavedRule[];
  base_sha: string | null;
} {
  const call = vi
    .mocked(fetch)
    .mock.calls.find(
      ([url, init]) => String(url).includes("/guardrails") && (init as RequestInit)?.method === "PUT",
    );
  expect(call).toBeDefined();
  return JSON.parse(String((call![1] as RequestInit).body));
}

/** Tick one list in the ⋯ beside Add, for the new rule. */
async function openNewRuleList(label: string): Promise<void> {
  await fireEvent.click(
    await screen.findByRole("button", {
      name: "Add people, emoji, channels or repositories to the new rule",
    }),
  );
  await fireEvent.click(screen.getByRole("menuitemcheckbox", { name: label }));
}

describe("GuardrailsPanel", () => {
  beforeEach(() => {
    stubFetch({
      "/guardrails": { ...RULES, mode: "instant" },
      "/api/people/search": { people: [ADA] },
      "/github/repositories": {
        available: true,
        repositories: [
          { full_name: "acme/api", private: true, default_branch: "main" },
          { full_name: "acme/web", private: true, default_branch: "main" },
        ],
        selected: ["acme/web"],
        note: null,
      },
    });
  });

  // Wire payload (PUT /api/agents/{name}/guardrails, issue #524): an edit sends
  // the whole list with each rule's id, so the room keeps where an edited rule
  // came from instead of recording the persona's rule as a person's, and the
  // version it was read at, so a change made since is refused, not overwritten.
  it("saves an edit as the whole list, each rule carrying its id, at the version read", async () => {
    renderWithClient(GuardrailsPanel, { agentName: "gate" });

    await fireEvent.click(
      await screen.findByRole("button", {
        name: "Edit the rule: Never change access without an approved ticket.",
      }),
    );
    const input = screen.getByRole("textbox", { name: "Edit the rule" });
    await fireEvent.input(input, {
      target: { value: "Never change access without an approved ticket in Jira." },
    });
    await fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await screen.findByRole("status");
    expect(savedBody()).toEqual({
      rules: [
        { id: 4, text: "Never change access without an approved ticket in Jira.", ...NONE },
        { id: 5, text: "Answer only in the request thread.", ...NONE },
      ],
      base_sha: "a".repeat(64),
    });
  });

  // One line per rule (2026-11-20): the actions sit behind one ⋯ button that is
  // always on screen, so the list is one line a rule and nobody hunts for them.
  it("keeps each rule's actions behind its own ⋯ button", async () => {
    renderWithClient(GuardrailsPanel, { agentName: "gate" });

    const more = await screen.findByRole("button", {
      name: "More for the rule: Answer only in the request thread.",
    });
    expect(screen.queryByRole("menuitem", { name: "Remove" })).toBeNull();

    await fireEvent.click(more);

    expect(more.getAttribute("aria-expanded")).toBe("true");
    const items = screen.getAllByRole("menuitem").map((item) => item.textContent?.trim());
    expect(items).toEqual([
      "Edit",
      "People",
      "Emoji",
      "Slack channels",
      "Repositories",
      "It broke this",
      "Remove",
    ]);
  });

  // Wire payload (people on a rule, 2026-11-20): the people picked for a rule
  // save with it, each a name and an email, beside the rule's id and words.
  it("saves the people picked for a rule with it", async () => {
    renderWithClient(GuardrailsPanel, { agentName: "gate" });

    await fireEvent.click(
      await screen.findByRole("button", {
        name: "More for the rule: Answer only in the request thread.",
      }),
    );
    await fireEvent.click(screen.getByRole("menuitem", { name: "People" }));
    const person = screen.getByLabelText("Add a person");
    await fireEvent.focus(person);
    await fireEvent.input(person, { target: { value: "ada" } });
    await fireEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }, { timeout: 2000 }));
    await fireEvent.click(screen.getByRole("button", { name: "Save people" }));

    await screen.findByRole("status");
    expect(savedBody().rules[1]).toEqual({
      id: 5,
      text: "Answer only in the request thread.",
      ...NONE,
      people: [ADA],
    });
  });

  // A rule like "Only answer these people" must never be live without them,
  // so a new rule takes its people before it is added, in the same save.
  it("adds a new rule and its people in one save", async () => {
    renderWithClient(GuardrailsPanel, { agentName: "gate" });

    await fireEvent.input(await screen.findByRole("textbox", { name: "New rule" }), {
      target: { value: "Only answer these people." },
    });
    await openNewRuleList("People");
    const box = screen.getByLabelText("Add a person");
    await fireEvent.input(box, { target: { value: "grace@example.test" } });
    await fireEvent.keyDown(box, { key: "Enter" });
    await fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await screen.findByRole("status");
    expect(savedBody().rules[2]).toEqual({
      text: "Only answer these people.",
      ...NONE,
      people: [{ name: "", email: "grace@example.test" }],
    });
  });

  // People open with nobody in it: Add waits. An email typed and never entered
  // still goes with the rule when Add is pressed, since leaving the box adds it.
  it("waits for people when People is open, and keeps an email typed but not entered", async () => {
    renderWithClient(GuardrailsPanel, { agentName: "gate" });

    await fireEvent.input(await screen.findByRole("textbox", { name: "New rule" }), {
      target: { value: "Only answer these people." },
    });
    await openNewRuleList("People");
    const add = screen.getByRole("button", { name: "Add" }) as HTMLButtonElement;
    expect(add.disabled).toBe(true);

    const box = screen.getByLabelText("Add a person");
    await fireEvent.input(box, { target: { value: "grace@example.test" } });
    await fireEvent.blur(box);
    expect(add.disabled).toBe(false);
    await fireEvent.click(add);

    await screen.findByRole("status");
    expect(savedBody().rules[2].people).toEqual([{ name: "", email: "grace@example.test" }]);
  });

  // Wire payload (2026-11-20): emoji, channels and repositories save with the
  // rule as Slack and GitHub name them: no colons, thumbsup as +1, a channel
  // without its #, a repository as owner/name, offered from the room's list.
  it("saves emoji, channels and repositories with a rule", async () => {
    renderWithClient(GuardrailsPanel, { agentName: "gate" });
    const more = await screen.findByRole("button", {
      name: "More for the rule: Answer only in the request thread.",
    });

    await fireEvent.click(more);
    await fireEvent.click(screen.getByRole("menuitem", { name: "Emoji" }));
    const emoji = screen.getByLabelText("Emoji");
    await fireEvent.input(emoji, { target: { value: ":thumbsup:" } });
    await fireEvent.keyDown(emoji, { key: "Enter" });
    await fireEvent.input(emoji, { target: { value: "ticket" } });
    await fireEvent.keyDown(emoji, { key: "Enter" });
    await fireEvent.click(screen.getByRole("button", { name: "Save emoji" }));
    await screen.findByRole("status");
    expect(savedBody().rules[1]).toEqual({
      id: 5,
      text: "Answer only in the request thread.",
      ...NONE,
      emoji: ["+1", "ticket"],
    });
  });

  it("adds a new rule with channels and a repository from the room's list", async () => {
    renderWithClient(GuardrailsPanel, { agentName: "gate" });

    await fireEvent.input(await screen.findByRole("textbox", { name: "New rule" }), {
      target: { value: "Only open pull requests here when asked in these channels." },
    });
    await openNewRuleList("Slack channels");
    const channels = screen.getByLabelText("Slack channels");
    await fireEvent.input(channels, { target: { value: "#SRE-Requests" } });
    await fireEvent.blur(channels);
    await openNewRuleList("Repositories");
    const repos = screen.getByLabelText("Repositories");
    await fireEvent.focus(repos);
    await fireEvent.input(repos, { target: { value: "web" } });
    await fireEvent.click(await screen.findByRole("button", { name: "acme/web" }, { timeout: 2000 }));
    await fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await screen.findByRole("status");
    expect(savedBody().rules[2]).toEqual({
      text: "Only open pull requests here when asked in these channels.",
      ...NONE,
      channels: ["sre-requests"],
      repositories: ["acme/web"],
    });
  });

  // Text a picker can't add (here a repository without its owner) holds Save
  // back instead of being dropped without a word.
  it("waits to save while a picker holds text it can't add", async () => {
    renderWithClient(GuardrailsPanel, { agentName: "gate" });
    await fireEvent.click(
      await screen.findByRole("button", {
        name: "More for the rule: Answer only in the request thread.",
      }),
    );
    await fireEvent.click(screen.getByRole("menuitem", { name: "Repositories" }));
    const repos = screen.getByLabelText("Repositories");
    await fireEvent.input(repos, { target: { value: "acme" } });
    await fireEvent.blur(repos);

    const save = screen.getByRole("button", { name: "Save repositories" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(screen.getByText("Finish or clear what's typed, to save.")).not.toBeNull();
    expect(repos.getAttribute("aria-invalid")).toBe("true");

    await fireEvent.input(repos, { target: { value: "acme/api" } });
    expect(save.disabled).toBe(false);
  });

  it("shows the people a rule is about under it", async () => {
    stubFetch({
      "/guardrails": {
        ...RULES,
        rules: [
          {
            ...RULES.rules[1],
            text: "Only answer these people.",
            people: [ADA],
            line: "Only answer these people. (These people: Ada Lovelace <ada@example.test>)",
          },
        ],
      },
    });
    renderWithClient(GuardrailsPanel, { agentName: "gate" });

    expect(await screen.findByText("These people: Ada Lovelace")).not.toBeNull();
  });
});
