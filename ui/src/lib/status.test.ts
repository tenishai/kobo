import { describe, expect, it } from "vitest";
import {
  TERMINAL,
  agentTone,
  anyAgentActive,
  detailRefetchInterval,
  isDestroyComplete,
  isFrozen,
  isTerminal,
  listRefetchInterval,
  stepTone,
} from "./status";

describe("TERMINAL / isTerminal", () => {
  it("TERMINAL is exactly ready and failed", () => {
    expect(TERMINAL).toEqual(["ready", "failed"]);
  });

  it("treats ready and failed as terminal", () => {
    expect(isTerminal("ready")).toBe(true);
    expect(isTerminal("failed")).toBe(true);
  });

  it("treats pending and provisioning as non-terminal", () => {
    expect(isTerminal("pending")).toBe(false);
    expect(isTerminal("provisioning")).toBe(false);
  });
});

describe("anyAgentActive", () => {
  it("is false for an empty list", () => {
    expect(anyAgentActive([])).toBe(false);
  });

  it("is false when every agent is terminal", () => {
    expect(anyAgentActive([{ status: "ready" }, { status: "failed" }])).toBe(
      false,
    );
  });

  it("is true when any agent is non-terminal", () => {
    expect(
      anyAgentActive([{ status: "ready" }, { status: "provisioning" }]),
    ).toBe(true);
  });
});

describe("isDestroyComplete", () => {
  it("is true when destroying and a 404 arrives", () => {
    expect(isDestroyComplete("destroying", true, 404)).toBe(true);
  });

  it("is true when destroy_failed and a 404 arrives", () => {
    expect(isDestroyComplete("destroy_failed", true, 404)).toBe(true);
  });

  it("is false when ready and a 404 arrives (not mid-destroy)", () => {
    expect(isDestroyComplete("ready", true, 404)).toBe(false);
  });

  it("is false when lastKnownStatus is null and a 404 arrives (unknown name, never loaded)", () => {
    expect(isDestroyComplete(null, true, 404)).toBe(false);
  });

  it("is false when destroying but the error isn't a 404", () => {
    expect(isDestroyComplete("destroying", true, 500)).toBe(false);
  });

  it("is false when destroying but there is no error", () => {
    expect(isDestroyComplete("destroying", false, undefined)).toBe(false);
  });
});

describe("isFrozen", () => {
  it("freezes destroy states", () => {
    expect(isFrozen("destroying")).toBe(true);
    expect(isFrozen("destroy_failed")).toBe(true);
  });

  it("leaves live states alone", () => {
    expect(isFrozen("ready")).toBe(false);
    expect(isFrozen("failed")).toBe(false);
    expect(isFrozen("provisioning")).toBe(false);
  });
});
