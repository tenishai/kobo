# Hardening + Stewardship Wave Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the create_skill publish-brick risk with a pre-push frontmatter gate, harden audit arg bounding, add a reversible persona archive lane, and land the persona naming law across every authoring surface.

**Architecture:** Four seams, no new subsystems. The frontmatter gate extends `marketplace.py`'s existing completion step (law hardcoded, vocabularies via a new optional config block). Audit bounding extends `_bounded` in place. Archive is a soft flag on the persona bucket index: ingest drops archived entries so every list surface (search route, picker, MCP) is clean automatically; a dedicated archived-listing route serves the UI. Naming law lands as copy: docstring, skill v1.0.1, PersonaBuilder microcopy, plus a new Personas catalog page that demonstrates the display hierarchy.

**Tech Stack:** FastAPI + pydantic (api/), sqlite FTS5 catalog, Svelte 5 runes + TanStack Query (ui/), pytest (no pytest-asyncio; drive async via `asyncio.run`), vitest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-24-hardening-stewardship-wave-design.md`. Rulings there are law.
- TDD, RED first: every behavior change lands with a test that fails against the old code. Run the failing test BEFORE implementing.
- Never name the marketplace repo or any vendor in tracked files: scan changed files against `.superpowers/sdd/vendor-names.txt` before EVERY commit (`git diff --cached --name-only | xargs grep -l -f .superpowers/sdd/vendor-names.txt` must return nothing).
- No em dashes in any user-facing copy string (docstrings, error messages, UI copy, skill content). Use plain hyphens or commas.
- API suite: `cd api && pytest -q` green before every commit. UI suite: `cd ui && npx vitest run` green before UI commits.
- The MCP tool count stays 18. The exact-set test in `api/tests/test_mcp_tools_read.py` must not change.
- Commit after every task, message prefix `feat:`/`fix:`/`docs:` per house style.

## Plan-time refinements (deviations from spec letter, same intent)

1. Spec said `GET /api/personas` gains `include_archived`. The room has no plain persona list route; every list surface is FTS-catalog-backed (`/api/personas/search`). Realized instead as: ingest drops archived entries (all active-list surfaces clean by construction) plus a new `GET /api/personas/archived` reading the bucket index directly for the UI's archived section.
2. Spec said archive controls follow "existing persona-management markup". No persona-management view exists. Realized as a new minimal Personas catalog page (Task 7), which also gives the naming-law display hierarchy a home.
3. The create-agent route already answers 422 `unknown persona` for anything not in the catalog. The honest "is archived" message is produced by checking the bucket index only on that failure arm (one storage read, failure path only).

---

### Task 1: `skills_frontmatter` config block

**Files:**
- Modify: `api/kobo_api/config.py` (beside `class MCPConfig`, ~line 103, and the `Config` fields ~line 213)
- Test: `api/tests/test_config.py`

**Interfaces:**
- Produces: `SkillsFrontmatterConfig(BaseModel)` with `allowed_profiles: list[str] = []` and `allowed_tags: list[str] = []`; `Config.skills_frontmatter: SkillsFrontmatterConfig` defaulting to the empty block. Task 2 reads `config.skills_frontmatter.allowed_profiles` / `.allowed_tags`.

- [ ] **Step 1: Write the failing tests**

Append to `api/tests/test_config.py` (match the file's existing Config-construction idiom; read its head first):

```python
def test_skills_frontmatter_defaults_empty():
    cfg = Config()
    assert cfg.skills_frontmatter.allowed_profiles == []
    assert cfg.skills_frontmatter.allowed_tags == []


def test_skills_frontmatter_accepts_vocabularies():
    cfg = Config(
        skills_frontmatter={
            "allowed_profiles": ["agent-sdk"],
            "allowed_tags": ["networking", "agent-ops"],
        }
    )
    assert cfg.skills_frontmatter.allowed_profiles == ["agent-sdk"]
    assert "agent-ops" in cfg.skills_frontmatter.allowed_tags
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd api && pytest -q tests/test_config.py -k skills_frontmatter`
Expected: FAIL (`AttributeError` / validation error: unknown field).

- [ ] **Step 3: Implement**

In `api/kobo_api/config.py`, add below `MCPConfig` (mirror its placement and comment register):

```python
class SkillsFrontmatterConfig(BaseModel):
    """Optional vocabularies for the marketplace frontmatter gate
    (hardening wave). Each configured list adds a subset check in
    kobo_api.marketplace._validate_skill_frontmatter; an empty list adds
    nothing, so the gate is fully functional with this block absent and
    vocabulary drift can never brick publishing."""

    allowed_profiles: list[str] = []
    allowed_tags: list[str] = []
```

Add to `Config` beside the other skills fields (~line 213):

```python
    skills_frontmatter: SkillsFrontmatterConfig = Field(default_factory=SkillsFrontmatterConfig)
```

- [ ] **Step 4: Run tests**

Run: `cd api && pytest -q tests/test_config.py`
Expected: PASS, whole file.

- [ ] **Step 5: Vendor scan + commit**

```bash
git add api/kobo_api/config.py api/tests/test_config.py
git diff --cached --name-only | xargs grep -l -f .superpowers/sdd/vendor-names.txt || git commit -m "feat(config): optional skills_frontmatter vocabulary block"
```

---

### Task 2: Frontmatter gate in `create_skill`

**Files:**
- Modify: `api/kobo_api/marketplace.py` (`create_skill`, lines 166-222; new helpers above it)
- Modify: `api/kobo_api/mcp/tools/write.py` (`kobo_create_skill` docstring, lines 895-911; rider (q) same docstring)
- Test: `api/tests/test_marketplace.py`, `api/tests/test_mcp_tools_write.py` (rider (o))

**Interfaces:**
- Consumes: `config.skills_frontmatter` (Task 1).
- Produces: `_complete_skill_frontmatter(frontmatter: dict) -> None` and `_validate_skill_frontmatter(frontmatter: dict, config: Config) -> None` (raises `MarketplaceError`), both called inside `create_skill` between the existing completion and the body check. Callers see no signature change.

- [ ] **Step 1: Write the failing tests**

Append to `api/tests/test_marketplace.py`, reusing its `FakeGitHub` seeding idiom (read the file's existing create_skill tests first; `GOOD_SKILL` and `Config` construction are already there). The named brick regression leads:

```python
def _gate_config(**kwargs) -> Config:
    return Config(github_org="org", skills_repo="skills", **kwargs)


