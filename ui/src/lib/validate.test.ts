import { describe, expect, it } from "vitest";
import {
  BUILTIN_GROUP,
  EMPTY_PERSONA_TEXT,
  mergeDraftFields,
  personaGroups,
  personaMissing,
  personaTeamOptions,
  splitLines,
  splitTags,
  validateName,
  validateSlug,
} from "./validate";

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

describe("validateSlug", () => {
  it("accepts a plain lowercase slug", () => {
    expect(validateSlug("growth")).toBeNull();
  });

  it("accepts inner hyphens and digits", () => {
    expect(validateSlug("growth-team-7")).toBeNull();
  });

  it("accepts a single character (unlike validateName's 2-char floor)", () => {
    expect(validateSlug("a")).toBeNull();
  });

  it("accepts a trailing hyphen (unlike validateName, the server's slug pattern allows one)", () => {
    expect(validateSlug("growth-")).toBeNull();
  });

  it("accepts the maximum length (24 chars)", () => {
    expect(validateSlug("a".repeat(24))).toBeNull();
  });

  it("rejects an empty string", () => {
    expect(validateSlug("")).toBe("Use 1-24 characters.");
  });

  it("rejects a slug longer than 24 characters", () => {
    expect(validateSlug("a".repeat(25))).toBe("Use 1-24 characters.");
  });

  it("rejects uppercase letters", () => {
    expect(validateSlug("Growth")).toBe(
      "Use lowercase letters, numbers, and hyphens only.",
    );
  });

  it("rejects other symbols/spaces", () => {
    expect(validateSlug("growth_team")).toBe(
      "Use lowercase letters, numbers, and hyphens only.",
    );
    expect(validateSlug("growth team")).toBe(
      "Use lowercase letters, numbers, and hyphens only.",
    );
  });

  it("rejects a leading digit", () => {
    expect(validateSlug("7growth")).toBe("Must start with a letter.");
  });

  it("rejects a leading hyphen", () => {
    expect(validateSlug("-growth")).toBe("Must start with a letter.");
  });
});

describe("splitTags", () => {
  it("splits on commas", () => {
    expect(splitTags("a,b,c")).toEqual(["a", "b", "c"]);
  });

  it("splits on whitespace", () => {
    expect(splitTags("a b c")).toEqual(["a", "b", "c"]);
  });

  it("splits on a mix of commas and whitespace, trimming pieces", () => {
    expect(splitTags("a, b   c,, d ")).toEqual(["a", "b", "c", "d"]);
  });

  it("returns an empty array for blank input", () => {
    expect(splitTags("")).toEqual([]);
    expect(splitTags("   ")).toEqual([]);
    expect(splitTags(" , , ")).toEqual([]);
  });
});

describe("splitLines", () => {
  it("splits one item per line", () => {
    expect(splitLines("first\nsecond\nthird")).toEqual([
      "first",
      "second",
      "third",
    ]);
  });

  it("trims each line and drops blank lines", () => {
    expect(splitLines("  first  \n\n  \nsecond\n")).toEqual([
      "first",
      "second",
    ]);
  });

  it("returns an empty array for blank input", () => {
    expect(splitLines("")).toEqual([]);
    expect(splitLines("\n\n  \n")).toEqual([]);
  });
});

describe("mergeDraftFields", () => {
  const draft = {
    name: "billing-buddy",
    who_they_are: "a calm troubleshooter for billing questions",
    what_they_do: "Helps customers resolve billing issues quickly.",
    rules: ["Never guess a figure.", "Always say what you checked."],
  };
  const DRAFTED = {
    name: draft.name,
    whoTheyAre: draft.who_they_are,
    whatTheyDo: draft.what_they_do,
    rules: "Never guess a figure.\nAlways say what you checked.",
  };

  it("fills all four parts of an empty form, rules one per line", () => {
    const result = mergeDraftFields(EMPTY_PERSONA_TEXT, EMPTY_PERSONA_TEXT, draft);

    expect(result.fields).toEqual(DRAFTED);
    expect(result.drafted).toEqual(DRAFTED);
  });

  it("keeps what a person typed and fills the rest, each field judged on its own", () => {
    const current = { ...EMPTY_PERSONA_TEXT, name: "my-own-name", whatTheyDo: "   " };

    const result = mergeDraftFields(current, EMPTY_PERSONA_TEXT, draft);

    expect(result.fields).toEqual({ ...DRAFTED, name: "my-own-name" });
    // The kept field is not the draft's, so the next draft leaves it too.
    expect(result.drafted.name).toBe("");
  });

  it("a second draft replaces what the first one wrote, and never an edit made since", () => {
    const edited = { ...DRAFTED, whoTheyAre: "a blunt auditor of invoices" };
    const second = {
      name: "ledger",
      who_they_are: "someone else",
      what_they_do: "A longer account of the work.",
      rules: ["Never round."],
    };

    const result = mergeDraftFields(edited, DRAFTED, second);

    expect(result.fields).toEqual({
      name: "ledger",
      whoTheyAre: "a blunt auditor of invoices",
      whatTheyDo: "A longer account of the work.",
      rules: "Never round.",
    });
  });

  // A route that always answers 200 can still hand back a body that does
  // not match its type. That is nothing to fill, never a value written
  // into form state that every binding assumes is a string.
  it("leaves the form as it was when the response is empty or the wrong type", () => {
    const current = { ...EMPTY_PERSONA_TEXT, whatTheyDo: "kept body" };

    expect(mergeDraftFields(current, EMPTY_PERSONA_TEXT, {}).fields).toEqual(current);
    expect(
      mergeDraftFields(current, EMPTY_PERSONA_TEXT, {
        name: 42,
        who_they_are: null,
        rules: "not a list",
        purpose_tags: "x",
      }).fields,
    ).toEqual(current);
  });
});

