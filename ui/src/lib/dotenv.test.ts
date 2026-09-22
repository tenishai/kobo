import { describe, expect, it } from "vitest";
import { keyRefusal, parseEnvPaste, ROOM_SETS_THIS_ONE } from "./dotenv";

describe("keyRefusal", () => {
  it("accepts a name the server would accept", () => {
    for (const key of ["A", "API_KEY", "DB_URL", "X1", "A".repeat(64)]) {
      expect(keyRefusal(key)).toBeNull();
    }
  });

  it("refuses anything that is not upper snake case", () => {
    for (const key of ["lower", "Mixed", "1LEADING", "_LEADING", "HAS-HYPHEN", "HAS SPACE"]) {
      expect(keyRefusal(key)).toContain("capital letters");
    }
  });

  it("refuses a name longer than the server's cap", () => {
    expect(keyRefusal("A".repeat(65))).toContain("64");
  });

  it("refuses every name the room sets itself, in the room's own words", () => {
    for (const key of [
      "CABAL_COLLEAGUE_TOKEN",
      "SLACK_BOT_TOKEN",
      "SLACK_APP_TOKEN",
      "SLACK_SIGNING_SECRET",
      "VERTEXAI_PROJECT",
      "VERTEXAI_LOCATION",
    ]) {
      expect(keyRefusal(key)).toContain(ROOM_SETS_THIS_ONE);
    }
  });

  it("never uses an em dash", () => {
    for (const key of ["lower", "A".repeat(65), "SLACK_BOT_TOKEN"]) {
      expect(keyRefusal(key)).not.toContain("—");
    }
  });
});

describe("parseEnvPaste", () => {
  it("reads plain KEY=value lines", () => {
    const parsed = parseEnvPaste("API_KEY=abc\nDB_URL=postgres://example.test/db\n");
    expect(parsed.problems).toEqual([]);
    expect(parsed.values).toEqual({
      API_KEY: "abc",
      DB_URL: "postgres://example.test/db",
    });
  });

  it("skips blank lines and comments", () => {
    const parsed = parseEnvPaste("\n# a note\n\nAPI_KEY=abc\n");
    expect(parsed.values).toEqual({ API_KEY: "abc" });
    expect(parsed.problems).toEqual([]);
  });

  it("accepts an export prefix, the way a shell file people already have looks", () => {
    expect(parseEnvPaste("export API_KEY=abc").values).toEqual({ API_KEY: "abc" });
  });

  it("strips a matched pair of quotes and nothing else", () => {
    const parsed = parseEnvPaste(`A="one"\nB='two'\nC="mixed'\nD=bare`);
    expect(parsed.values).toEqual({
      A: "one",
      B: "two",
      C: `"mixed'`,
      D: "bare",
    });
  });

  it("keeps a value that is itself empty", () => {
    expect(parseEnvPaste("API_KEY=").values).toEqual({ API_KEY: "" });
  });

  it("names the line number of anything that is not KEY=value", () => {
    const parsed = parseEnvPaste("API_KEY=abc\njust some words\n");
    expect(parsed.problems).toEqual(["Line 2: write this as KEY=value."]);
    // Nothing partial: the caller writes nothing while a problem stands,
    // so the good line above is parsed but not yet anybody's to send.
    expect(parsed.values).toEqual({ API_KEY: "abc" });
  });

  it("puts every key through the same rule a typed one goes through", () => {
    const parsed = parseEnvPaste("lower=1\nSLACK_BOT_TOKEN=2\n");
    expect(parsed.problems[0]).toContain("Line 1");
    expect(parsed.problems[0]).toContain("capital letters");
    expect(parsed.problems[1]).toContain("Line 2");
    expect(parsed.problems[1]).toContain(ROOM_SETS_THIS_ONE);
    expect(parsed.values).toEqual({});
  });

  it("refuses a line with no name at all before the equals sign", () => {
    expect(parseEnvPaste("=value").problems).toEqual([
      "Line 1: write this as KEY=value.",
    ]);
  });

  it("keeps the last of a repeated key, the way a shell would", () => {
    expect(parseEnvPaste("A=one\nA=two").values).toEqual({ A: "two" });
  });
});