def test_create_skill_completes_version_and_profiles_2026_08_23_brick_regression():
    """The exact 2026-08-23 shape: caller content with no version and no
    profiles used to publish as-is and brick the whole publish pipeline.
    Completion now makes it lawful from birth."""
    github = FakeGitHub()
    create_skill(github, _gate_config(), skill_id="ptr-primer",
                 content=GOOD_SKILL, team="sre", author="a@b.c")
    pushed = github.pushed_file("skills/ptr-primer/SKILL.md")
    assert "version: 1.0.0" in pushed
    assert "profiles:" in pushed and "agent-sdk" in pushed


def test_create_skill_refuses_malformed_version():
    github = FakeGitHub()
    bad = "---\nversion: 2.oops\n---\nBody.\n"
    with pytest.raises(MarketplaceError, match="version"):
        create_skill(github, _gate_config(), skill_id="v-bad",
                     content=bad, team="sre", author="a@b.c")


def test_create_skill_refuses_float_version_yaml_gotcha():
    """`version: 1.0` parses as a YAML float, not a string. Refused with
    the field named rather than published unlawfully."""
    github = FakeGitHub()
    bad = "---\nversion: 1.0\n---\nBody.\n"
    with pytest.raises(MarketplaceError, match="version"):
        create_skill(github, _gate_config(), skill_id="v-float",
                     content=bad, team="sre", author="a@b.c")


def test_create_skill_refuses_missing_tags():
    github = FakeGitHub()
    with pytest.raises(MarketplaceError, match="tags"):
        create_skill(github, _gate_config(), skill_id="no-tags",
                     content=GOOD_SKILL, team="sre", author="a@b.c")


def test_create_skill_vocab_unknown_profile_refused_when_configured():
    github = FakeGitHub()
    cfg = _gate_config(skills_frontmatter={"allowed_profiles": ["agent-sdk"]})
    content = "---\ntags: [networking]\nprofiles: [desktop]\n---\nBody.\n"
    with pytest.raises(MarketplaceError, match="profiles"):
        create_skill(github, cfg, skill_id="vocab-bad",
                     content=content, team="sre", author="a@b.c")


def test_create_skill_vocab_unconfigured_passes_any_profile():
    github = FakeGitHub()
    content = "---\ntags: [networking]\nprofiles: [desktop]\n---\nBody.\n"
    create_skill(github, _gate_config(), skill_id="vocab-open",
                 content=content, team="sre", author="a@b.c")
    assert github.pushed_file("skills/vocab-open/SKILL.md")
```

NOTE: `GOOD_SKILL` has no frontmatter, so after completion its `tags` is still absent; `test_create_skill_refuses_missing_tags` pins that refusal, and the brick-regression test therefore needs tags in its content. Adjust it to use:

```python
GATED_GOOD_SKILL = "---\ntags: [networking]\n---\n# PTR primer\n\nA PTR record maps an IP back to a hostname.\n"
```

and pass `content=GATED_GOOD_SKILL` there (keep `GOOD_SKILL` untouched for the pre-gate tests, which must now also gain `tags` where they expect success -- update ONLY the ones that break, and verify each break is the tags refusal, not collateral).

- [ ] **Step 2: Run to verify the new tests fail**

Run: `cd api && pytest -q tests/test_marketplace.py -k "brick_regression or malformed_version or float_version or missing_tags or vocab"`
Expected: FAIL (gate helpers not defined / no refusal raised).

- [ ] **Step 3: Implement the gate**

In `api/kobo_api/marketplace.py`, add above `create_skill`:

```python
# The publish law (hardening wave): the org publish pipeline validates the
# WHOLE corpus and uploads nothing when any one skill is unlawful, so an
# unlawful publish from the room's direct-commit lane bricks every team's
# publishes until fixed (lived 2026-08-23, ~35 minutes). This gate makes
# the room enforce the law pre-push. Structural rules are hardcoded here;
# org-specific vocabularies ride config.skills_frontmatter (each empty
# list adds no check). Drift stays bounded: the pipeline is the backstop.
_SEMVER_RE = re.compile(r"\d+\.\d+\.\d+")
_REQUIRED_SKILL_FIELDS = ("name", "description", "version", "tags", "profiles")


def _complete_skill_frontmatter(frontmatter: dict) -> None:
    """Fill the two lawful defaults, in place: version 1.0.0 (a create is
    the first version by construction -- publishes never overwrite) and
    profiles [agent-sdk] (the room's lane). Same complete-the-boring-parts
    philosophy as the name/owner/description completion in create_skill."""
    if not frontmatter.get("version"):
        frontmatter["version"] = "1.0.0"
    if not frontmatter.get("profiles"):
        frontmatter["profiles"] = ["agent-sdk"]


def _validate_skill_frontmatter(frontmatter: dict, config: Config) -> None:
    """Raise MarketplaceError (field named, expectation stated) when the
    completed frontmatter would fail the org publish law. Runs AFTER
    completion, so defaults flow through validation like caller values."""
    for field in _REQUIRED_SKILL_FIELDS:
        if not frontmatter.get(field):
            raise MarketplaceError(
                f"frontmatter is missing required field {field!r} "
                f"(the publish law requires: {', '.join(_REQUIRED_SKILL_FIELDS)})"
            )
    version = frontmatter["version"]
    if not isinstance(version, str) or not _SEMVER_RE.fullmatch(version):
        raise MarketplaceError(
            f"frontmatter version {version!r} must be a three-part semver string, e.g. \"1.0.0\""
        )
    for field in ("tags", "profiles"):
        value = frontmatter[field]
        if not isinstance(value, list) or not all(isinstance(i, str) and i for i in value):
            raise MarketplaceError(
                f"frontmatter {field} must be a non-empty list of non-empty strings"
            )
    law = config.skills_frontmatter
    for field, allowed in (("profiles", law.allowed_profiles), ("tags", law.allowed_tags)):
        if not allowed:
            continue
        unknown = sorted(set(frontmatter[field]) - set(allowed))
        if unknown:
            raise MarketplaceError(
                f"frontmatter {field} {unknown!r} not in the configured vocabulary {sorted(allowed)!r}"
            )
```

Wire into `create_skill` immediately after the existing `description` completion (line ~201) and BEFORE the body check:

```python
    _complete_skill_frontmatter(frontmatter)
    _validate_skill_frontmatter(frontmatter, config)