describe("personaMissing", () => {
  const FULL = {
    name: "ledger-keeper",
    whoTheyAre: "a careful keeper of the books",
    whatTheyDo: "x".repeat(300),
    rules: "one\ntwo\nthree",
  };

  it("names the next thing the form needs, top to bottom, then nothing", () => {
    expect(personaMissing(EMPTY_PERSONA_TEXT, "sre")).toBe("Give it a name.");
    expect(personaMissing({ ...FULL, name: "one two three four" }, "sre")).toBe(
      "Keep the name to one or two words, three at most.",
    );
    expect(personaMissing({ ...FULL, whoTheyAre: " " }, "sre")).toBe("Say who they are in one line.");
    expect(personaMissing({ ...FULL, whatTheyDo: "Helps." }, "sre")).toBe(
      "Say more about what they do: 6 of at least 300 characters.",
    );
    expect(personaMissing({ ...FULL, rules: "one\n\ntwo" }, "sre")).toBe(
      "Add at least 3 rules, one per line.",
    );
    expect(personaMissing(FULL, null)).toBe("Choose a group.");
    expect(personaMissing(FULL, "sre")).toBeNull();
  });

  it("asks nothing of a name the edit form cannot change", () => {
    const old = { ...FULL, name: "one two three four" };
    expect(personaMissing(old, "sre", true)).toBeNull();
    expect(personaMissing({ ...old, rules: "one" }, "sre", true)).toBe(
      "Add at least 3 rules, one per line.",
    );
  });
});

describe("personaGroups", () => {
  const row = (id: string, team: string, builtin = false) => ({ id, team, builtin });

  it("orders Built-in, the caller's groups, other groups, then Everyone, and drops empty ones", () => {
    const groups = personaGroups(
      [
        row("wire-probe", "shared"),
        row("zeta", "qa"),
        row("helper", "shared", true),
        row("stoic", "sre"),
        row("alpha", "billing"),
      ],
      ["sre", "growth"],
    );

    expect(groups.map((g) => [g.key, g.label, g.personas.map((p) => p.id)])).toEqual([
      [BUILTIN_GROUP, "Built-in", ["helper"]],
      ["sre", "sre", ["stoic"]],
      ["billing", "billing", ["alpha"]],
      ["qa", "qa", ["zeta"]],
      ["shared", "Everyone", ["wire-probe"]],
    ]);
  });
});

describe("personaTeamOptions", () => {
  const ROOM = [
    { name: "shared", kind: "shared" as const },
    { name: "sre", kind: "engineering" as const },
    { name: "qa", kind: "engineering" as const },
    { name: "design", kind: "non-engineering" as const },
  ];

  it("is the caller's own groups, without the commons", () => {
    expect(
      personaTeamOptions({ operator: false, groups: ["qa", "shared", "sre"] }, ROOM),
    ).toEqual(["qa", "sre"]);
  });

  it("is every named group for an operator, then any of their own the room does not list", () => {
    expect(personaTeamOptions({ operator: true, groups: ["ops"] }, ROOM)).toEqual([
      "sre",
      "qa",
      "design",
      "ops",
    ]);
  });

  it("is empty before whoami has answered", () => {
    expect(personaTeamOptions(undefined, ROOM)).toEqual([]);
  });
});
