import { describe, expect, it } from "vitest";
import { lineDiff } from "./diff";

describe("lineDiff", () => {
  it("marks every line same when before and after are identical", () => {
    expect(lineDiff("a\nb\nc", "a\nb\nc")).toEqual([
      { kind: "same", text: "a" },
      { kind: "same", text: "b" },
      { kind: "same", text: "c" },
    ]);
  });

  it("marks a pure insertion as one added line among same lines", () => {
    expect(lineDiff("a\nc", "a\nb\nc")).toEqual([
      { kind: "same", text: "a" },
      { kind: "added", text: "b" },
      { kind: "same", text: "c" },
    ]);
  });

  it("marks a pure deletion as one removed line among same lines", () => {
    expect(lineDiff("a\nb\nc", "a\nc")).toEqual([
      { kind: "same", text: "a" },
      { kind: "removed", text: "b" },
      { kind: "same", text: "c" },
    ]);
  });

  it("emits removed lines before added lines for a replaced region", () => {
    expect(lineDiff("a\nb\nc\nd", "a\nx\ny\nd")).toEqual([
      { kind: "same", text: "a" },
      { kind: "removed", text: "b" },
      { kind: "removed", text: "c" },
      { kind: "added", text: "x" },
      { kind: "added", text: "y" },
      { kind: "same", text: "d" },
    ]);
  });

  it("marks every line added when before is empty", () => {
    expect(lineDiff("", "a\nb")).toEqual([
      { kind: "added", text: "a" },
      { kind: "added", text: "b" },
    ]);
  });

  it("marks every line removed when after is empty", () => {
    expect(lineDiff("a\nb", "")).toEqual([
      { kind: "removed", text: "a" },
      { kind: "removed", text: "b" },
    ]);
  });

  it("is a no-op (empty array) when both before and after are empty", () => {
    expect(lineDiff("", "")).toEqual([]);
  });

  it("preserves original ordering across multiple separate changes", () => {
    expect(lineDiff("a\nb\nc\nd\ne", "a\nx\nc\ny\ne")).toEqual([
      { kind: "same", text: "a" },
      { kind: "removed", text: "b" },
      { kind: "added", text: "x" },
      { kind: "same", text: "c" },
      { kind: "removed", text: "d" },
      { kind: "added", text: "y" },
      { kind: "same", text: "e" },
    ]);
  });

  it("pins the trailing-newline convention: a single trailing newline is a trailing empty-string line, not stripped", () => {
    // split("\n") on "a\n" is ["a", ""] — the trailing empty line is real
    // content (an unterminated blank line) and diffs like any other line,
    // distinct from a wholly-empty string (which means zero lines).
    expect(lineDiff("a\n", "a\n")).toEqual([
      { kind: "same", text: "a" },
      { kind: "same", text: "" },
    ]);
  });

  it("treats adding a trailing newline as appending an empty line", () => {
    expect(lineDiff("a", "a\n")).toEqual([
      { kind: "same", text: "a" },
      { kind: "added", text: "" },
    ]);
  });
});
