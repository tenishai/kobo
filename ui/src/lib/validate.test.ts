import { describe, expect, it } from "vitest";
import { validateName } from "./validate";

describe("validateName", () => {
  it("accepts a plain lowercase name", () => {
    expect(validateName("agent")).toBeNull();
  });

  it("accepts inner hyphens and digits", () => {
    expect(validateName("helper-agent-7")).toBeNull();
    expect(validateName("a1-b2-c3")).toBeNull();
  });

  it("accepts the minimum length (2 chars)", () => {
    expect(validateName("ab")).toBeNull();
  });

  it("accepts the maximum length (24 chars, matching the server's MAX_NAME_LENGTH)", () => {
    expect(validateName("a".repeat(24))).toBeNull();
  });

  it("rejects a name shorter than 2 characters", () => {
    expect(validateName("a")).toBe("Use 2-24 characters.");
    expect(validateName("")).toBe("Use 2-24 characters.");
  });

  it("rejects a name longer than 24 characters", () => {
    expect(validateName("a".repeat(25))).toBe("Use 2-24 characters.");
  });

  it("rejects uppercase letters", () => {
    expect(validateName("Agent")).toBe(
      "Use lowercase letters, numbers, and hyphens only.",
    );
  });

  it("rejects other symbols/spaces", () => {
    expect(validateName("agent_1")).toBe(
      "Use lowercase letters, numbers, and hyphens only.",
    );
    expect(validateName("my agent")).toBe(
      "Use lowercase letters, numbers, and hyphens only.",
    );
  });

  it("rejects a leading digit", () => {
    expect(validateName("1agent")).toBe("Must start with a letter.");
  });

  it("rejects a leading hyphen", () => {
    expect(validateName("-agent")).toBe("Must start with a letter.");
  });

  it("rejects a trailing hyphen (matches the server's own rule)", () => {
    expect(validateName("agent-")).toBe("Can't end with a hyphen.");
  });
});
