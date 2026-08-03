// Pure line-level diff, no dependencies. Splits each input on "\n" and finds
// the longest common subsequence (LCS) of lines via a standard O(n*m) DP
// table — knowledge files are small, so the quadratic table is fine and
// keeps the algorithm easy to verify by inspection rather than reaching for
// a Myers-diff library.
//
// Trailing-newline convention: an empty string denotes zero lines (there's
// nothing to diff for a file that doesn't exist yet, or was just deleted),
// not one blank line — otherwise `lineDiff("", "a")` would show a spurious
// removed blank line ahead of the real insert. Any non-empty string is
// split on "\n" verbatim, so a single trailing newline ("a\n") produces a
// trailing empty-string line (["a", ""]) that participates in the diff
// like any other line, rather than being stripped.
//
// For a replaced region (a run of lines with no match between the two
// sides), removed lines are always emitted before added lines — in their
// respective original orders — so a whole-block replacement reads as "the
// old lines, then the new lines" instead of interleaved.

export type DiffLine = { kind: "same" | "added" | "removed"; text: string };

function splitLines(text: string): string[] {
  return text === "" ? [] : text.split("\n");
}

export function lineDiff(before: string, after: string): DiffLine[] {
  const a = splitLines(before);
  const b = splitLines(after);
  const n = a.length;
  const m = b.length;

  // dp[i][j] = length of the LCS of a[i:] and b[j:].
  const dp: number[][] = Array.from({ length: n + 1 }, () =>
    new Array(m + 1).fill(0),
  );
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] =
        a[i] === b[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }

  const result: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      result.push({ kind: "same", text: a[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      result.push({ kind: "removed", text: a[i] });
      i++;
    } else {
      result.push({ kind: "added", text: b[j] });
      j++;
    }
  }
  while (i < n) {
    result.push({ kind: "removed", text: a[i] });
    i++;
  }
  while (j < m) {
    result.push({ kind: "added", text: b[j] });
    j++;
  }
  return result;
}
