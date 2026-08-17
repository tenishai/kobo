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

function stripComments(source: string): string {
  return source
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:])\/\/[^\n]*/g, "$1");
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
