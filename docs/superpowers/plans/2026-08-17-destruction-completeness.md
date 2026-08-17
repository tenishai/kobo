# Destruction Completeness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Destruction tears down what the room's own access step built: the agent GSA, its workload-identity binding (dies with the GSA), and its model-group membership.

**Architecture:** Two check-then-act teardown verbs on `CloudClient` mirror the ensure side exactly (`gsa_email(name, project)` is the shared composition; the group value comes from `config.model_group`, never restated). A `destroy-access` step slots between `destroy-platform` and `destroy-slack` in the ledger, membership removed before GSA deletion (a deleted GSA's ghost membership is unremovable by email).

**Tech Stack:** FastAPI + SQLAlchemy API, gcloud subprocess client, existing destroy-ledger machinery.

## Global Constraints

- REAL-GATE RULE: the GSA email in teardown comes from the SAME `gsa_email(name, ...)` helper the ensure side uses with the SAME `f"agent-{agent.name}"` name composition `AccessStep` uses; the group is `config.model_group`. Never a re-derived lookalike.
- Not-found recognition grounded in the real gcloud error surface (run the failing command shapes or read the SDK error text as the platform-teardown task did); permission/quota/network failures RAISE, never map to already-gone.
- No em dashes in step reasons or any user-facing string (the API copy guard enforces main.py; destroy.py strings keep the same discipline by hand).
- No vendor names in tracked files; vendor scan is its OWN pre-commit step (`grep -i -f ../.superpowers/sdd/vendor-names.txt <files>` from api/).
- TDD RED-first. Baseline: API 1294 (`venv/bin/python -m pytest -q` from api/). No schema changes in this plan (step rows are created per-run).

## File Structure

- `api/kobo_api/clients.py` — `remove_from_group`, `delete_gsa` on the CloudClient protocol + gcloud implementation + FakeCloud knobs in tests/fakes (follow how FakeCloud models ensure_gsa/add_to_group state today).
- `api/kobo_api/destroy.py` — `destroy-access` in `DESTROY_STEPS` + `DESTROY_STEP_LABELS` ("Model access") + the step runner.
- Tests: `api/tests/test_clients.py` (verb argv/no-op/raise matrix), `api/tests/test_destroy.py` (step + resume matrix), `api/tests/test_web_destroy.py` (label rides the payload).

---

### Task 1: CloudClient teardown verbs

**Files:**
- Modify: `api/kobo_api/clients.py` (beside ensure_gsa/add_to_group, ~line 736+)
- Modify: `api/tests/fakes.py` (FakeCloud state knobs, following its existing conventions)
- Test: `api/tests/test_clients.py`

**Interfaces:**
- Consumes: `gsa_email(name, project)` (existing helper), the runner idiom, the existing not-found mapping discipline (see `_gh_returned_404` and the platform arc's `_exists` precedents for style).
- Produces (Task 2 contract): `remove_from_group(gsa_email: str, group: str) -> bool` (True removed, False already absent, raises otherwise); `delete_gsa(email: str) -> bool` (True deleted, False already gone, raises otherwise). FakeCloud gains `group_members: set[str]` and reuses its existing GSA-existence state, plus `remove_group_error`/`delete_gsa_error` raise knobs.

- [ ] **Step 1: RED tests** — in test_clients.py, following its RecordingRunner idiom:

```python
def test_remove_from_group_removes_and_returns_true(...):
    # argv pinned exactly: gcloud identity groups memberships delete
    #   --group-email <group> --member-email <email> --quiet
def test_remove_from_group_absent_membership_returns_false_without_raising(...):
    # runner raises ClientError whose stderr carries the real not-found text
    # (ground it: run `gcloud identity groups memberships delete` against a
    # nonexistent membership once, or read the gcloud SDK's error text; put
    # the REAL text in the fixture and cite the source in a comment)
def test_remove_from_group_permission_denied_raises(...):
def test_delete_gsa_deletes_and_returns_true(...):
    # argv: gcloud iam service-accounts delete <email> --project=<proj> --quiet
    # (mirror how ensure_gsa passes project — read it first)
def test_delete_gsa_absent_returns_false(...):
    # real not-found stderr text, same grounding discipline
def test_delete_gsa_permission_denied_raises(...):
```

Run: `venv/bin/python -m pytest tests/test_clients.py -q` — FAIL (methods missing).

- [ ] **Step 2: Implement both verbs** on the protocol + gcloud class, catching ONLY the grounded not-found class (returning False), re-raising everything else as the existing ClientError paths do. FakeCloud: implement per its state conventions.

- [ ] **Step 3: GREEN + full suite** (1294 + new). Ruff on touched files.

- [ ] **Step 4: Vendor scan (own step), commit**

```bash
grep -i -f ../.superpowers/sdd/vendor-names.txt kobo_api/clients.py tests/fakes.py tests/test_clients.py || echo clean
git add kobo_api/clients.py tests/fakes.py tests/test_clients.py
git commit -m "feat(api): the cloud client learns to take back what it granted"
```

---

### Task 2: The destroy-access step

**Files:**
- Modify: `api/kobo_api/destroy.py` (DESTROY_STEPS order, DESTROY_STEP_LABELS ~line 56, step runner)
- Test: `api/tests/test_destroy.py`, `api/tests/test_web_destroy.py`

**Interfaces:**
- Consumes: Task 1's verbs; `config.model_group`; the agent name; the same `f"agent-{agent.name}"` + `gsa_email` composition AccessStep uses (import/reuse, never restate).
- Produces: `DESTROY_STEPS` = platform, access, slack, repo, forget; label "Model access".

- [ ] **Step 1: RED tests** — engine matrix additions mirroring the existing per-step idioms:

```python
def test_destroy_access_removes_membership_then_gsa_in_order(...):
    # FakeCloud call order pinned: remove_from_group BEFORE delete_gsa
def test_destroy_access_absent_membership_proceeds_to_gsa(...):
def test_destroy_access_absent_gsa_is_done(...):
def test_destroy_access_failure_marks_step_and_resume_retries_access_skipping_platform(...):
    # fail access -> DESTROY_FAILED at access; resume: platform fakes get NO
    # second call, access retried, run completes to forget
```

Plus test_web_destroy.py: the destroy-steps payload now carries the "Model access" label between the platform and Slack rows (extend the existing steps-serialization test).

Also UPDATE existing tests that pin the four-step sequence (grep for DESTROY_STEPS/step-count assertions — the count is now five; adjust without weakening any original assertion).

Run: RED.

- [ ] **Step 2: Implement** — DESTROY_STEPS gains "destroy-access" after "destroy-platform"; label "Model access"; runner:

```python
def _destroy_access(agent, clients, config) -> str:
    email = gsa_email(f"agent-{agent.name}", <project source — read how
        clients.cloud carries project; the gcloud class holds self._project,
        so prefer a CloudClient accessor or compose via the client itself
        rather than importing config another way — match ensure-side reality>)
    removed = clients.cloud.remove_from_group(email, config.model_group)
    gone = clients.cloud.delete_gsa(email)
    return f"membership removed: {removed}, gsa deleted: {gone}"
```

(The email composition detail is deliberately stated as a constraint, not
final code: the implementer reads `ensure_gsa`/`gsa_email` and composes
IDENTICALLY — the plan refuses to guess the project plumbing. If
`CloudClient` lacks a clean email accessor, add `gsa_email_for(name) -> str`
to the protocol delegating to the same helper, and use it on BOTH sides so
they cannot drift.)

- [ ] **Step 3: GREEN + full suite; ruff.**

- [ ] **Step 4: Vendor scan (own step), commit, push**

```bash
grep -i -f ../.superpowers/sdd/vendor-names.txt kobo_api/destroy.py tests/test_destroy.py tests/test_web_destroy.py || echo clean
git add kobo_api/destroy.py tests/test_destroy.py tests/test_web_destroy.py
git commit -m "feat(api): destruction reaches the access step's leavings"
git push origin main
```

(Task 1's commit rides this push too.)

---

### Task 3: Cleanup + live proof (operational, controller-driven)

- [ ] Manual cleanup of the known orphan once the operator acks: remove `agent-warden@...` from the model group, delete the GSA (the exact gcloud commands the new verbs encode, run by hand since no ledger row survives).
- [ ] Rollout rides the next kobo image roll (no schema change; the self-service plan's roll carries it).
- [ ] Live proof: on the next scratch-agent destroy (acceptance B or the self-service acceptance), verify by direct inspection that `agent-<name>@` is gone and the model group no longer lists it.