```

- [ ] **Step 4: Run the new tests, then the whole file**

Run: `cd api && pytest -q tests/test_marketplace.py`
Expected: new tests PASS. Pre-existing create_skill success tests that now refuse on missing tags: add `tags: [x]` frontmatter to their content ONLY where the failure message names tags. Re-run until green.

- [ ] **Step 5: Mutation-pin the gate**

Temporarily delete the `_validate_skill_frontmatter(frontmatter, config)` call line, run `pytest -q tests/test_marketplace.py -k "malformed_version"`, confirm it FAILS (the pin has teeth), restore the line, re-run, confirm PASS. Record the mutation result in the task report.

- [ ] **Step 6: Update the MCP docstring (disclosure + rider q)**

In `api/kobo_api/mcp/tools/write.py` `kobo_create_skill` docstring (lines 900-905), replace:

```
        id (must not already exist) and `content` is the skill's Markdown
        body, frontmatter and all; `name`/`owner` in that frontmatter are
        set to `skill_id`/`team` for you when absent. If you paste an
```

with:

```
        id (must not already exist) and `content` is the skill's Markdown
        body, frontmatter and all; `name` and `owner` are always set to
        `skill_id`/`team` for you (overwriting any pasted values), and
        `description`/`version`/`profiles` are filled with lawful defaults
        when absent. `tags` is required in the frontmatter and must be a
        non-empty list; unlawful frontmatter is refused with the field
        named, never pushed. If you paste an
```

- [ ] **Step 7: Rider (o), tighten did-not-answer matches**

In `api/tests/test_mcp_tools_write.py` and `api/tests/test_mcp_tools_read.py`, find every assertion matching the sanitized transport error loosely (grep `did not answer`). Point each at the exact module constant instead (`_MARKETPLACE_DID_NOT_ANSWER` in read.py:80 for marketplace tools, `_SWITCHBOARD_DID_NOT_ANSWER` read.py:53, `_DID_NOT_ANSWER` write.py:343), importing the constant rather than repeating the string, so the two constants can never be confused by a loose match again.

- [ ] **Step 8: Full suite + commit**

```bash
cd api && pytest -q
git add api/kobo_api/marketplace.py api/kobo_api/mcp/tools/write.py api/tests/test_marketplace.py api/tests/test_mcp_tools_write.py api/tests/test_mcp_tools_read.py
git diff --cached --name-only | xargs grep -l -f .superpowers/sdd/vendor-names.txt || git commit -m "feat(marketplace): frontmatter-law gate in create_skill -- completion, structural rules, config vocabularies"
```

---

### Task 3: Audit `_bounded` hardening

**Files:**
- Modify: `api/kobo_api/mcp/audit.py` (`_bounded`, lines 82-97; `_redact` docstring, lines 135-142)
- Test: `api/tests/test_mcp_audit.py`

**Interfaces:**
- Produces: `_bounded(value: Any, depth: int = 0) -> Any` with `_MAX_LIST_ELEMENTS = 64` and `_MAX_DEPTH = 8` module constants. `_redact` calls it unchanged (`_bounded(value)`).

- [ ] **Step 1: Write the failing tests**

Append to `api/tests/test_mcp_audit.py` (module constants `EMAIL` etc. already exist; these are direct `_bounded`/`_redact` unit tests, no async needed):

```python
def test_bounded_list_at_cap_untouched():
    value = list(range(64))
    assert audit._bounded(value) == value


def test_bounded_list_over_cap_truncates_with_marker():
    value = list(range(65))
    out = audit._bounded(value)
    assert len(out) == 65  # 64 kept + 1 marker
    assert out[:64] == list(range(64))
    assert out[64] == "[+1 more]"


def test_bounded_marker_counts_all_dropped():
    out = audit._bounded(list(range(200)))
    assert out[64] == "[+136 more]"


def test_bounded_recurses_into_dict_values():
    long = "x" * 600
    out = audit._bounded({"skills": [{"id": long, "team": "sre"}]})
    inner = out["skills"][0]["id"]
    assert inner.startswith("x" * 512)
    assert "[truncated 88 chars]" in inner
    assert out["skills"][0]["team"] == "sre"


def test_bounded_depth_capped():
    nested: dict = {"k": "v"}
    for _ in range(12):
        nested = {"k": nested}
    out = audit._bounded(nested)
    probe = out
    for _ in range(7):
        probe = probe["k"]
    assert probe == "[depth capped]"


def test_bounded_never_mutates_caller_structures():
    original = {"rules": [str(i) for i in range(100)], "inner": {"long": "y" * 600}}
    snapshot = json.loads(json.dumps(original))
    audit._redact(dict(original))
    assert original == snapshot
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd api && pytest -q tests/test_mcp_audit.py -k bounded`
Expected: FAIL (no marker, no dict recursion, no depth cap).

- [ ] **Step 3: Implement**

Replace `_bounded` in `api/kobo_api/mcp/audit.py` (keep `_ARG_TEXT_CAP = 512` and its comment):

```python
# Bounding caps (hardening wave): 512 chars per string, 64 elements per
# list (with an explicit dropped-count marker), 8 levels of nesting. The
# audit path must never crash, hang, or balloon a tool call, whatever
# shape a future tool's kwargs take.
_MAX_LIST_ELEMENTS = 64
_MAX_DEPTH = 8


def _bounded(value: Any, depth: int = 0) -> Any:
    """Cap string values at _ARG_TEXT_CAP with an explicit truncation
    marker; lists are bounded per element AND per count (first
    _MAX_LIST_ELEMENTS kept, one "[+N more]" marker appended); dict
    VALUES are bounded recursively (keys are schema-shaped, not user
    text). Anything nested past _MAX_DEPTH collapses to a sentinel.
    Always returns NEW lists/dicts -- callers' kwargs are never mutated.
    Everything else passes through for json.dumps's default=str."""
    if depth >= _MAX_DEPTH:
        return "[depth capped]"
    if isinstance(value, str) and len(value) > _ARG_TEXT_CAP:
        return value[:_ARG_TEXT_CAP] + f"...[truncated {len(value) - _ARG_TEXT_CAP} chars]"
    if isinstance(value, list):
        bounded = [_bounded(item, depth + 1) for item in value[:_MAX_LIST_ELEMENTS]]
        dropped = len(value) - _MAX_LIST_ELEMENTS
        if dropped > 0:
            bounded.append(f"[+{dropped} more]")
        return bounded
    if isinstance(value, dict):
        return {key: _bounded(item, depth + 1) for key, item in value.items()}
    return value
