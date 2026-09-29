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

describe("listRefetchInterval", () => {
  it("is false for undefined (no data fetched yet)", () => {
    expect(listRefetchInterval(undefined)).toBe(false);
  });

  it("is false for an empty list", () => {
    expect(listRefetchInterval([])).toBe(false);
  });

  it("is 3500 when one agent is provisioning", () => {
    expect(
      listRefetchInterval([{ status: "ready" }, { status: "provisioning" }]),
    ).toBe(3500);
  });

  it("is 3500 when an agent is pending", () => {
    expect(listRefetchInterval([{ status: "pending" }])).toBe(3500);
  });

  it("is false once every agent is ready", () => {
    expect(
      listRefetchInterval([{ status: "ready" }, { status: "ready" }]),
    ).toBe(false);
  });

  it("is false for a mix of only terminal statuses (ready + failed)", () => {
    expect(
      listRefetchInterval([{ status: "ready" }, { status: "failed" }]),
    ).toBe(false);
  });
});

describe("detailRefetchInterval", () => {
  it("is false for undefined (no data fetched yet)", () => {
    expect(detailRefetchInterval(undefined)).toBe(false);
  });

  it("is 3500 while the agent is pending", () => {
    expect(detailRefetchInterval({ status: "pending" })).toBe(3500);
  });

  it("is 3500 while the agent is provisioning", () => {
    expect(detailRefetchInterval({ status: "provisioning" })).toBe(3500);
  });

  it("is false once the agent is ready", () => {
    expect(detailRefetchInterval({ status: "ready" })).toBe(false);
  });

  it("is false once the agent has failed", () => {
    expect(detailRefetchInterval({ status: "failed" })).toBe(false);
  });
});

describe("stepTone", () => {
  it("maps done -> ok", () => {
    expect(stepTone("done")).toBe("ok");
  });

  it("maps skipped -> its own tone (not ok, not fail: a real reason worth showing)", () => {
    expect(stepTone("skipped")).toBe("skipped");
  });

  it("maps failed -> fail", () => {
    expect(stepTone("failed")).toBe("fail");
  });

  it("maps running -> active", () => {
    expect(stepTone("running")).toBe("active");
  });

  it("maps pending -> active (queued work still reads as in-progress)", () => {
    expect(stepTone("pending")).toBe("active");
  });

  it("maps waiting -> its own tone (parked on a person, not failed and not running)", () => {
    expect(stepTone("waiting")).toBe("waiting");
  });
});

describe("agentTone for a parked agent", () => {
  it("shows waiting as warn: still mid-flight from the room's point of view", () => {
    expect(agentTone("waiting")).toBe("warn");
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

describe("agentTone", () => {
  it("maps ready -> ok", () => {
    expect(agentTone("ready")).toBe("ok");
  });

  it("maps failed -> fail", () => {
    expect(agentTone("failed")).toBe("fail");
  });

  it("maps pending -> warn (amber pulse)", () => {
    expect(agentTone("pending")).toBe("warn");
  });

  it("maps provisioning -> warn (amber pulse)", () => {
    expect(agentTone("provisioning")).toBe("warn");
  });

  it("defaults an unrecognized status to warn, not a false ok/fail claim", () => {
    expect(agentTone("something-new")).toBe("warn");
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
