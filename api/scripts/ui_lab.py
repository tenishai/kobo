"""A local room for working on the UI: the real API routes over the test
fakes, seeded with a handful of agents in every state the UI draws.

Nothing here reaches GitHub, the cluster, the switchboard or any bucket.
The database is a throwaway SQLite file under api/.lab/, re-seeded on every
start unless KOBO_LAB_KEEP=1.

    cd api && .venv/bin/python scripts/ui_lab.py        # API on :8100
    cd ui  && npm run dev                               # UI on :5173, proxies /api

The signed-in person is KOBO_LAB_EMAIL (default alex.rivera@example.com), an admin of
the room with the admin panel on. KOBO_LAB_ACCESS=0 turns the permission
service off (the Harness then shows its Identity and access section).
"""

from __future__ import annotations

import base64
import colorsys
import json
import math
import os
import shutil
import struct
import sys
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

API = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API))  # `tests.fakes` imports as a namespace package

import uvicorn
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config, GroupChoice, ModelChoice
from kobo_api.kinds import KIND_CONNECTED
from kobo_api.main import create_app
from kobo_api.models import (
    Agent,
    AgentLearning,
    AgentStatus,
    AgentStep,
    Base,
    StepStatus,
)
from kobo_api.personas import seed_default_personas
from kobo_api.provisioner import CONNECTED_PIPELINE, PIPELINE
from tests.fakes import (
    FakeAgent,
    FakeBab,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeSero,
    FakeSlack,
    FakeStorage,
)

ME = os.environ.get("KOBO_LAB_EMAIL", "alex.rivera@example.com")
PORT = int(os.environ.get("KOBO_LAB_PORT", "8100"))
LAB = API / ".lab"
ACCESS_ON = os.environ.get("KOBO_LAB_ACCESS", "1") != "0"

SKILLS_BUCKET = "skills"
PERSONAS_BUCKET = "personas"
MODELS = [
    ModelChoice(
        id="vertex_ai/claude-opus-4-1",
        label="Claude Opus 4.1",
        family="Claude",
        tier="best",
    ),
    ModelChoice(
        id="vertex_ai/claude-sonnet-4-5",
        label="Claude Sonnet 4.5",
        family="Claude",
        tier="balanced",
    ),
    ModelChoice(
        id="vertex_ai/claude-haiku-4-5",
        label="Claude Haiku 4.5",
        family="Claude",
        tier="fast",
    ),
    ModelChoice(
        id="vertex_ai/gemini-2.5-pro",
        label="Gemini 2.5 Pro",
        family="Gemini",
        tier="best",
    ),
    ModelChoice(
        id="vertex_ai/gemini-2.5-flash",
        label="Gemini 2.5 Flash",
        family="Gemini",
        tier="balanced",
    ),
]
GROUPS = [
    GroupChoice(name="infra-engineering", okta_group="infra-engineering"),
    GroupChoice(name="growth", okta_group="growth-eng"),
    GroupChoice(name="security", okta_group="security"),
    GroupChoice(name="finance", okta_group="finance", kind="non-engineering"),
]


class LabGroups:
    """The directory, as a dict: who is in which room group."""

    def groups_for(self, email: str) -> list[str] | None:
        if email == ME:
            return ["infra-engineering", "growth"]
        return ["growth"]


class LabIdentity:
    """What the identity proxy would have done: name the caller."""

    def __init__(self, app, email: str):
        self.app = app
        self.email = email

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope.setdefault("state", {})["iap_email"] = self.email
        await self.app(scope, receive, send)


def _storage() -> FakeStorage:
    storage = FakeStorage()
    seed_default_personas(storage, bucket=PERSONAS_BUCKET)
    skills = [
        (
            "shared",
            "release-notes",
            "Release notes",
            "Turns merged pull requests into release notes.",
            ["docs"],
        ),
        (
            "shared",
            "word-games",
            "Word games",
            "Plays word games with the user.",
            ["fun"],
        ),
        (
            "sre",
            "incident-triage",
            "Incident triage",
            "Walks an alert to a first hypothesis.",
            ["ops"],
        ),
        (
            "sre",
            "runbook-lookup",
            "Runbook lookup",
            "Finds and quotes the runbook for a service.",
            ["ops"],
        ),
        (
            "qa",
            "test-plan",
            "Test plan",
            "Drafts a test plan from a change description.",
            ["quality"],
        ),
    ]
    entries = []
    for team, sid, name, desc, tags in skills:
        storage.write_bytes(
            SKILLS_BUCKET,
            f"{team}/{sid}/SKILL.md",
            f"---\nname: {sid}\ndescription: {desc}\n---\n# {name}\n\n{desc}\n".encode(),
            metadata={"source-sha": f"sha-{sid}"},
            content_type="text/markdown",
        )
        entries.append(
            {
                "id": sid,
                "name": name,
                "description": desc,
                "team": team,
                "tags": tags,
                "sha": f"sha-{sid}",
                "builtin": False,
            }
        )
    storage.write_bytes(
        SKILLS_BUCKET,
        "index.json",
        json.dumps({"entries": entries}).encode(),
        content_type="application/json",
    )
    return storage