```

Update `_redact`'s docstring list clause (lines 138-141) to say "a list is bounded per element and per count, a dict per value".

- [ ] **Step 4: Run the audit file, then full suite**

Run: `cd api && pytest -q tests/test_mcp_audit.py && pytest -q`
Expected: PASS. (If an existing test pinned the OLD list passthrough of >64 elements, adjust it to the new law and say so in the report.)

- [ ] **Step 5: Commit**

```bash
git add api/kobo_api/mcp/audit.py api/tests/test_mcp_audit.py
git diff --cached --name-only | xargs grep -l -f .superpowers/sdd/vendor-names.txt || git commit -m "feat(mcp): bound audit args by count and depth, recurse dicts -- registered fast-follow"
```

---

### Task 4: Persona archive primitives + ingest filter + seeding preserve

**Files:**
- Modify: `api/kobo_api/personas.py` (new function beside `_write_index`, line ~283; `seed_default_personas` merge, lines 404-415)
- Modify: `api/kobo_api/search.py` (`ingest_indexes`, lines 413-416)
- Test: `api/tests/test_personas.py` (unit tests; the file already imports `json`, `pytest`, `PersonaError`, `seed_default_personas`, `FakeStorage` -- add `set_persona_archived` to its `kobo_api.personas` import), `api/tests/test_search.py` (ingest filter; reuse its `engine` fixture, `_index_bytes` helper, and `FakeStorage(buckets=...)` idiom, see its `storage` fixture at line 73).

**Interfaces:**
- Produces: `set_persona_archived(storage, *, bucket, prefix="", persona_id, archived: bool) -> dict` (returns the updated index entry; raises `PersonaError` when the id is not in the index). Archived entries carry `"archived": True`; unarchive REMOVES the key (absence is active, old index files stay valid). `ingest_indexes` drops entries where `entry.get("archived")` is truthy, for every kind.

- [ ] **Step 1: Write the failing tests**

In the file holding `seed_default_personas` tests (add beside them, reusing their `FakeStorage` seeding idiom):

```python
def test_set_persona_archived_flags_and_unflags():
    storage = FakeStorage()
    storage.write_bytes("b", "index.json", json.dumps({"entries": [
        {"id": "wire-probe", "name": "Wire Probe", "description": "d",
         "team": "shared", "tags": [], "builtin": False},
    ]}).encode())
    entry = set_persona_archived(storage, bucket="b", persona_id="wire-probe", archived=True)
    assert entry["archived"] is True
    index = json.loads(storage.read_bytes("b", "index.json"))
    assert index["entries"][0]["archived"] is True

    entry = set_persona_archived(storage, bucket="b", persona_id="wire-probe", archived=False)
    assert "archived" not in entry
    index = json.loads(storage.read_bytes("b", "index.json"))
    assert "archived" not in index["entries"][0]


def test_set_persona_archived_unknown_id_raises():
    storage = FakeStorage()
    storage.write_bytes("b", "index.json", json.dumps({"entries": []}).encode())
    with pytest.raises(PersonaError, match="no persona"):
        set_persona_archived(storage, bucket="b", persona_id="ghost", archived=True)


def test_seed_default_personas_preserves_archived_flag():
    """The resurrect trap: seeding rewrites builtin index entries wholesale
    every boot; an archived builtin must stay archived across a restart."""
    storage = FakeStorage()
    seed_default_personas(storage, bucket="b")
    set_persona_archived(storage, bucket="b", persona_id="helpful-generalist", archived=True)
    seed_default_personas(storage, bucket="b")  # the restart
    index = json.loads(storage.read_bytes("b", "index.json"))
    entry = next(e for e in index["entries"] if e["id"] == "helpful-generalist")
    assert entry.get("archived") is True
```

In `api/tests/test_search.py`, beside `test_ingest_indexes_returns_counts` (line ~132):

```python
def test_ingest_drops_archived_entries(engine):
    storage = FakeStorage(
        buckets={
            "personas-bucket": {
                "index.json": (
                    _index_bytes(
                        [
                            {"id": "active-one", "name": "Active", "description": "d",
                             "team": "shared", "tags": [], "builtin": False},
                            {"id": "shelved-one", "name": "Shelved", "description": "d",
                             "team": "shared", "tags": [], "builtin": False,
                             "archived": True},
                        ]
                    ),
                    {},
                )
            },
        }
    )
    counts = ingest_indexes(engine, storage, {"persona": "personas-bucket"})
    assert counts == {"persona": 1}
    ids = [e.id for e in catalog_search(engine, "persona", "")]
    assert ids == ["active-one"]
```

(Adapt the two imports to the file's existing import block; both `ingest_indexes` and `catalog_search` are already exercised there.)

- [ ] **Step 2: Run to verify they fail**

Run: `cd api && pytest -q tests/ -k "set_persona_archived or preserves_archived or drops_archived"`
Expected: FAIL (`set_persona_archived` not defined; ingest keeps archived rows).

- [ ] **Step 3: Implement**

In `api/kobo_api/personas.py`, after `_write_index`:

```python
def set_persona_archived(
    storage: StorageClient,
    *,
    bucket: str,
    prefix: str = "",
    persona_id: str,
    archived: bool,
) -> dict:
    """Flip one index entry's archived flag (hardening wave: archive, not
    delete -- the persona.md object is never touched, and unarchive
    restores full standing). Archived is presence-based: setting True
    writes `"archived": True`; setting False REMOVES the key, so every
    pre-archive index file stays valid and absence always means active.
    Returns the updated entry. Raises PersonaError when `persona_id` is
    not in the index (no anti-enumeration fold here: the persona catalog
    is room-readable, honesty wins)."""
    index = _read_index(storage, bucket, prefix)
    for entry in index:
        if entry["id"] == persona_id:
            if archived:
                entry["archived"] = True
            else:
                entry.pop("archived", None)
            _write_index(storage, bucket, prefix, index)
            return entry
    raise PersonaError(f"no persona with id {persona_id!r}")
