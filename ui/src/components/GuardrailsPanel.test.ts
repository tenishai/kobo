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
};

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

describe("GuardrailsPanel", () => {
  beforeEach(() => {
    stubFetch({
      "/guardrails": { ...RULES, mode: "instant" },
      "/api/people/search": { people: [ADA] },
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
        { id: 4, text: "Never change access without an approved ticket in Jira.", people: [] },
        { id: 5, text: "Answer only in the request thread.", people: [] },
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
    expect(items).toEqual(["Edit", "People", "It broke this", "Remove"]);
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
    await fireEvent.input(screen.getByLabelText("Add a person"), { target: { value: "ada" } });
    await fireEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }, { timeout: 2000 }));
    await fireEvent.click(screen.getByRole("button", { name: "Save people" }));

    await screen.findByRole("status");
    expect(savedBody().rules[1]).toEqual({
      id: 5,
      text: "Answer only in the request thread.",
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
    await fireEvent.click(screen.getByRole("button", { name: /People/ }));
    const box = screen.getByLabelText("Add a person");
    await fireEvent.input(box, { target: { value: "grace@example.test" } });
    await fireEvent.keyDown(box, { key: "Enter" });
    await fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await screen.findByRole("status");
    expect(savedBody().rules[2]).toEqual({
      text: "Only answer these people.",
      people: [{ name: "", email: "grace@example.test" }],
    });
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
