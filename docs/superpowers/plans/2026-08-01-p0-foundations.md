# P0 Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a hand-written agent-bundle repo deploy to the dev cluster through the full chain (repo → gojira onboarding → auto-merged setup PR → CI image build → running pod that seeds its techne memory and answers a hello) with no manual step after the API call.

**Architecture:** Two feature branches in sibling repos. In **daedalus** (`feat/runtime-image`): a `docker/` directory with a convergent knowledge seeder, an entrypoint, and a base image every agent image builds `FROM`. In **gojira** (`feat/agent-deploys`): the `agent` workload class gains an HTTP readiness probe (so a Service is generated), a JSON connect API (`POST/GET /api/v1/apps`), and an opt-in auto-merge of the setup PR. Nothing lands on either repo's `main`; PRs open only after the E2E runbook (Task 8) passes.

**Tech Stack:** Python 3.12, pytest, Click, FastAPI/TestClient, SQLAlchemy, Docker (with named build contexts), techne (embedded SQLite), daedalus a2a transport.

## Global Constraints

- Never write the employer's name (any casing) into any file in these repos. Env-specific values (GCP project, cluster, AR host, Google Group) appear only as `$VARS` in the runbook's config block.
- All sibling-repo work on branches: daedalus `feat/runtime-image`, gojira `feat/agent-deploys`. Never commit to their `main`.
- daedalus requires Python ≥3.11; gojira tests run with the repo's own venv (`make test` in each repo).
- TDD per task; one commit per task minimum; run each repo's full suite (`make check` in daedalus, `python -m pytest` in gojira) before its commit.
- Follow surrounding code style: gojira routes live inline in `create_app()`; daedalus `docker/` stays self-contained (daedalus's `testpaths=["tests"]` must NOT pick up `docker/` tests — they run explicitly).
- The seeder must never touch entries whose source doesn't start with `seed:` — learned memories are sacred.

---

## Part A — daedalus branch `feat/runtime-image`

Work in `../daedalus`. First step of Task 1 creates the branch.

### Task 1: Knowledge-file parser + type map (`docker/seeder.py`, pure part)

**Files:**
- Create: `docker/seeder.py`
- Create: `docker/test_seeder.py`

**Interfaces:**
- Produces: `parse_knowledge_file(path: Path) -> list[str]` (one string per `## ` section, heading included); `type_for(filename: str) -> str` (techne knowledge type); `TYPE_MAP: dict[str, str]`.
- Consumes: nothing (stdlib only — techne is NOT imported at module top level; Task 2 keeps techne behind the `main()` wiring so tests run without it).

- [ ] **Step 1: Create the branch**

```bash
cd ../daedalus
git checkout -b feat/runtime-image
```

- [ ] **Step 2: Write the failing tests**

Create `docker/test_seeder.py`:

```python
"""Tests for the runtime image's knowledge seeder. Stdlib-only — the techne
write path is injected, so these run in daedalus's dev venv without techne."""

from pathlib import Path

from seeder import TYPE_MAP, parse_knowledge_file, type_for


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def test_parse_splits_on_h2_sections(tmp_path):
    p = _write(
        tmp_path,
        "gotchas.md",
        "## First gotcha\nBody one.\n\n## Second gotcha\nBody two.\n",
    )
    sections = parse_knowledge_file(p)
    assert sections == [
        "## First gotcha\nBody one.",
        "## Second gotcha\nBody two.",
    ]


def test_parse_ignores_preamble_and_blank_sections(tmp_path):
    p = _write(tmp_path, "facts.md", "Intro prose, no heading.\n\n## Kept\nYes.\n\n## \n\n")
    assert parse_knowledge_file(p) == ["## Kept\nYes."]


def test_type_for_maps_filename_stems():
    assert type_for("gotchas.md") == "evaluation"
    assert type_for("operational-playbooks.md") == "procedure"
    assert type_for("playbooks.md") == "procedure"
    assert type_for("decisions.md") == "decision"
    assert type_for("team-directory.md") == "fact"  # default
    assert set(TYPE_MAP.values()) <= {"fact", "decision", "evaluation", "procedure"}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd ../daedalus/docker && python -m pytest test_seeder.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'seeder'`

- [ ] **Step 4: Write the minimal implementation**

Create `docker/seeder.py`:

```python
"""Convergent knowledge seeder for the daedalus runtime image.

Seeds bundle/knowledge/*.md into a techne SQLite store at boot. Convergent
per file: only files whose content hash changed are re-seeded; entries from
removed files are dropped; learned (non-seed) entries are never touched.

Section convention: knowledge files are split on `## ` headings; the filename
stem picks the techne knowledge type (see TYPE_MAP; anything else is a fact).
"""

from __future__ import annotations

import re
from pathlib import Path

TYPE_MAP = {
    "gotchas": "evaluation",
    "playbooks": "procedure",
    "operational-playbooks": "procedure",
    "decisions": "decision",
}

_H2 = re.compile(r"^## ", re.MULTILINE)


def type_for(filename: str) -> str:
    return TYPE_MAP.get(Path(filename).stem, "fact")


def parse_knowledge_file(path: Path) -> list[str]:
    text = path.read_text()
    starts = [m.start() for m in _H2.finditer(text)]
    if not starts:
        return []
    sections = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        section = text[start:end].strip()
        if section.removeprefix("##").strip():
            sections.append(section)
    return sections
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ../daedalus/docker && python -m pytest test_seeder.py -v`
Expected: 3 passed

- [ ] **Step 6: Verify daedalus's own suite doesn't collect docker/**

Run: `cd ../daedalus && make check`
Expected: same pass count as before this task (pyproject `testpaths = ["tests"]` excludes `docker/`). Ruff must pass on the new files; if `make check` lints only `src`/`tests`, run `ruff check docker/` explicitly.

- [ ] **Step 7: Commit**

```bash
cd ../daedalus
git add docker/seeder.py docker/test_seeder.py
git commit -m "feat(docker): knowledge-file parser + type map for runtime seeder"
```

### Task 2: Convergent sync engine (`docker/seeder.py`, state + injection)

**Files:**
- Modify: `docker/seeder.py`
- Modify: `docker/test_seeder.py`

**Interfaces:**
- Produces:
  - `sync(knowledge_dir: Path, techne_db: Path, state_db: Path, store_fn, delete_fn) -> dict` returning `{"seeded": int, "removed": int, "unchanged": int}`.
  - `store_fn(db_path: Path, content: str, type: str, tags: str, source: str) -> int` (matches a partial of `techne.knowledge.store`, which is `store(db_path, content, type="fact", tags="", confidence="medium", source="session", anchor=None) -> int`).
  - `delete_fn(db_path: Path, entry_id: int) -> bool` (matches `techne.knowledge.delete`).
  - `main(argv) -> int` — CLI: `python seeder.py <bundle_dir>`; techne home from `$TECHNE_HOME` (default `/techne`); calls `techne.core.database.init_db` then `sync` with `techne.knowledge.store`/`delete`. Missing `knowledge/` dir → exit 0 with a log line.
- Consumes: Task 1's `parse_knowledge_file`, `type_for`.

- [ ] **Step 1: Write the failing tests**

Append to `docker/test_seeder.py`:

```python
import json
import sqlite3

from seeder import sync


class FakeStore:
    """Records store/delete calls the way techne.knowledge would see them."""

    def __init__(self):
        self.next_id = 1
        self.rows = {}  # id -> (content, type, source)

    def store(self, db_path, content, type, tags, source):
        i = self.next_id
        self.next_id += 1
        self.rows[i] = (content, type, source)
        return i

    def delete(self, db_path, entry_id):
        return self.rows.pop(entry_id, None) is not None


def _bundle(tmp_path):
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "gotchas.md").write_text("## A\nalpha\n\n## B\nbeta\n")
    (kdir / "facts.md").write_text("## C\ngamma\n")
    return kdir


def test_first_sync_seeds_everything(tmp_path):
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    result = sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)
    assert result == {"seeded": 3, "removed": 0, "unchanged": 0}
    types = sorted(t for (_, t, _) in fake.rows.values())
    assert types == ["evaluation", "evaluation", "fact"]
    assert all(src in ("seed:gotchas.md", "seed:facts.md")
               for (_, _, src) in fake.rows.values())


def test_unchanged_file_is_skipped(tmp_path):
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)
    before = dict(fake.rows)
    result = sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)
    assert result["seeded"] == 0 and result["unchanged"] == 2
    assert fake.rows == before


def test_changed_file_replaces_only_its_entries(tmp_path):
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)
    facts_ids = {i for i, (_, _, s) in fake.rows.items() if s == "seed:facts.md"}
    (kdir / "gotchas.md").write_text("## A2\nalpha revised\n")
    result = sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)
    assert result["seeded"] == 1
    assert facts_ids <= set(fake.rows)  # facts.md untouched
    assert all(s != "seed:gotchas.md" or c.startswith("## A2")
               for c, _, s in fake.rows.values())


def test_removed_file_drops_its_entries(tmp_path):
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)
    (kdir / "facts.md").unlink()
    result = sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)
    assert result["removed"] == 1
    assert all(s != "seed:facts.md" for (_, _, s) in fake.rows.values())


def test_state_survives_in_sqlite(tmp_path):
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)
    conn = sqlite3.connect(state)
    rows = conn.execute("SELECT filename, entry_ids FROM seed_state ORDER BY filename").fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["facts.md", "gotchas.md"]
    assert all(isinstance(json.loads(r[1]), list) for r in rows)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ../daedalus/docker && python -m pytest test_seeder.py -v`
Expected: new tests FAIL with `ImportError: cannot import name 'sync'`

- [ ] **Step 3: Implement sync + main**

Append to `docker/seeder.py`:

```python
import hashlib
import json
import logging
import os
import sqlite3
import sys

log = logging.getLogger("seeder")


def _state_conn(state_db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(state_db)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS seed_state (
               filename TEXT PRIMARY KEY,
               sha256 TEXT NOT NULL,
               entry_ids TEXT NOT NULL
           )"""
    )
    return conn