def _portrait(hue: float) -> str:
    """A 96x96 lab portrait: a soft two-tone radial gradient with a lighter
    disc for a head, as base64 PNG, the way the room stores avatars. Pure
    standard library, so the lab needs no image package."""
    size = 96
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            dx, dy = (x - size / 2) / size, (y - size / 2) / size
            d = math.hypot(dx, dy)
            light = 0.55 + 0.25 * (1 - min(d * 1.6, 1))
            head = math.hypot(x - size / 2, y - size * 0.42) < size * 0.2
            body = math.hypot((x - size / 2) * 0.8, y - size * 1.02) < size * 0.42
            if head or body:
                light = min(light + 0.2, 0.95)
            red, green, blue = colorsys.hls_to_rgb(hue, light, 0.45)
            row += bytes((int(red * 255), int(green * 255), int(blue * 255)))
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode("ascii")


def _steps(
    agent: Agent,
    pipeline,
    *,
    done: int,
    then: str | None = None,
    error: str | None = None,
    start: datetime,
) -> list[AgentStep]:
    rows = []
    t = start
    for i, step in enumerate(pipeline):
        if i < done:
            rows.append(
                AgentStep(
                    agent=agent,
                    name=step.name,
                    status=StepStatus.DONE,
                    started_at=t,
                    finished_at=t + timedelta(seconds=40),
                )
            )
            t += timedelta(seconds=45)
        elif i == done and then:
            rows.append(
                AgentStep(
                    agent=agent, name=step.name, status=then, error=error, started_at=t
                )
            )
        else:
            rows.append(
                AgentStep(agent=agent, name=step.name, status=StepStatus.PENDING)
            )
    return rows


def _seed(session_factory) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    sonnet, opus, flash = MODELS[1].id, MODELS[0].id, MODELS[4].id
    with session_factory() as s:
        clio = Agent(
            name="clio",
            persona="helpful-generalist",
            model_id=sonnet,
            status=AgentStatus.READY,
            repo_full_name="example-org/agent-clio",
            group_name="growth",
            team="shared",
            created_by=ME,
            capability="Writes release notes from the week's merged pull requests.",
            purpose_tags=json.dumps(["release notes", "changelog", "github"]),
            archetype="scribe",
            skills=[
                {
                    "name": "release-notes",
                    "team": "shared",
                    "sha": "sha-release-notes",
                    "source": "catalog",
                }
            ],
            colleague_mode="colleague",
            slack_wired=True,
            slack_app_id="A0CLIO",
            slack_channels=json.dumps(["#releases"]),
            slack_wake="mention",
            first_reply="Hello! I'm Clio. Point me at a repository and I'll write up what shipped.",
            created_at=now - timedelta(days=41),
        )
        ward = Agent(
            name="ward",
            persona="helpful-generalist",
            model_id=opus,
            status=AgentStatus.READY,
            repo_full_name="example-org/agent-ward",
            group_name="security",
            team="sre",
            created_by="sam.okafor@example.com",
            capability="Audits access requests and keeps the evidence trail.",
            purpose_tags=json.dumps(["audit", "access", "sox"]),
            archetype="auditor",
            skills=[
                {
                    "name": "incident-triage",
                    "team": "sre",
                    "sha": "sha-incident-triage",
                    "source": "catalog",
                },
                {
                    "name": "runbook-lookup",
                    "team": "sre",
                    "sha": "sha-runbook-lookup",
                    "source": "catalog",
                },
            ],
            colleague_mode="roster-only",
            created_at=now - timedelta(days=23),
        )
        ledger = Agent(
            name="ledger",
            persona="helpful-generalist",
            model_id=flash,
            status=AgentStatus.READY,
            repo_full_name="example-org/agent-ledger",
            group_name="finance",
            team="shared",
            created_by=None,
            capability="",
            purpose_tags=None,
            colleague_mode="none",
            created_at=now - timedelta(days=9),
        )
        forge = Agent(
            name="forge",
            persona="helpful-generalist",
            model_id=sonnet,
            status=AgentStatus.PROVISIONING,
            repo_full_name="example-org/agent-forge",
            group_name="infra-engineering",
            team="sre",
            created_by=ME,
            capability="Opens the pull request that fixes a failing build.",
            purpose_tags=json.dumps(["ci", "builds"]),
            created_at=now - timedelta(minutes=4),
            run_heartbeat_at=now,
        )
        atlas = Agent(
            name="atlas",
            persona="helpful-generalist",
            model_id=sonnet,
            status=AgentStatus.FAILED,
            repo_full_name="example-org/agent-atlas",
            group_name="growth",
            team="qa",
            created_by=ME,
            capability="Maps which services call which, from traces.",
            purpose_tags=json.dumps(["topology", "tracing"]),
            error="Build failed: the runtime image could not be pulled (manifest unknown).",
            created_at=now - timedelta(hours=3),
        )
        prudence = Agent(
            name="prudence",
            persona="connected",
            model_id=sonnet,
            status=AgentStatus.WAITING,
            kind=KIND_CONNECTED,
            repo_full_name="example-org/pr-reviewer",
            group_name="infra-engineering",
            created_by=ME,
            capability="Reviews pull requests for the platform team.",
            purpose_tags=json.dumps(["code review"]),
            created_at=now - timedelta(days=1),
        )
        scribe = Agent(
            name="scribe",
            persona="helpful-generalist",
            model_id=sonnet,
            status=AgentStatus.READY,
            repo_full_name="example-org/agent-scribe",
            group_name="growth",
            team="shared",
            created_by="lee.nakamura@example.com",
            capability="Keeps meeting notes and turns them into decisions.",
            purpose_tags=json.dumps(["notes", "decisions"]),
            skills=[
                {
                    "name": "word-games",
                    "team": "shared",
                    "sha": "sha-word-games",
                    "source": "catalog",
                }
            ],
            colleague_mode="colleague",
            created_at=now - timedelta(days=60),
        )
        # Every composed agent has a portrait, as on a real room; prudence,
        # the connected one, has none, so the card's fallback shows too.
        for agent, hue in (
            (clio, 0.08),
            (ward, 0.58),
            (ledger, 0.3),
            (forge, 0.02),
            (atlas, 0.75),
            (scribe, 0.13),
        ):
            agent.avatar_png = _portrait(hue)
        s.add_all([clio, ward, ledger, forge, atlas, prudence, scribe])
        s.flush()
        for a in (clio, ward, ledger, scribe):
            s.add_all(_steps(a, PIPELINE, done=len(PIPELINE), start=a.created_at))
        s.add_all(
            _steps(
                forge, PIPELINE, done=5, then=StepStatus.RUNNING, start=forge.created_at
            )
        )
        s.add_all(
            _steps(
                atlas,
                PIPELINE,
                done=5,
                then=StepStatus.FAILED,
                error=atlas.error,
                start=atlas.created_at,
            )
        )
        s.add_all(
            _steps(
                prudence,
                CONNECTED_PIPELINE,
                done=2,
                then=StepStatus.WAITING,
                error="Waiting for your team to merge https://github.com/example-org/pr-reviewer/pull/7",
                start=prudence.created_at,
            )
        )
        learnings = [
            (
                "The release notes go in CHANGELOG.md under an Unreleased heading, newest first.",
                "fact",
                "pending",
                "kyle",
            ),
            (
                "Skip dependabot pull requests when listing what shipped.",
                "rule",
                "pending",
                "sam",
            ),
            ("Growth's release day is Thursday.", "fact", "kept", "lee"),
            (
                "Use the PR title, not the first commit, as the headline.",
                "rule",
                "kept",
                "kyle",
            ),
        ]
        for i, (content, kind, status, who) in enumerate(learnings):
            s.add(
                AgentLearning(
                    agent_name="clio",
                    day=(now - timedelta(days=i)).strftime("%Y-%m-%d"),
                    content=content,
                    kind=kind,
                    source=f"conversation:{1000 + i}",
                    evidence=content,
                    said_by=who,
                    content_hash=f"h{i:04d}",
                    status=status,
                )
            )
        s.commit()