```

In `seed_default_personas` (lines 404-415), preserve the flag across the wholesale rewrite:

```python
        previous = index_by_id.get(info.id)
        index_by_id[info.id] = {
            "id": info.id,
            "name": info.id,
            "description": info.description,
            "team": "shared",
            "tags": [],
            "builtin": True,
        }
        # The resurrect trap (hardening wave): this rewrite runs every
        # boot; without carrying the flag forward, an archived builtin
        # would silently return on the next restart.
        if previous and previous.get("archived"):
            index_by_id[info.id]["archived"] = True
```

In `api/kobo_api/search.py` `ingest_indexes` (lines 413-416), filter at load so rows AND counts reflect active entries only:

```python
    entries_by_kind = {
        kind: [
            e
            for e in _load_catalog_entries(storage, bucket, prefix, kind)
            # Archived catalog entries (hardening wave, personas today) are
            # invisible to every search-backed surface: the room list, the
            # picker, and the MCP list tools all read this table.
            if not e.get("archived")
        ]
        for kind, bucket in catalogs.items()
    }
```

Verify `_load_catalog_entries` tolerates the extra `archived` key (it validates required fields; if it rejects unknown keys, permit this one and pin that with a test).

- [ ] **Step 4: Run tests + full suite**

Run: `cd api && pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/kobo_api/personas.py api/kobo_api/search.py api/tests/
git diff --cached --name-only | xargs grep -l -f .superpowers/sdd/vendor-names.txt || git commit -m "feat(personas): archive flag primitives, ingest filter, seeding preserves archived"
```

---

### Task 5: Archive routes + honest archived-pick refusal

**Files:**
- Modify: `api/kobo_api/main.py` (new routes beside `create_persona_route` line 1970; create-agent persona arm lines 2274-2277; import `set_persona_archived` at line 88)
- Test: `api/tests/test_api_personas.py`, `api/tests/test_api_agents.py`

**Interfaces:**
- Consumes: `set_persona_archived` (Task 4), `_persona_team_gate` (main.py:1936), `_is_admin`, `configured_catalogs`/`ingest_indexes` (the create route's instant-ingest idiom, lines 2002-2004).
- Produces: `POST /api/personas/{persona_id}/archive`, `POST /api/personas/{persona_id}/unarchive` (200 with the updated entry; 403 refusal copy below; 404 `{"error": "no persona with that id"}`; 503 catalogs-unwired same as create), `GET /api/personas/archived` returning `{"personas": [entry, ...]}`. Task 6 and Task 7 consume these.

- [ ] **Step 1: Write the failing route tests**

Append to `api/tests/test_api_personas.py`, below the existing gate tests -- everything used here already exists in that file: the `gate_config`/`fake_storage` fixtures, `_gate_client`, `_gate_headers`, `_GroupsStub`, `_index_bytes`, and the `OPERATOR_EMAIL`/`MEMBER_EMAIL`/`STRANGER_EMAIL` constants (`gate_config` lists `OPERATOR_EMAIL` in `operators`, which is what `_is_admin` reads).

```python
ARCHIVE_ENTRY = {"id": "wire-probe", "name": "Wire Probe", "description": "a test persona",
                 "team": "shared", "tags": [], "builtin": False}
TEAM_ARCHIVE_ENTRY = {"id": "stoic-mentor", "name": "stoic-mentor", "description": "d",
                      "team": "sre", "tags": [], "builtin": False}


def _seed_personas_index(fake_storage, entries: list[dict]) -> None:
    fake_storage.write_bytes("personas-bucket", "index.json", _index_bytes(entries))


def test_archive_shared_persona_operator_200_and_instantly_evicted(
    gate_config, fake_storage, tmp_path
):
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=_GroupsStub())
    _seed_personas_index(fake_storage, [ARCHIVE_ENTRY])
    response = client.post(
        "/api/personas/wire-probe/archive", headers=_gate_headers(key, email=OPERATOR_EMAIL)
    )
    assert response.status_code == 200
    assert response.json()["archived"] is True
    search = client.get("/api/personas/search")
    assert "wire-probe" not in [e["id"] for e in search.json()["results"]]


def test_archive_shared_persona_non_operator_403(gate_config, fake_storage, tmp_path):
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=_GroupsStub())
    _seed_personas_index(fake_storage, [ARCHIVE_ENTRY])
    response = client.post(
        "/api/personas/wire-probe/archive", headers=_gate_headers(key, email=STRANGER_EMAIL)
    )
    assert response.status_code == 403
    assert "room operator" in response.json()["error"]


def test_archive_team_persona_member_200_non_member_403(gate_config, fake_storage, tmp_path):
    resolver = _GroupsStub({MEMBER_EMAIL: ["sre"]})
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=resolver)
    _seed_personas_index(fake_storage, [TEAM_ARCHIVE_ENTRY])
    denied = client.post(
        "/api/personas/stoic-mentor/archive", headers=_gate_headers(key, email=STRANGER_EMAIL)
    )
    assert denied.status_code == 403
    assert "member of team sre" in denied.json()["error"]
    allowed = client.post(
        "/api/personas/stoic-mentor/archive", headers=_gate_headers(key, email=MEMBER_EMAIL)
    )
    assert allowed.status_code == 200


def test_archive_builtin_operator_only(gate_config, fake_storage, tmp_path):
    builtin = {**ARCHIVE_ENTRY, "id": "helpful-generalist", "builtin": True}
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=_GroupsStub())
    _seed_personas_index(fake_storage, [builtin])
    denied = client.post(
        "/api/personas/helpful-generalist/archive",
        headers=_gate_headers(key, email=STRANGER_EMAIL),
    )
    assert denied.status_code == 403
    allowed = client.post(
        "/api/personas/helpful-generalist/archive",
        headers=_gate_headers(key, email=OPERATOR_EMAIL),
    )
    assert allowed.status_code == 200


def test_archive_unknown_id_404(gate_config, fake_storage, tmp_path):
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=_GroupsStub())
    _seed_personas_index(fake_storage, [])
    response = client.post(
        "/api/personas/ghost/archive", headers=_gate_headers(key, email=OPERATOR_EMAIL)
    )
    assert response.status_code == 404
    assert response.json()["error"] == "no persona with that id"


def test_unarchive_restores_and_reindexes(gate_config, fake_storage, tmp_path):
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=_GroupsStub())
    _seed_personas_index(fake_storage, [{**ARCHIVE_ENTRY, "archived": True}])
    response = client.post(
        "/api/personas/wire-probe/unarchive", headers=_gate_headers(key, email=OPERATOR_EMAIL)
    )
    assert response.status_code == 200
    assert "archived" not in response.json()
    search = client.get("/api/personas/search")
    assert "wire-probe" in [e["id"] for e in search.json()["results"]]


