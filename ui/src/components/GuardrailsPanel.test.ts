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
      source: { kind: "persona", ref: "access-agent" },
      added_by: "room",
      added_at: "2026-11-19T10:00:00",
      edited_by: null,
      edited_at: null,
    },
    {
      id: 5,
      text: "Answer only in the request thread.",
      source: { kind: "person", ref: null },
      added_by: "dana@example.test",
      added_at: "2026-11-19T11:00:00",
      edited_by: null,
      edited_at: null,
    },
  ],
  limit_chars: 4000,
  used_chars: 87,
  sha: "a".repeat(64),
  can_edit: true,
};

function savedBody(): {
  rules: Array<{ id?: number | null; text: string }>;
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
    stubFetch({ "/guardrails": { ...RULES, mode: "instant" } });
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
        { id: 4, text: "Never change access without an approved ticket in Jira." },
        { id: 5, text: "Answer only in the request thread." },
      ],
      base_sha: "a".repeat(64),
    });
  });
});