def main() -> None:
    if os.environ.get("KOBO_LAB_KEEP") != "1" and LAB.exists():
        shutil.rmtree(LAB)
    LAB.mkdir(parents=True, exist_ok=True)
    db = LAB / "kobo.db"
    fresh = not db.exists()
    config = Config(
        workdir=LAB / "work",
        db_url=f"sqlite:///{db}",
        allow_unauthenticated=True,
        operators=[ME],
        admin_groups=["infra-engineering"],
        admin_panel=True,
        groups=GROUPS,
        models=MODELS,
        skills_bucket=SKILLS_BUCKET,
        personas_bucket=PERSONAS_BUCKET,
        cabal_url="http://cabal.lab",
        agent_endpoint_template="http://agent-{name}.lab",
        noto_url="https://noto.example",
        noto_project="agents",
        bab_url="http://bab.lab" if ACCESS_ON else "",
        logout_url="/?signed-out",
    )
    os.environ.setdefault("CABAL_OPERATOR_TOKEN", "lab-operator-token")
    engine = create_engine(config.db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    if fresh:
        _seed(session_factory)

    clients = Clients(
        github=FakeGitHub(),
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(reply="Hello from the lab. Nothing I say leaves this laptop."),
        kubectl=FakeKubectl(),
        slack=FakeSlack(),
        storage=_storage(),
        cabal=FakeCabal(
            relay_result={
                "reply": "Hello from the lab. Nothing I say leaves this laptop."
            }
        ),
        groups=LabGroups(),
        sero=FakeSero(),
    )
    if ACCESS_ON:
        clients.bab = FakeBab()
    app = create_app(session_factory, config=config, clients=clients)
    app.add_middleware(LabIdentity, email=ME)
    print(f"ui lab: API on http://127.0.0.1:{PORT} as {ME}; db {db}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