def sync(knowledge_dir: Path, techne_db: Path, state_db: Path, store_fn, delete_fn) -> dict:
    conn = _state_conn(state_db)
    try:
        state = {
            row[0]: {"sha256": row[1], "entry_ids": json.loads(row[2])}
            for row in conn.execute("SELECT filename, sha256, entry_ids FROM seed_state")
        }
        current = {p.name: p for p in sorted(knowledge_dir.glob("*.md"))}
        seeded = removed = unchanged = 0

        for filename in set(state) - set(current):
            for entry_id in state[filename]["entry_ids"]:
                delete_fn(techne_db, entry_id)
            conn.execute("DELETE FROM seed_state WHERE filename = ?", (filename,))
            removed += 1

        for filename, path in current.items():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            prior = state.get(filename)
            if prior and prior["sha256"] == digest:
                unchanged += 1
                continue
            if prior:
                for entry_id in prior["entry_ids"]:
                    delete_fn(techne_db, entry_id)
            ids = [
                store_fn(techne_db, section, type_for(filename), "seed", f"seed:{filename}")
                for section in parse_knowledge_file(path)
            ]
            conn.execute(
                "INSERT OR REPLACE INTO seed_state (filename, sha256, entry_ids) VALUES (?, ?, ?)",
                (filename, digest, json.dumps(ids)),
            )
            seeded += len(ids)
        conn.commit()
        return {"seeded": seeded, "removed": removed, "unchanged": unchanged}
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="seeder: %(message)s")
    if len(argv) != 2:
        log.error("usage: seeder.py <bundle_dir>")
        return 2
    knowledge_dir = Path(argv[1]) / "knowledge"
    if not knowledge_dir.is_dir():
        log.info("no knowledge/ in bundle — nothing to seed")
        return 0
    home = Path(os.environ.get("TECHNE_HOME", "/techne"))
    home.mkdir(parents=True, exist_ok=True)
    techne_db = home / "techne.db"

    from techne.core import knowledge
    from techne.core.database import init_db

    init_db(techne_db).close()

    def store_fn(db_path, content, type, tags, source):
        return knowledge.store(
            db_path, content, type=type, tags=tags, confidence="high", source=source
        )

    result = sync(knowledge_dir, techne_db, home / "seed_state.db", store_fn, knowledge.delete)
    log.info("seeded=%(seeded)s removed=%(removed)s unchanged=%(unchanged)s", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ../daedalus/docker && python -m pytest test_seeder.py -v`
Expected: 8 passed

- [ ] **Step 5: Run daedalus's suite + lint, commit**

```bash
cd ../daedalus
make check && ruff check docker/
git add docker/seeder.py docker/test_seeder.py
git commit -m "feat(docker): convergent seed sync with sqlite state + techne wiring"
```

### Task 3: Runtime image — Dockerfile, entrypoint, Makefile targets

**Files:**
- Create: `docker/Dockerfile`
- Create: `docker/entrypoint.sh`
- Modify: `Makefile` (append targets)

**Interfaces:**
- Produces: an image whose entrypoint is `entrypoint.sh <bundle_dir>` (default `/bundle`): runs the seeder, then `exec daedalus run <bundle_dir> -v`. Env: `TECHNE_HOME=/techne`. Port 8080 exposed (a2a). Per-agent images are `FROM` this one.
- Consumes: `docker/seeder.py` (Tasks 1–2); a sibling checkout of techne at `../techne` supplied as a named build context.

- [ ] **Step 1: Write `docker/entrypoint.sh`**

```bash
#!/bin/sh
# Runtime entrypoint: seed bundle knowledge into techne, then run the agent.
# Seeding failure is fatal — a silently unseeded agent is worse than a restart.
set -eu

BUNDLE="${1:-/bundle}"

python /usr/local/bin/seeder.py "$BUNDLE"
exec daedalus run "$BUNDLE" -v
```

- [ ] **Step 2: Write `docker/Dockerfile`**

```dockerfile
# daedalus runtime base image. Agent images layer on top:
#   FROM <ar>/daedalus-runtime:<tag>
#   COPY bundle/ /bundle
#
# techne is not on PyPI; it is supplied as a named build context:
#   docker build -f docker/Dockerfile --build-context techne=../techne -t daedalus-runtime .
FROM python:3.12-slim

RUN useradd --uid 10001 --create-home agent

COPY --from=techne / /opt/techne
RUN pip install --no-cache-dir /opt/techne && rm -rf /opt/techne

COPY . /opt/daedalus
RUN pip install --no-cache-dir "/opt/daedalus[a2a]" && rm -rf /opt/daedalus

COPY docker/seeder.py /usr/local/bin/seeder.py
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV TECHNE_HOME=/techne
RUN mkdir -p /techne && chown agent:agent /techne
VOLUME /techne

USER agent
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/bundle"]
```

Also create `.dockerignore` at the daedalus repo root if absent, containing:

```
.git
.venv
__pycache__
*.egg-info
```

- [ ] **Step 3: Append Makefile targets**

Append to `../daedalus/Makefile` (match its existing tab-indented style):

```make
# --- runtime image (docker/) -------------------------------------------------
IMAGE ?= daedalus-runtime
TAG ?= $(shell git rev-parse --short HEAD)
AR_IMAGE ?=  # e.g. <location>-docker.pkg.dev/<project>/<repo>/daedalus-runtime

image:
	docker build -f docker/Dockerfile --build-context techne=../techne -t $(IMAGE):$(TAG) -t $(IMAGE):latest .

image-smoke: image
	docker run --rm --entrypoint daedalus $(IMAGE):$(TAG) version
	docker run --rm --entrypoint python $(IMAGE):$(TAG) -c "import techne; print('techne ok')"

image-push: image
	test -n "$(AR_IMAGE)" || (echo "set AR_IMAGE=<ar-path>/daedalus-runtime" && exit 1)
	docker tag $(IMAGE):$(TAG) $(AR_IMAGE):$(TAG)
	docker push $(AR_IMAGE):$(TAG)
```

- [ ] **Step 4: Build and smoke-test**

Run: `cd ../daedalus && make image-smoke`
Expected: prints the daedalus version (e.g. `0.4.0.dev0`) and `techne ok`. If the build fails on the techne context, verify `../techne` is a clean checkout.

- [ ] **Step 5: Seeder-in-container smoke**

```bash
cd ../daedalus
mkdir -p /tmp/p0-bundle/knowledge
printf '## Rule one\nBe helpful.\n' > /tmp/p0-bundle/knowledge/gotchas.md
docker run --rm -v /tmp/p0-bundle:/bundle --entrypoint python daedalus-runtime:latest \
  /usr/local/bin/seeder.py /bundle
```

Expected output includes: `seeder: seeded=1 removed=0 unchanged=0`

- [ ] **Step 6: Commit**

```bash
cd ../daedalus
git add docker/Dockerfile docker/entrypoint.sh Makefile .dockerignore
git commit -m "feat(docker): daedalus-runtime base image (seed-then-run entrypoint)"
```

---

## Part B — gojira branch `feat/agent-deploys`

Work in `../gojira`. First step of Task 4 creates the branch.

### Task 4: `agent` class gets an HTTP readiness probe → Service is generated

**Files:**
- Modify: `gojira/opinions/sizing.yaml:39-40`
- Test: `tests/test_opinions.py`, `tests/test_generator.py`

Background: `gojira/generator/__init__.py:471-474` — `_has_service(cls)` is true only when the class has a `readiness` probe of type `http`. The daedalus a2a transport serves `GET /healthz` (its defaults are host 127.0.0.1, port 9200 — bundles deployed in-cluster MUST declare `host: 0.0.0.0, port: 8080` so the probe and Service can reach it; 8080 equals gojira's `APP_PORT`, `generator/__init__.py:27`). The deployment template's probes target the named container port `http`. Adding an HTTP readiness probe therefore yields a generated `service.yaml` (port 80 → targetPort `http`) with no template changes.

- [ ] **Step 1: Create the branch**

```bash
cd ../gojira
git checkout -b feat/agent-deploys
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_opinions.py`:

```python
def test_agent_class_has_http_readiness_on_healthz():
    cls = load_sizing().classes["agent"]
    readiness = cls.probes["readiness"]
    assert readiness.type == "http"
    assert readiness.path == "/healthz"
    assert cls.probes["liveness"].type == "tcp"
    assert cls.strategy == "Recreate"
```

(Match the file's existing import of `load_sizing`; if it imports differently, follow that file's pattern.)

Append to `tests/test_generator.py`, following its existing helper for building a `GeneratorSpec` (reuse the file's existing spec-construction helper/fixture; only the class name differs):

```python
def test_agent_class_renders_service():
    spec = _spec(workload_class="agent")  # use this file's existing spec helper
    files = render(spec)
    assert "k8s/base/service.yaml" in files
    assert "readinessProbe" in files["k8s/base/deployment.yaml"]
    assert "/healthz" in files["k8s/base/deployment.yaml"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd ../gojira && python -m pytest tests/test_opinions.py tests/test_generator.py -v -k agent`
Expected: both new tests FAIL (`KeyError: 'readiness'` / missing `service.yaml`)

- [ ] **Step 4: Edit the opinion**

In `gojira/opinions/sizing.yaml`, change the agent class probes block from:

```yaml
    probes:
      liveness: { type: tcp }
```

to:

```yaml
    probes:
      # a2a transport serves /healthz on APP_PORT; an http readiness probe is
      # also what makes the generator emit a Service for this class.
      readiness: { type: http, path: /healthz }
      liveness: { type: tcp }
```

- [ ] **Step 5: Run the full gojira suite**

Run: `cd ../gojira && python -m pytest -q`
Expected: all pass. The golden fixtures (`tests/golden/{web,worker,cron,multiproject}`) don't cover the agent class, so no golden churn; if any golden test fails, stop and inspect — that means the agent class leaked into a shared rendering path.

- [ ] **Step 6: Commit**

```bash
git add gojira/opinions/sizing.yaml tests/test_opinions.py tests/test_generator.py
git commit -m "feat(opinions): agent class readiness probe on /healthz -> Service generated"
```

### Task 5: JSON connect API — `POST /api/v1/apps` + `GET /api/v1/apps/{id}`

**Files:**
- Modify: `gojira/web/__init__.py` (add routes next to `POST /api/v1/usage`, `web/__init__.py:293`)
- Modify: `gojira/models.py:60-86` (App: add `auto_merge_setup_pr` column — used by Task 6, added here so the API can accept it)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `service.connect_app(session, *, name, repo_full_name, workload_class, env_map, installation_id)` (`service.py:120`), `service.start_provision(session_factory, app_id, clients, config)` (`service.py:171`), `service.app_by_repo`, `service.repo_has_dockerfile`.
- Produces: `POST /api/v1/apps` accepting
  ```json
  {"name": "clio", "repo_full_name": "acme/agent-clio", "workload_class": "agent",
   "environments": [{"name": "dev", "cluster": "dev", "namespace": "clio"}],
   "auto_merge_setup_pr": true}
  ```
  → `201 {"id": 1, "status": "pending"}`; errors: 409 already connected, 404 unknown cluster, 422 invalid body/class/no Dockerfile.
  `GET /api/v1/apps/{app_id}` → `200 {"id", "name", "status", "setup_pr_url", "auto_merge_setup_pr", "steps": [{"name", "status", "error"}]}` (steps from the `ProvisionStep` ledger: `step_name`, `status`, `error` — `models.py`), 404 unknown id. Cortex's P1 provisioner polls this.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web.py` (reuse its `session_factory`, `clients`, `client` fixtures and `_register_three` helper):

```python
def _api_body(**overrides):
    body = {
        "name": "clio",
        "repo_full_name": "acme/agent-clio",
        "workload_class": "agent",
        "environments": [{"name": "dev", "cluster": "dev", "namespace": "clio"}],
        "auto_merge_setup_pr": False,
    }
    body.update(overrides)
    return body


def test_api_connect_creates_app_and_provisions(client, session_factory, clients):
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body())
    assert resp.status_code == 201
    app_id = resp.json()["id"]

    deadline = time.time() + 5
    while time.time() < deadline:
        status = client.get(f"/api/v1/apps/{app_id}").json()
        if status["status"] in ("awaiting_pr", "active", "failed"):
            break
        time.sleep(0.05)
    assert status["status"] == "awaiting_pr"
    assert status["setup_pr_url"] == "https://github.com/acme/agent-clio/pull/1"
    assert {"name", "status", "error"} <= set(status["steps"][0])


def test_api_connect_unknown_cluster_404(client, session_factory):
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body(
        environments=[{"name": "dev", "cluster": "nope", "namespace": "x"}]))
    assert resp.status_code == 404


def test_api_connect_duplicate_repo_409(client, session_factory):
    _register_three(session_factory)
    assert client.post("/api/v1/apps", json=_api_body()).status_code == 201
    assert client.post("/api/v1/apps", json=_api_body(name="other")).status_code == 409


def test_api_connect_bad_class_422(client, session_factory):
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body(workload_class="mainframe"))
    assert resp.status_code == 422


def test_api_status_unknown_id_404(client):
    assert client.get("/api/v1/apps/999").status_code == 404
```

Note: `FakeGitHubClient.file_exists` returns its `dockerfile` flag for `"Dockerfile"` (`tests/fakes.py:169`), so the Dockerfile precheck passes by default.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ../gojira && python -m pytest tests/test_web.py -v -k api_`
Expected: FAIL — 404s from the missing routes.

- [ ] **Step 3: Add the App column**

In `gojira/models.py`, in `class App` directly under the `setup_pr_url` column (`models.py:76`), add:

```python
    # Caller-created repos (cortex) opt in to auto-merging the setup PR — the
    # one-click deploy contract. Human-connected repos keep the review gate.
    auto_merge_setup_pr: Mapped[bool] = mapped_column(default=False, server_default="0")
```

(`Base.metadata.create_all` covers fresh DBs; existing dev DBs need `ALTER TABLE apps ADD COLUMN auto_merge_setup_pr BOOLEAN DEFAULT 0` — note this in the eventual PR body.)

- [ ] **Step 4: Add the routes**

In `gojira/web/__init__.py`, add module-level imports near the existing pydantic-adjacent imports: `from pydantic import BaseModel, Field` (check the file head first — extend the existing import if pydantic is already imported), plus `from gojira.models import App, Cluster` if not already imported.

Directly above the `@app.post("/api/v1/usage")` route (`web/__init__.py:293`), insert:

```python
    class EnvIn(BaseModel):
        name: str
        cluster: str
        namespace: str = ""

    class ConnectIn(BaseModel):
        name: str = Field(min_length=1, max_length=63)
        repo_full_name: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
        workload_class: str
        environments: list[EnvIn] = Field(min_length=1)
        auto_merge_setup_pr: bool = False

    @app.post("/api/v1/apps")
    def api_connect(body: ConnectIn, request: Request, session: Session = Depends(get_session)):
        from gojira.opinions import WORKLOAD_CLASSES

        if body.workload_class not in WORKLOAD_CLASSES:
            return JSONResponse(
                {"error": f"unknown workload class {body.workload_class!r}"}, status_code=422
            )
        if service.app_by_repo(session, body.repo_full_name):
            return JSONResponse({"error": "repo already connected"}, status_code=409)
        if not service.repo_has_dockerfile(request.app.state.clients.github, body.repo_full_name):
            return JSONResponse({"error": "repo has no Dockerfile at its root"}, status_code=422)
        env_map: dict[str, tuple[int, str]] = {}
        for env in body.environments:
            cluster = session.query(Cluster).filter_by(name=env.cluster).one_or_none()
            if cluster is None:
                return JSONResponse(
                    {"error": f"unknown cluster {env.cluster!r}"}, status_code=404
                )
            env_map[env.name] = (cluster.id, env.namespace or body.name)
        try:
            installation_id = request.app.state.clients.github.installation_id_for(
                body.repo_full_name
            )
        except Exception:
            installation_id = 0
        app_obj = service.connect_app(
            session,
            name=body.name,
            repo_full_name=body.repo_full_name,
            workload_class=body.workload_class,
            env_map=env_map,
            installation_id=installation_id,
        )
        app_obj.auto_merge_setup_pr = body.auto_merge_setup_pr
        session.commit()
        service.start_provision(
            request.app.state.session_factory,
            app_obj.id,
            clients=request.app.state.clients,
            config=request.app.state.config,
        )
        return JSONResponse({"id": app_obj.id, "status": app_obj.status}, status_code=201)

    @app.get("/api/v1/apps/{app_id}")
    def api_app_status(app_id: int, session: Session = Depends(get_session)):
        app_obj = session.get(App, app_id)
        if app_obj is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(
            {
                "id": app_obj.id,
                "name": app_obj.name,
                "status": app_obj.status,
                "setup_pr_url": app_obj.setup_pr_url,
                "auto_merge_setup_pr": app_obj.auto_merge_setup_pr,
                "steps": [
                    {"name": s.step_name, "status": s.status, "error": s.error}
                    for s in app_obj.steps
                ],
            }
        )
```

(Adjust import placement/names to the file's actual head — e.g. `Cluster`/`App` may already be imported; do not duplicate.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ../gojira && python -m pytest tests/test_web.py -v -k api_`
Expected: 5 passed. Then the full suite: `python -m pytest -q` — all pass.

- [ ] **Step 6: Commit**

```bash
git add gojira/web/__init__.py gojira/models.py tests/test_web.py
git commit -m "feat(api): JSON connect + status endpoints for programmatic onboarding"
```

### Task 6: Auto-merge the setup PR (`MergeSetupPr` step + `merge_pr` client method)

**Files:**
- Modify: `gojira/github.py` (ABC at `github.py:43`, `GhCLI` at `github.py:146`, `GitHubAppClient`)
- Modify: `gojira/steps.py` (new step class after `OpenSetupPr` at `steps.py:504`; `per_app_steps()` at `steps.py:664`)
- Modify: `tests/fakes.py:141` (FakeGitHubClient)
- Test: `tests/test_steps.py`, `tests/test_web.py`

**Interfaces:**
- Produces: `GitHubClient.merge_pr(repo: str, pr_url: str) -> None` (squash-merge, delete branch); `MergeSetupPr` step (name `merge_setup_pr`) appended to `per_app_steps()`; on success sets `ctx.app.status = AppStatus.ACTIVE`.
- Consumes: `ctx.app.auto_merge_setup_pr` (Task 5), `ctx.app.setup_pr_url` set by `OpenSetupPr` (`steps.py:562`), `_github(ctx)` (`steps.py:128`), step engine semantics: `check() -> str | None` where a string means "skip with this detail" and `None` means "run apply()".

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_steps.py` (reuse that file's existing `ProvisionContext` construction helper/fixtures — follow how existing `OpenSetupPr` tests build `ctx`):

```python
def test_merge_setup_pr_skips_when_not_requested(ctx_factory):
    ctx = ctx_factory()
    ctx.app.auto_merge_setup_pr = False
    ctx.app.setup_pr_url = "https://github.com/acme/agent-clio/pull/1"
    assert MergeSetupPr().check(ctx) == "auto-merge not requested"


def test_merge_setup_pr_skips_when_no_pr(ctx_factory):
    ctx = ctx_factory()
    ctx.app.auto_merge_setup_pr = True
    ctx.app.setup_pr_url = None
    assert MergeSetupPr().check(ctx) == "no setup PR to merge"


def test_merge_setup_pr_merges_and_activates(ctx_factory):
    ctx = ctx_factory()
    ctx.app.auto_merge_setup_pr = True
    ctx.app.setup_pr_url = "https://github.com/acme/agent-clio/pull/1"
    result = MergeSetupPr().apply(ctx)
    github = ctx.clients.github  # adjust to how ctx exposes the client in this file
    assert github.merged == ["https://github.com/acme/agent-clio/pull/1"]
    assert ctx.app.status == AppStatus.ACTIVE
    assert "merged" in result
```

And an end-to-end assertion in `tests/test_web.py`:

```python
def test_api_connect_auto_merge_activates(client, session_factory, clients):
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body(auto_merge_setup_pr=True))
    app_id = resp.json()["id"]
    deadline = time.time() + 5
    while time.time() < deadline:
        status = client.get(f"/api/v1/apps/{app_id}").json()
        if status["status"] in ("active", "failed"):
            break
        time.sleep(0.05)
    assert status["status"] == "active"
    assert any(s["name"] == "merge_setup_pr" for s in status["steps"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ../gojira && python -m pytest tests/test_steps.py tests/test_web.py -v -k merge`
Expected: FAIL — `NameError: MergeSetupPr` / fake lacks `merge_pr`.

- [ ] **Step 3: Add `merge_pr` to the client layer**

In `gojira/github.py`, add to the `GitHubClient` ABC (after `open_pr`, `github.py:103`):

```python
    @abstractmethod
    def merge_pr(self, repo: str, pr_url: str) -> None:
        """Squash-merge the PR and delete its branch."""
```

In `GhCLI` (after its `open_pr`):

```python
    def merge_pr(self, repo: str, pr_url: str) -> None:
        self._exec([self._gh, "pr", "merge", pr_url, "--repo", repo,
                    "--squash", "--delete-branch"])
```

In `GitHubAppClient`, following its existing httpx REST call style (find a sibling REST method in the class and mirror its request/error handling exactly):

```python
    def merge_pr(self, repo: str, pr_url: str) -> None:
        number = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
        self._request(  # use this class's actual request helper name
            "PUT", f"/repos/{repo}/pulls/{number}/merge",
            json={"merge_method": "squash"},
        )
```

In `tests/fakes.py` `FakeGitHubClient`, add `self.merged: list[str] = []` to `__init__` and:

```python
    def merge_pr(self, repo, pr_url):
        self.calls.append(f"merge_pr:{pr_url}")
        self.merged.append(pr_url)
        self.pr_merged = True
```

- [ ] **Step 4: Add the step**

In `gojira/steps.py`, after `class OpenSetupPr` (ends `steps.py:563`):

```python
class MergeSetupPr:
    """Auto-merge the setup PR when the app opted in (caller-created repos).

    The one-click contract: a programmatic caller that authored the repo has
    no human content to protect, so the review gate is theirs to waive. Like
    OpenSetupPr, re-decides against the real world each run.
    """

    name = "merge_setup_pr"
    ephemeral = True

    def check(self, ctx: ProvisionContext) -> str | None:
        if not ctx.app.auto_merge_setup_pr:
            return "auto-merge not requested"
        if not ctx.app.setup_pr_url:
            return "no setup PR to merge"
        return None

    def apply(self, ctx: ProvisionContext) -> str:
        gh = _github(ctx)
        url = ctx.app.setup_pr_url
        gh.merge_pr(ctx.app.repo_full_name, url)
        ctx.app.status = AppStatus.ACTIVE
        return f"merged {url}"
```

(Import `AppStatus` at the top of `steps.py` if not already imported.) Then change `per_app_steps()` (`steps.py:664`):

```python
def per_app_steps() -> list[Step]:
    """The repo flow: clone, render artifacts, open the setup PR, optionally merge it."""
    return [PrepareWorkdir(), GenerateArtifacts(), OpenSetupPr(), MergeSetupPr()]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ../gojira && python -m pytest -q`
Expected: all pass, including the two prior `awaiting_pr` API tests (flag defaults False → step SKIPPED). If an existing provisioner test asserts the exact per-app step list, update it to include `merge_setup_pr`.

- [ ] **Step 6: Commit**

```bash
git add gojira/github.py gojira/steps.py tests/fakes.py tests/test_steps.py tests/test_web.py
git commit -m "feat(provisioner): opt-in auto-merge of the setup PR (MergeSetupPr)"
```

---

## Part C — E2E milestone

### Task 7: Hand-written bundle repo (the P0 exemplar)

**Files:**
- Create (new GitHub repo, e.g. `tenishai/agent-hello`): `bundle/agent.yaml`, `bundle/agent-card.json`, `bundle/persona.md`, `bundle/knowledge/gotchas.md`, `Dockerfile`

**Interfaces:**
- Consumes: the pushed runtime image tag (Task 3), daedalus bundle schema.
- Produces: the repo the Task 8 runbook onboards.

- [ ] **Step 1: Author the bundle**

`bundle/agent.yaml`:

```yaml
name: hello
card: agent-card.json
persona: persona.md
model:
  id: ${AGENT_MODEL:-vertex_ai/claude-sonnet-4-5}
  max_tokens: 1024
  budget: { per_task_tokens: 50000 }
transports:
  # host is required: the a2a transport defaults to 127.0.0.1 (and port 9200),
  # which kubelet probes and the Service cannot reach inside a pod.
  - { kind: a2a, host: 0.0.0.0, port: 8080 }
tools: { provider: none, allow: [] }
memory: { personal: techne, home: /techne }
checkpoints: sqlite
approvals:
  ceiling: { write: false }
  gates: [{ match: "*", require: plan_approval }]
  escalation: { route: cli }
```

`bundle/agent-card.json`:

```json
{
  "name": "hello",
  "description": "P0 smoke agent — proves the assembly chain end to end.",
  "version": "0.1.0",
  "capabilities": [],
  "skills": []
}
```

`bundle/persona.md`:

```markdown
You are hello, a smoke-test agent. Answer briefly and cheerfully. If asked
what you know, mention anything relevant from your memory.
```

`bundle/knowledge/gotchas.md`:

```markdown
## Deployment chain
You were deployed through the P0 assembly chain: repo, onboarding, build,
deploy, first contact. If someone asks how you got here, tell them that.
```

`Dockerfile` (RUNTIME_TAG is pinned by whoever creates the repo — the Task 8 config block):

```dockerfile
ARG RUNTIME_IMAGE
FROM ${RUNTIME_IMAGE}
COPY bundle/ /bundle
```

Note: gojira's generated CI builds with plain `docker build .`, so bake the resolved image ref instead of an ARG if the generated workflow passes no build args — i.e. write `FROM <ar>/daedalus-runtime:<tag>` literally. Check the generated `gojira-ci.yaml` after onboarding; literal-`FROM` is the safe default.

- [ ] **Step 2: Validate the bundle locally**

```bash
cd ../daedalus
python - <<'EOF'
from daedalus.bundle import Bundle
b = Bundle.load("/path/to/agent-hello/bundle")
print("bundle ok:", b.spec.name)
EOF
```

Expected: `bundle ok: hello`

- [ ] **Step 3: Create the repo and push**

```bash
gh repo create tenishai/agent-hello --private --source /path/to/agent-hello --push
```

### Task 8: E2E runbook — laptop gojira → dev cluster

**Files:**
- Create: `docs/superpowers/plans/p0-e2e-runbook.md` in the cortex repo (this checklist, with the config block filled in), updated with actual results.

Config block (fill in once, locally — values never committed anywhere except this private runbook):

```bash
export GCP_PROJECT=...        # dev project
export CLUSTER=...            # existing GKE cluster name
export LOCATION=...           # cluster location
export AR_IMAGE=...           # <location>-docker.pkg.dev/$GCP_PROJECT/<repo>/daedalus-runtime
export MODEL_GROUP=...        # Google Group with Vertex IAM
export VERTEX_PROJECT=...     # shared model-access project
```

- [ ] **Step 1: Push the runtime image**: `cd daedalus && make image-push AR_IMAGE=$AR_IMAGE`, then set the literal `FROM` in agent-hello's Dockerfile to `$AR_IMAGE:<tag>` and push.
- [ ] **Step 2: Manual model access** (P1 automates this — doing it by hand validates the design):

```bash
gcloud iam service-accounts create agent-hello --project "$GCP_PROJECT"
# Bind KSA <-> GSA (namespace "hello", KSA name from gojira.yaml service_account):
gcloud iam service-accounts add-iam-policy-binding \
  "agent-hello@$GCP_PROJECT.iam.gserviceaccount.com" \
  --role roles/iam.workloadIdentityUser \
  --member "serviceAccount:$GCP_PROJECT.svc.id.goog[hello/agent-hello]"
# Add the GSA to the model-access group (Cloud Identity; needs group-manager rights):
gcloud identity groups memberships add --group-email "$MODEL_GROUP" \
  --member-email "agent-hello@$GCP_PROJECT.iam.gserviceaccount.com"
```

Then add to the agent-hello repo: `k8s/base/serviceaccount.yaml` (KSA `agent-hello`, annotation `iam.gke.io/gcp-service-account: agent-hello@$GCP_PROJECT.iam.gserviceaccount.com`) and in `gojira.yaml` after onboarding: `service_account: agent-hello` and `env: {VERTEXAI_PROJECT: $VERTEX_PROJECT, VERTEXAI_LOCATION: $LOCATION}` — gojira preserves both.
- [ ] **Step 3: Run gojira from the branch**: `cd gojira && python -m gojira.web` (laptop mode, gcloud/kubectl creds active); register the cluster via `/clusters`.
- [ ] **Step 4: One API call**:

```bash
curl -s -X POST localhost:8000/api/v1/apps -H 'content-type: application/json' -d '{
  "name": "hello", "repo_full_name": "tenishai/agent-hello", "workload_class": "agent",
  "environments": [{"name": "dev", "cluster": "'$CLUSTER'", "namespace": "hello"}],
  "auto_merge_setup_pr": true}'
```

- [ ] **Step 5: Watch**: poll `GET /api/v1/apps/1` until `active`; watch the repo's Actions build; `kubectl -n hello get pods,svc` until Ready.
- [ ] **Step 6: First contact**:

```bash
kubectl -n hello port-forward svc/hello 8080:80 &
curl -s localhost:8080/healthz
curl -s -X POST localhost:8080/a2a/tasks -H 'content-type: application/json' \
  -d '{"message": {"role": "user", "parts": [{"kind": "text", "text": "hello! how did you get here?"}]}}'
```

Expected: `/healthz` 200; the task reply mentions the deployment chain (proving the seeded memory was recalled). Record the exact request/response shapes observed — the P1 provisioner's `hello` step reuses them.
- [ ] **Step 7: Verify convergent reseed**: edit `bundle/knowledge/gotchas.md` in the repo, push, wait for CI+rollout, exec into the pod: `kubectl -n hello exec deploy/hello -- python -c "import sqlite3; c=sqlite3.connect('/techne/techne.db'); print(c.execute('select count(*) from knowledge').fetchone())"` — count reflects replacement, not accumulation. (Note: without a PVC the techne volume resets on pod replacement — full-count check is informational; the seeder log line `seeded=.. removed=.. unchanged=..` in `kubectl logs` is the real assertion.)
- [ ] **Step 8: Write results into the runbook file and commit it to cortex.** P0 exit criteria met when Steps 4–6 run with no manual intervention between the API call and the hello reply.

---

## Self-review notes (already applied)

- Spec §6 coverage: daedalus runtime image (Tasks 1–3), gojira JSON API (Task 5), auto-merge (Task 6), agent-class Service (Task 4), branch-only workflow (Global Constraints), E2E exit criterion (Task 8). The `access` step automation, PVC-backed techne, and everything UI are P1 by design.
- Two intentional adaptation points are marked inline rather than guessed: the exact spec-helper name in `tests/test_generator.py` (Task 4) and `GitHubAppClient`'s request-helper name (Task 6) — the implementer mirrors the sibling code in the same file, which is more reliable than this plan hardcoding a name it can't verify.
- Type consistency: `merge_pr(repo, pr_url)` is identical across ABC/GhCLI/AppClient/Fake; step name `merge_setup_pr` matches the web test's assertion; seeder `store_fn`/`delete_fn` signatures match `techne.knowledge.store`/`delete` as read from `techne/core/knowledge.py:58,613`.
