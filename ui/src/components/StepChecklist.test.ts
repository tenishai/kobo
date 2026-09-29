// @vitest-environment jsdom
//
// Final-review fix (create-identity): a skipped step used to render with
// the exact same green checkmark as a done step (stepTone folded "skipped"
// into "ok"), so "Paint the portrait" showed a check with no portrait.
// Skipped now gets its own honest marker and its reason shown the way a
// failed step shows its own, and is excluded from the "N of M steps done"
// sr-only count.
import { render, screen } from "@testing-library/svelte";
import { describe, expect, it } from "vitest";
import StepChecklist from "./StepChecklist.svelte";
import type { StepView } from "../lib/api";

function step(overrides: Partial<StepView> & { name: string }): StepView {
  return {
    label: "Paint the portrait",
    status: "pending",
    error: null,
    ...overrides,
  };
}

describe("StepChecklist -- per-step tone", () => {
  it("shows a checkmark for a done step and no error details", () => {
    render(StepChecklist, {
      steps: [step({ name: "repo", label: "Create the repo", status: "done" })],
    });

    expect(screen.getByText("✓")).not.toBeNull();
    expect(screen.queryByText(/why it/i)).toBeNull();
  });

  it("shows a distinct neutral marker, not a checkmark, for a skipped step", () => {
    render(StepChecklist, {
      steps: [
        step({
          name: "avatar",
          status: "skipped",
          error: "picture generation is not available",
        }),
      ],
    });

    expect(screen.queryByText("✓")).toBeNull();
    expect(screen.getByText("○")).not.toBeNull();
  });

  it("renders a skipped step's reason the way a failed step shows its own", () => {
    render(StepChecklist, {
      steps: [
        step({
          name: "avatar",
          status: "skipped",
          error: "picture generation is not available",
        }),
      ],
    });

    expect(screen.getByText("Why it was skipped")).not.toBeNull();
    expect(screen.getByText("picture generation is not available")).not.toBeNull();
  });

  it("still shows a failed step's own reason under 'Why it failed'", () => {
    render(StepChecklist, {
      steps: [step({ name: "onboard", status: "failed", error: "gojira never came up" })],
    });

    expect(screen.getByText("Why it failed")).not.toBeNull();
    expect(screen.getByText("gojira never came up")).not.toBeNull();
  });

  it("shows a waiting step's reason, with the pull request as a real link", () => {
    render(StepChecklist, {
      steps: [
        step({
          name: "onboard",
          label: "Platform onboarding",
          status: "waiting",
          error:
            "Waiting for someone on your team to review and merge the setup pull request: https://github.com/o/r/pull/9. The room resumes on its own once it is merged.",
        }),
      ],
    });
    expect(screen.getByText("What it is waiting for")).not.toBeNull();
    const link = screen.getByRole("link", { name: "https://github.com/o/r/pull/9" });
    expect(link.getAttribute("href")).toBe("https://github.com/o/r/pull/9");
    expect(screen.queryByText("Why it failed")).toBeNull();
  });

  it("does not count a waiting step as done", () => {
    render(StepChecklist, {
      steps: [
        step({ name: "a", status: "done" }),
        step({ name: "b", status: "waiting", error: "Waiting: https://x/pull/1" }),
      ],
    });
    expect(screen.getByText("1 of 2 steps done")).not.toBeNull();
  });

  it("shows no error details for a skipped step with no reason", () => {
    render(StepChecklist, {
      steps: [step({ name: "avatar", status: "skipped", error: null })],
    });

    expect(screen.getByText("○")).not.toBeNull();
    expect(screen.queryByText(/why it/i)).toBeNull();
  });

  it("shows the spinner dot for both pending and running steps", () => {
    const { container } = render(StepChecklist, {
      steps: [
        step({ name: "a", status: "pending" }),
        step({ name: "b", status: "running" }),
      ],
    });

    expect(container.querySelectorAll(".step-row__dot.pulse")).toHaveLength(2);
  });
});

describe("StepChecklist -- 'N of M steps done' sr-only count", () => {
  it("counts only done steps; a pending step doesn't count", () => {
    render(StepChecklist, {
      steps: [
        step({ name: "a", status: "done" }),
        step({ name: "b", status: "pending" }),
      ],
    });

    expect(screen.getByRole("status").textContent).toContain("1 of 2 steps done");
  });

  it("excludes a skipped step from N -- it is honestly not done", () => {
    render(StepChecklist, {
      steps: [
        step({ name: "a", status: "done" }),
        step({ name: "b", status: "skipped", error: "not available" }),
        step({ name: "c", status: "pending" }),
      ],
    });

    expect(screen.getByRole("status").textContent).toContain("1 of 3 steps done");
  });

  it("does not count a skipped step as done even when every other step is done", () => {
    render(StepChecklist, {
      steps: [
        step({ name: "a", status: "done" }),
        step({ name: "b", status: "skipped", error: "not available" }),
      ],
    });

    expect(screen.getByRole("status").textContent).toContain("1 of 2 steps done");
  });
});