def test_archived_listing_route(gate_config, fake_storage, tmp_path):
    client, key = _gate_client(gate_config, fake_storage, tmp_path, groups_resolver=_GroupsStub())
    _seed_personas_index(
        fake_storage, [ARCHIVE_ENTRY, {**TEAM_ARCHIVE_ENTRY, "archived": True}]
    )
    response = client.get("/api/personas/archived")
    assert response.status_code == 200
    listed = response.json()["personas"]
    assert [e["id"] for e in listed] == ["stoic-mentor"]
    assert listed[0]["archived"] is True
```

(If the `fake_storage` fixture pre-seeds a personas index, `_seed_personas_index` overwrites it -- that is fine, these tests own their catalog state. If `/api/personas/search` needs the app's catalog engine primed, the archive route's own re-ingest primes it; the eviction assertion runs after the POST deliberately.)

- [ ] **Step 2: Run to verify they fail**

Run: `cd api && pytest -q tests/test_api_personas.py -k archive`
Expected: FAIL (404 route not found).

- [ ] **Step 3: Implement the routes**

In `api/kobo_api/main.py`, import `set_persona_archived` in the personas import (line 88). Add after `create_persona_route` (line 2026):

```python
    def _persona_archive_gate(request: Request, entry: dict) -> JSONResponse | None:
        """Who may archive/unarchive (hardening wave rulings): builtin and
        commons personas are operator-only (no ownership axis exists to
        check); a team persona takes a member of that team or an operator.
        The team arm reuses _persona_team_gate's fail-closed resolver
        logic by delegation, with archive-specific refusal copy."""
        if entry.get("builtin") or entry.get("team") == "shared":
            if _is_admin(request):
                return None
            return JSONResponse(
                {"error": "only a room operator can archive a persona filed under the commons"},
                status_code=403,
            )
        gate = _persona_team_gate(request, entry["team"])
        if gate is None:
            return None
        return JSONResponse(
            {
                "error": (
                    f"you must be a member of team {entry['team']} "
                    "to archive a persona filed under it"
                )
            },
            status_code=403,
        )

    def _set_archived_route(
        persona_id: str, request: Request, session: Session, archived: bool
    ) -> JSONResponse:
        cfg: Config = app.state.config
        clients = app.state.clients
        if clients is None or clients.storage is None or not cfg.personas_bucket:
            return JSONResponse({"error": "catalogs aren't set up yet"}, status_code=503)

        index = personas_read_index(clients.storage, cfg.personas_bucket, "")
        entry = next((e for e in index if e["id"] == persona_id), None)
        if entry is None:
            return JSONResponse({"error": "no persona with that id"}, status_code=404)

        gate = _persona_archive_gate(request, entry)
        if gate is not None:
            return gate

        try:
            updated = set_persona_archived(
                clients.storage,
                bucket=cfg.personas_bucket,
                persona_id=persona_id,
                archived=archived,
            )
            # Same instant-ingest idiom as create_persona_route: full
            # kind->bucket mapping, so the flip is visible (or invisible)
            # in search immediately.
            engine = session.get_bind()
            ingest_indexes(engine, clients.storage, configured_catalogs(cfg))
            app.state.catalog_loaded_at = time.monotonic()
        except PersonaError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except ClientError as exc:
            log.warning("persona archive flip failed: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse(updated)

    @app.post("/api/personas/{persona_id}/archive")
    def archive_persona_route(
        persona_id: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        return _set_archived_route(persona_id, request, session, archived=True)

    @app.post("/api/personas/{persona_id}/unarchive")
    def unarchive_persona_route(
        persona_id: str, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        return _set_archived_route(persona_id, request, session, archived=False)

    @app.get("/api/personas/archived")
    def archived_personas_route() -> JSONResponse:
        cfg: Config = app.state.config
        clients = app.state.clients
        if clients is None or clients.storage is None or not cfg.personas_bucket:
            return JSONResponse({"error": "catalogs aren't set up yet"}, status_code=503)
        index = personas_read_index(clients.storage, cfg.personas_bucket, "")
        return JSONResponse(
            {"personas": [e for e in index if e.get("archived")]}
        )
```

`personas_read_index` here is `kobo_api.personas._read_index` imported under a public alias -- add `read_index = _read_index` in personas.py with a one-line docstring (the route needs the raw index; keep the underscore original for in-module callers) and import it as `read_index as personas_read_index`. ROUTE ORDER: FastAPI matches `/api/personas/archived` against `/api/personas/{persona_id}/...` only if declared safely -- the GET has a distinct method+path so there is no clash, but declare `archived_personas_route` BEFORE any future `GET /api/personas/{persona_id}` anyway and leave a comment saying why.

- [ ] **Step 4: Honest archived-pick refusal in create-agent**

RED first, in `api/tests/test_api_agents.py`, directly below `test_create_agent_unknown_persona_returns_422` (line 278; that test stays untouched and keeps pinning the `unknown persona` arm):

```python
def test_create_agent_archived_persona_returns_honest_422(client, storage):
    # `storage` = this file's FakeStorage fixture (the personas-bucket
    # seeding lives around line 44; adapt the fixture name to the file's
    # actual one). Append an archived entry to the index, refresh the
    # catalog so ingest drops it, then pick it.
    index = json.loads(storage.read_bytes("personas-bucket", "index.json"))
    index["entries"].append(
        {"id": "shelved-soul", "name": "shelved-soul", "description": "d",
         "team": "shared", "tags": [], "builtin": False, "archived": True}
    )
    storage.write_bytes("personas-bucket", "index.json", json.dumps(index).encode())
    client.post("/api/catalogs/refresh")
    response = client.post(
        "/api/agents",
        json={"name": AGENT_NAME, "persona": "shelved-soul", "team": TEAM},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "persona 'shelved-soul' is archived"
```

Then in `main.py` lines 2274-2277 replace:

```python
        persona_entry = get_entry(engine, "persona", payload.persona, team=None)
        if persona_entry is None:
            return JSONResponse(
                {"error": f"unknown persona: {payload.persona!r}"}, status_code=422
            )
```

with:

```python
        persona_entry = get_entry(engine, "persona", payload.persona, team=None)
        if persona_entry is None:
            # Ingest drops archived entries, so an archived pick lands here
            # exactly like an unknown one. One storage read on this failure
            # arm buys the honest answer (the persona catalog is
            # room-readable; no anti-enumeration fold applies).
            if _persona_archived_in_index(payload.persona):
                return JSONResponse(
                    {"error": f"persona {payload.persona!r} is archived"}, status_code=422
                )
            return JSONResponse(
                {"error": f"unknown persona: {payload.persona!r}"}, status_code=422
            )
```

with the helper beside the persona routes:

```python
    def _persona_archived_in_index(persona_id: str) -> bool:
        """True only when the bucket index carries this id with the
        archived flag set. Any storage trouble answers False -- the caller
        then gives the same unknown-persona answer as before this wave."""
        cfg: Config = app.state.config
        clients = app.state.clients
        if clients is None or clients.storage is None or not cfg.personas_bucket:
            return False
        try:
            index = personas_read_index(clients.storage, cfg.personas_bucket, "")
        except Exception:  # noqa: BLE001 -- failure path must stay quiet
            return False
        entry = next((e for e in index if e["id"] == persona_id), None)
        return bool(entry and entry.get("archived"))
```

- [ ] **Step 5: Full suite + commit**

```bash
cd api && pytest -q
git add api/kobo_api/main.py api/kobo_api/personas.py api/tests/test_api_personas.py api/tests/test_api_agents.py
git diff --cached --name-only | xargs grep -l -f .superpowers/sdd/vendor-names.txt || git commit -m "feat(personas): archive/unarchive routes, archived listing, honest archived-pick refusal"
```

---

### Task 6: MCP surface -- naming law docstring + archived behavior through the shim

**Files:**
- Modify: `api/kobo_api/mcp/tools/write.py` (`kobo_create_persona` docstring; grep `kobo_create_persona` for the def)
- Test: `api/tests/test_mcp_tools_read.py`, `api/tests/test_mcp_tools_write.py`

**Interfaces:**
- Consumes: Task 5's routes and refusal copy; the existing shim test harness in both test files (synthetic `room_request`, FakeStorage-backed app -- read the files' existing persona-tool tests and reuse their fixtures exactly).

- [ ] **Step 1: RED tests through the tools**

In `api/tests/test_mcp_tools_read.py`: seed one active + one archived persona in the fake index, ingest, call `kobo_list_personas` through the existing harness, assert the archived id is absent and the active one present.

In `api/tests/test_mcp_tools_write.py`: `kobo_create_agent` picking the archived persona raises `ToolError` whose message contains `is archived` (the 422 mapping carries the route message verbatim -- this pins shim inheritance for the new arm).

Run both, expect FAIL only if Tasks 4-5 are unmerged; on a linear execution these may already PASS -- in that case flip the assertion first to prove the test discriminates (assert the archived id IS present -> must FAIL), then restore. State which path happened in the report.

- [ ] **Step 2: Naming law in the docstring**

In `kobo_create_persona`'s docstring, insert after the check-fit sentence (exact copy, no em dashes):

```
        Name the behavior, not a codename: a good persona name passes the
        glance test (a stranger reading the name plus who_they_are knows
        what picking it gets them). Prefer lowercase hyphenated trait
        words ("direct-factual-condensed-mellow"), an archetype plus a
        qualifier ("stoic-mentor"), or a figure whose mannerisms the
        persona adopts (mannerisms and tone only, never identity claims).
```

- [ ] **Step 3: Suite + commit**

```bash
cd api && pytest -q
git add api/kobo_api/mcp/tools/write.py api/tests/test_mcp_tools_read.py api/tests/test_mcp_tools_write.py
git diff --cached --name-only | xargs grep -l -f .superpowers/sdd/vendor-names.txt || git commit -m "feat(mcp): persona naming law in create_persona docstring; archived personas proven invisible through the tools"
```

---

### Task 7: UI -- Personas page, naming microcopy, soul-line display

**Files:**
- Create: `ui/src/pages/Personas.svelte`, `ui/src/pages/Personas.test.ts`
- Modify: `ui/src/lib/api.ts` (beside the personas search fetch, line ~588), `ui/src/lib/queries.ts` (beside `personasSearchQuery`), `ui/src/App.svelte` (router), `ui/src/components/Sidebar.svelte` (nav entry), `ui/src/components/PersonaBuilder.svelte` (Name field, line ~598), `ui/src/pages/AgentDetail.svelte` (persona line, ~327)
- Test: co-located `.test.ts` files per component touched (house pattern: every component has one)

**Interfaces:**
- Consumes: `GET /api/personas/search` (existing), `GET /api/personas/archived`, `POST /api/personas/{id}/archive`, `POST /api/personas/{id}/unarchive` (Task 5 shapes).
- Produces: api.ts functions `archivedPersonas()`, `archivePersona(id)`, `unarchivePersona(id)` following the file's existing fetch wrapper idiom (read the personas search fetch first and mirror it exactly, including error surfacing).

- [ ] **Step 1: api.ts + queries.ts additions (RED via api.test.ts first)**

Mirror the existing persona search test in `ui/src/lib/api.test.ts` for the three new functions (URL, method, error shape). Implement in api.ts/queries.ts following the surrounding idiom.

- [ ] **Step 2: Personas page**

New `ui/src/pages/Personas.svelte`: a catalog page listing active personas via the existing `personasSearchQuery("")`, each card rendering the DISPLAY HIERARCHY (this is the naming law made visible): persona name as the primary line, `who_they_are`/description as the grey secondary line directly beneath, team and tags as chips, builtin badge where true. Controls per card: an Archive button; a "Show archived" toggle at the top revealing an archived section (from `archivedPersonas()`) whose cards carry Unarchive instead. Server enforces the gates; the UI shows the buttons to everyone and surfaces a 403's error copy as a toast (house toast idiom, `ui/src/lib/toast.svelte.ts`). Empty states via the existing `EmptyState.svelte`. RED first in `Personas.test.ts`: renders entries from a mocked query; archive button calls the api function; 403 surfaces as toast; toggle reveals archived section.

- [ ] **Step 3: Router + nav**

Register the page in `App.svelte`'s router and add a Sidebar entry labeled `Personas` (match the existing nav item markup exactly). Extend the Sidebar test for the new entry.

- [ ] **Step 4: PersonaBuilder microcopy**

Under the Name field (label at line ~598), add helper text in the builder's existing hint style (create the style if none exists, matching the form's muted text class):

```
Name the behavior, not a codename: "direct-factual-condensed-mellow" tells a stranger what they get. Trait words, an archetype like "stoic-mentor", or a figure whose mannerisms you want all work; the name is this persona's soul at a glance.
```

Pin the copy in PersonaBuilder.test.ts (exact-substring assertion, so copy drift is deliberate).

- [ ] **Step 5: AgentDetail soul-line**

At `AgentDetail.svelte:327` the composition summary prints the persona id plain. Restyle: the agent's NAME remains the page's one real name; the persona renders as a secondary grey soul-line, `{persona-name} -- {who_they_are}` shape, sourced by querying `personasSearchQuery(data.persona)` and matching on id (fall back to the bare id when the query misses, e.g. an archived or deleted-from-catalog persona). Test: soul-line renders name + who-line when the catalog answers; falls back to the id alone when it does not.

- [ ] **Step 6: UI suite + commit**

```bash
cd ui && npx vitest run
git add ui/src
git diff --cached --name-only | xargs grep -l -f .superpowers/sdd/vendor-names.txt || git commit -m "feat(ui): personas catalog page with archive lane, naming microcopy, agent soul-line"
```

---

### Task 8: persona-authoring v1.0.1 -- the naming section

**Files:**
- Create: `<scratchpad>/persona-authoring-v1.0.1-naming-section.md` (content deliverable; NOT committed to this repo)
- The full updated SKILL.md is assembled at rollout by cloning the marketplace repo over the operator SSH lane (create_skill is create-only by design; the update lane is a registered follow-up).

**Interfaces:**
- Consumes: the published persona-authoring SKILL.md (fetched from the marketplace repo at rollout).
- Produces: the section below, inserted as a new `## Naming: the glance test` section immediately after the skill's field-overview section, and frontmatter `version` bumped `1.0.0` -> `1.0.1`.

- [ ] **Step 1: Write the section content to the scratchpad, verbatim**

```markdown
## Naming: the glance test

A persona is shareable only if a stranger can read its card and know
what picking it gets them. That is the whole law: the name plus the
one-line who_they_are must answer "what does this soul make my agent
act like" at a glance. A codename fails this test every time.

Three naming patterns pass it, in order of preference:

1. **Trait-contract names.** Lowercase hyphenated trait words that ARE
   the behavior contract: `direct-factual-condensed-mellow`,
   `patient-thorough-teacher`, `terse-skeptical-auditor`. The name
   doubles as documentation, and it slugs cleanly.
2. **Archetype names.** An archetype word plus a qualifier:
   `stoic-mentor`, `cheerful-triage-nurse`, `methodical-cartographer`.
   Archetypes carry rich expectations in a single word; the qualifier
   aims them at your domain.
3. **Figure-inspired names.** Naming a well-known figure deliberately
   invokes everything the model already knows about how they speak and
   reason, which is powerful when you want exactly those mannerisms.
   Two rules ride along: the persona adopts mannerisms and tone only,
   never identity claims (the agent must not present itself as being
   that person), and never reproduce protected expression verbatim.

Anti-patterns, refused at review even though no gate enforces them:
codenames (`probe-7`, `gemini-one`), inside jokes that need the
authoring team present to decode, and names that describe the AGENT's
job instead of the persona's manner (the agent's own name and purpose
tags carry the job; the persona carries the soul).
```

- [ ] **Step 2: Validate the copy**

Scan the section for em dashes (`grep -n "—"`, must be empty), vendor names (`grep -f .superpowers/sdd/vendor-names.txt`, must be empty). NOTE: `gemini-one` above is a room-local catalog id, not a vendor name, and it names a real anti-pattern already in the catalog; keep it.

- [ ] **Step 3: Record the rollout procedure in the task report**

At rollout (operator SSH lane): clone the marketplace repo, open `skills/persona-authoring/SKILL.md`, bump frontmatter `version: 1.0.0` to `1.0.1`, insert the section after the field-overview section, run the repo's own frontmatter validator over the corpus, commit `persona-authoring 1.0.1: naming, the glance test`, push. Bucket + room catalog confirm after publish CI + TTL, same as v1.0.0.

---

### Task 9: Docs, spec status, ledger

**Files:**
- Modify: `README.md` (the personas bullet under What-the-room-does-today)
- Modify: `docs/superpowers/specs/2026-08-24-hardening-stewardship-wave-design.md` (status flip + plan-time amendments)
- Modify: `.superpowers/sdd/progress.md` (wave record; untracked, no vendor scan needed there)

**Steps:**

- [ ] **Step 1: README personas bullet**

Extend the existing personas bullet with one sentence (no em dashes): `Personas can be archived and restored from the Personas page, and good persona names pass the glance test: a stranger reading the card knows what picking it gets them.` Keep the bullet's existing voice; do not overpromise (archive is UI-only).

- [ ] **Step 2: Spec status + amendments**

Flip the spec's Status to `Implemented (rollout pending)`. Append an `## Execution amendments` section recording the three plan-time refinements from this plan's header verbatim.

- [ ] **Step 3: Ledger + commit**

Append the wave execution record to `.superpowers/sdd/progress.md` tail (task-by-task, house style). Then:

```bash
cd api && pytest -q && cd ../ui && npx vitest run && cd ..
git add README.md docs/superpowers/specs/2026-08-24-hardening-stewardship-wave-design.md
git diff --cached --name-only | xargs grep -l -f .superpowers/sdd/vendor-names.txt || git commit -m "docs: hardening wave README bullet, spec status, execution amendments"
```

---

## Rollout (operator lane, after all tasks -- from the spec, restated)

1. Build + roll the kobo image at wave HEAD; digest-verify. No DB migration.
2. org.yaml `skills_frontmatter` block only if/when vocabulary enforcement is wanted (image first). The gate is live without it.
3. persona-authoring v1.0.1 via the SSH lane per Task 8's procedure.
4. Wire acceptance: lawful publish passes; missing-version content publishes via completion; malformed-version refuses with the field named (operator-provable, it is a validation refusal); archive wire-probe/infra-probe/ceiling-probe in the UI; a create pick of an archived persona refuses honestly; unarchive one and it returns to search; `kobo_list_personas` never shows archived; PersonaBuilder microcopy and the agent soul-line eyeballed in the browser.
