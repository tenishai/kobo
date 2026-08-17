// The em-dash guard: user-facing copy uses plain punctuation. Scans every
// .svelte file's template text and script string literals; code comments
// and test files are exempt by construction.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = join(__dirname, "..");

function svelteFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      out.push(...svelteFiles(full));
    } else if (entry.endsWith(".svelte")) {
      out.push(full);
    }
  }
  return out;
}

/** Finds the start of a real `//` line comment on `line`, starting the
 * search at `from` — skipping any `//` that's actually part of a `://` (a
 * URL scheme), which is not a comment opener. Returns -1 if there isn't one. */
function findLineCommentStart(line: string, from: number): number {
  let i = from;
  while (i < line.length) {
    const idx = line.indexOf("//", i);
    if (idx === -1) {
      return -1;
    }
    if (idx > 0 && line[idx - 1] === ":") {
      i = idx + 2; // part of `://` — keep looking past it
      continue;
    }
    return idx;
  }
  return -1;
}

/** Strips HTML comments, then walks the source line by line tracking
 * whether a `/* ... *\/` block comment is currently open. This replaces a
 * prior three-regex approach whose block-comment regex ran over the whole
 * source first: a `//` comment whose prose merely contained a literal `/*`
 * substring (e.g. a glob like `tools/*.{py,go}`) was read as a real opener,
 * and the regex then greedily swallowed everything up to the next `*\/`
 * (real or itself accidental) — hiding live code, including user-facing
 * copy, from the guard below.
 *
 * Per line: if already inside a block comment, look only for `*\/` (exit,
 * keep scanning any remainder); otherwise find the next real `/*` and `//`
 * (skipping `://`) and take whichever comes first — a `//` truncates the
 * rest of the line as a comment (same as before, and never opens a block
 * comment from text after it); a `/*` opens block-comment state and scanning
 * continues right after it, on the same line, so a comment and real code can
 * still share a line. */
function stripComments(source: string): string {
  const withoutHtmlComments = source.replace(/<!--[\s\S]*?-->/g, "");
  const lines = withoutHtmlComments.split("\n");
  const out: string[] = [];
  let inBlockComment = false;

  for (const line of lines) {
    let result = "";
    let i = 0;
    while (i < line.length) {
      if (inBlockComment) {
        const end = line.indexOf("*/", i);
        if (end === -1) {
          i = line.length; // block comment continues onto the next line
          break;
        }
        inBlockComment = false;
        i = end + 2;
        continue;
      }

      const blockStart = line.indexOf("/*", i);
      const lineStart = findLineCommentStart(line, i);

      if (blockStart === -1 && lineStart === -1) {
        result += line.slice(i);
        break;
      }
      if (lineStart !== -1 && (blockStart === -1 || lineStart < blockStart)) {
        // A real `//` comes first — the rest of the line is a comment.
        result += line.slice(i, lineStart);
        break;
      }
      // A real `/*` comes first — open block-comment state and keep
      // scanning right after it, still on this line.
      result += line.slice(i, blockStart);
      inBlockComment = true;
      i = blockStart + 2;
    }
    out.push(result);
  }

  return out.join("\n");
}

function offendingLines(source: string): string[] {
  const stripped = stripComments(source);
  return stripped
    .split("\n")
    .filter((line) => line.includes("—"));
}

describe("user-facing copy", () => {
  it("contains no em dashes outside comments", () => {
    const offenders: string[] = [];
    for (const file of svelteFiles(ROOT)) {
      for (const line of offendingLines(readFileSync(file, "utf8"))) {
        offenders.push(`${file.replace(ROOT, "src")}: ${line.trim()}`);
      }
    }
    expect(offenders, offenders.join("\n")).toEqual([]);
  });
});

describe("stripComments", () => {
  it("does not let a // comment's glob pattern open a phantom block comment (the ToolingPanel regression)", () => {
    // Reproduces the exact shape that slipped past the old three-regex
    // version: a `//` comment whose prose contains a literal `/*` (a glob
    // like `tools/*.{py,go}`), hundreds of characters before the next real
    // block comment's closing `*/`. Live code — including a user-facing em
    // dash — sits between the two and must still be scanned.
    const filler = Array.from(
      { length: 20 },
      (_, i) => `  // filler doc-comment line ${i}, padding out the distance to the real close`,
    ).join("\n");
    const source = [
      "  // land in `bundle/tools/*.{py,go}` in the agent's own repo, validated and",
      "  // delivered the same way knowledge/skills are.",
      filler,
      '  const liveNotice = "Stored and delivered — this must still be flagged";',
      "  /** a real block comment that finally",
      "   * closes several lines down here */",
    ].join("\n");

    const stripped = stripComments(source);

    expect(stripped).toContain("Stored and delivered — this must still be flagged");
  });

  it("does not treat :// inside a URL as a line-comment opener", () => {
    const source = 'const helpUrl = "https://example.com/path"; // trailing note — stripped';

    const stripped = stripComments(source);

    expect(stripped).toContain("https://example.com/path");
    expect(stripped).not.toContain("trailing note");
  });

  it("still strips real block comments", () => {
    const source = [
      "const a = 1;",
      "/* a real block comment",
      "   spanning several lines — with a dash that must not be flagged */",
      'const b = "kept — flagged";',
    ].join("\n");

    const stripped = stripComments(source);

    expect(stripped).not.toContain("spanning several lines");
    expect(stripped).toContain('const b = "kept — flagged";');
  });

  it("scans the code before a trailing // comment, but not the comment itself", () => {
    // The string literal before `//` is real code and must be scanned; the
    // line comment after it is exempt, per the existing accepted behavior
    // for line comments (truncate the rest of the line).
    const source =
      'const notice = "Stored and delivered — flagged"; // internal note — not flagged';

    const stripped = stripComments(source);

    expect(stripped).toContain("Stored and delivered — flagged");
    expect(stripped).not.toContain("internal note");
    expect(stripped.match(/—/g)?.length).toBe(1);
  });
});
