// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RequestAccessCard from "./RequestAccessCard.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const WHOAMI = { email: "me@example.com", operator: false, groups: [], groups_unavailable: false };

describe("RequestAccessCard", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("explains you need an agent of your own first when the picker would be empty", async () => {
    stubFetch({
      "/api/whoami": WHOAMI,
      "/api/agents": {
        agents: [
          { name: "someone-elses-bot", status: "ready", model: "m", model_label: "M", created_at: "2026-08-01T00:00:00Z", has_avatar: false, created_by: "other@example.com", group_name: "growth" },
        ],
        groups_unavailable: false,
      },
    });
    renderWithClient(RequestAccessCard, { toAgentName: "target-bot" });

    expect(
      await screen.findByText(/you need an agent of your own/i),
    ).not.toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("filters the picker to agents the caller owns (created_by matches whoami email, case-insensitively)", async () => {
    stubFetch({
      "/api/whoami": WHOAMI,
      "/api/agents": {
        agents: [
          { name: "my-bot", status: "ready", model: "m", model_label: "M", created_at: "2026-08-01T00:00:00Z", has_avatar: false, created_by: "ME@example.com", group_name: "growth" },
          { name: "someone-elses-bot", status: "ready", model: "m", model_label: "M", created_at: "2026-08-01T00:00:00Z", has_avatar: false, created_by: "other@example.com", group_name: "growth" },
          { name: "unowned-bot", status: "ready", model: "m", model_label: "M", created_at: "2026-08-01T00:00:00Z", has_avatar: false, created_by: null, group_name: null },
        ],
        groups_unavailable: false,
      },
    });
    renderWithClient(RequestAccessCard, { toAgentName: "target-bot" });

    const picker = (await screen.findByRole("combobox")) as HTMLSelectElement;
    const optionValues = Array.from(picker.options).map((o) => o.value);
    expect(optionValues).toEqual(["my-bot"]);
    expect(screen.queryByText("someone-elses-bot")).toBeNull();
  });

  it("submitting posts the picked from-agent and the target to-agent", async () => {
    stubFetch({
      "/api/whoami": WHOAMI,
      "/api/agents": {
        agents: [
          { name: "my-bot", status: "ready", model: "m", model_label: "M", created_at: "2026-08-01T00:00:00Z", has_avatar: false, created_by: "me@example.com", group_name: "growth" },
          { name: "my-other-bot", status: "ready", model: "m", model_label: "M", created_at: "2026-08-01T00:00:00Z", has_avatar: false, created_by: "me@example.com", group_name: "growth" },
        ],
        groups_unavailable: false,
      },
    });
    renderWithClient(RequestAccessCard, { toAgentName: "target-bot" });

    const picker = (await screen.findByRole("combobox")) as HTMLSelectElement;
    await fireEvent.change(picker, { target: { value: "my-other-bot" } });
    await fireEvent.click(screen.getByRole("button", { name: /request colleague access/i }));

    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/agents/my-other-bot/invites" &&
          (init as RequestInit | undefined)?.method === "POST",
      );
    expect(call).toBeDefined();
    const body = JSON.parse((call as [RequestInfo | URL, RequestInit])[1].body as string);
    expect(body).toEqual({ to_agent: "target-bot" });
  });

});
