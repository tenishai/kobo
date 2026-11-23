import { QueryClient } from "@tanstack/svelte-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { endRestart, isRestarting } from "./delivery.svelte";
import { teachMutation, uploadIntakeMutation } from "./queries";

describe("teachMutation", () => {
  afterEach(() => endRestart("gate"));

  // 2026-11-20: a taught note showed in Learnings only after a refresh. A
  // teach refreshes everything on the agent's page, and only one the room had
  // to commit (no live hand-over) restarts the agent.
  it("refreshes the agent's page, Learnings included, and restarts only on a commit", () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");

    teachMutation(client, "gate").onSuccess({ filename: "note.md", mode: "instant" });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["agents", "gate"] });
    expect(isRestarting("gate")).toBe(false);

    teachMutation(client, "gate").onSuccess({ filename: "note.md", mode: "archive", sha: "abc" });
    expect(isRestarting("gate")).toBe(true);
  });
});

describe("uploadIntakeMutation", () => {
  afterEach(() => endRestart("gate"));

  it("restarts the agent only when the room had to commit the file", () => {
    const client = new QueryClient();
    uploadIntakeMutation(client, "gate").onSuccess({ mode: "instant" });
    expect(isRestarting("gate")).toBe(false);
    uploadIntakeMutation(client, "gate").onSuccess({ mode: "archive", sha: "abc" });
    expect(isRestarting("gate")).toBe(true);
  });
});
