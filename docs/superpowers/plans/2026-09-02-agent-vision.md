# Agent Vision (Wave 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Images uploaded in Slack conversations reach the agent's model as vision inputs -- downloaded with the bot token, base64-encoded, carried on the Message, emitted as multimodal content blocks -- with every skip (wrong type, too big, too many, fetch failure) announced in the message text, never silent.

**Architecture:** Three daedalus layers gain image plumbing: `Message` gets an optional `images: list[ImagePart]` carrier (default None, so every existing construction site is untouched); `Agent._messages` emits OpenAI-style multipart content when a message carries images (plain string otherwise -- litellm translates per provider); the Slack transport gathers `event["files"]` through caps and mimetype filters, downloads via a new `SlackApi.download_file`, and appends skip-notes to the message text. The learnings journal strips image bytes (a text marker replaces them) so wave 1's storage never balloons. kobo's manifest generator adds the `files:read` scope.

**Tech Stack:** Python 3.12, pydantic, slack_sdk (AsyncWebClient), httpx (added to the `slack` extra -- repo convention for raw authenticated GETs), litellm multimodal content blocks.

**Spec:** `docs/superpowers/specs/2026-09-01-learnings-ui-vision-design.md`, Wave 3 section including the corrected Scopes note.

## Global Constraints

- The org/vendor name (acme) NEVER appears in daedalus or kobo source, comments, or string literals; no em dashes (use "--" in comments).
- Exact bounds: per-image size cap 20 MB pre-encoding (`_IMAGE_MAX_BYTES = 20 * 1024 * 1024`, checked against `file["size"]` BEFORE download); per-message cap 4 images (`_IMAGE_CAP = 4`); accepted mimetypes exactly `{"image/png", "image/jpeg", "image/gif", "image/webp"}`.
- Bounds are announced, never silent: skipped files append a parenthetical note to the message text (exact copy in Task 3).
- An image-only Slack message (no text) still reaches the agent (`text` falls back to `"(image)"`); a message with neither text nor images is ignored exactly as an empty message is today.
- A download failure skips that image with a note -- it never fails the event.
- The A2A wire format stays text-only (`task_to_a2a` serializes `m.text` verbatim; images are deliberately not carried -- registered follow-up, out of scope).
- The learnings journal NEVER stores base64 image data: journaled entries exclude `images` and append a `[shared N image(s)]` marker to the text.
- daedalus: `ruff check src tests` passes; tests run `python -m pytest tests/ -x -q` from the repo root.
- kobo API tests run from `api/`: `uv run python -m pytest tests/<file> -x -q`.
- daedalus work happens on branch `feat/vision`, branched FROM `feat/learnings` (this plan's journal work builds on PR #26's code; the PR must say "merge #26 first"). kobo work happens on branch `feat/vision-scope` from `main`.

---

## Repo split

| Tasks | Repo | Branch |
|-------|------|--------|
| 1-3 | `~/Documents/GitHub/daedalus` | `feat/vision` (from `feat/learnings`) |
| 4 | `~/Documents/GitHub/kobo` | `feat/vision-scope` (from `main`) |

---

### Task 1: ImagePart carrier on Message (daedalus)

**Files:**
- Modify: `src/daedalus/task.py` (Message class, lines ~46-48)
- Test: `tests/test_task_images.py` (new)

**Interfaces:**
- Consumes: nothing.
- Produces: `class ImagePart(BaseModel)` with `data: str` (raw base64, no `data:` URI prefix) and `media_type: str`; `Message.images: list[ImagePart] | None = None`. Tasks 2-3 import `ImagePart` from `daedalus.task`.

- [ ] **Step 1: Create the branch**

```bash
git -C ~/Documents/GitHub/daedalus checkout feat/learnings
git -C ~/Documents/GitHub/daedalus checkout -b feat/vision
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_task_images.py`:

```python
"""The image carrier on Message: optional, default None, round-trips."""

from daedalus.task import ImagePart, Message, Task


def test_message_images_default_none_keeps_old_call_sites_working():
    message = Message(role="user", text="hi")
    assert message.images is None
    # Serialization keeps the field (schema-stable for checkpoints), value None.
    assert message.model_dump()["images"] is None


def test_image_part_round_trips_through_model_dump():
    part = ImagePart(data="aGVsbG8=", media_type="image/png")
    message = Message(role="user", text="look", images=[part])

    dumped = message.model_dump()
    revived = Message.model_validate(dumped)

    assert revived.images is not None
    assert revived.images[0].data == "aGVsbG8="
    assert revived.images[0].media_type == "image/png"


def test_from_text_carries_no_images():
    task = Task.from_text("hello")
    assert task.history[0].images is None
```

- [ ] **Step 3: Run to verify failure**

Run: `cd ~/Documents/GitHub/daedalus && python -m pytest tests/test_task_images.py -x -q`
Expected: FAIL with `ImportError: cannot import name 'ImagePart'`

- [ ] **Step 4: Implement**

In `src/daedalus/task.py`, directly above the `Message` class:

```python
class ImagePart(BaseModel):
    """One image attached to a Message (agent-vision spec, wave 3).

    `data` is the raw base64 string -- no `data:` URI prefix; the LLM
    message builder (agent._messages) adds the provider wrapper. The A2A
    wire format deliberately does NOT carry these (task_to_a2a serializes
    text only; agent-to-agent image passing is a registered follow-up).
    """

    data: str
    media_type: str
```

and extend `Message`:

```python
class Message(BaseModel):
    role: Literal["user", "agent"]
    text: str
    # None (not []) so every existing Message(role=..., text=...) call site
    # -- from_text, reply, the cli/a2a/slack transports -- is untouched.
    images: list[ImagePart] | None = None
```

- [ ] **Step 5: Run the new tests, then the full suite**

Run: `cd ~/Documents/GitHub/daedalus && python -m pytest tests/test_task_images.py -x -q && python -m pytest tests/ -x -q`
Expected: all pass (the full run proves checkpoint/journal/a2a paths tolerate the new optional field)

- [ ] **Step 6: Lint and commit**

```bash
cd ~/Documents/GitHub/daedalus && ruff check src tests
git -C ~/Documents/GitHub/daedalus add src/daedalus/task.py tests/test_task_images.py
git -C ~/Documents/GitHub/daedalus commit -m "feat(task): optional image parts on Message"
```

---

### Task 2: Multimodal _messages + journal image-strip (daedalus)

**Files:**
- Modify: `src/daedalus/agent.py` (`_messages` ~751-763, `_journal_conversation` ~366-392; anchors drift, locate by content)
- Test: `tests/test_agent_vision.py` (new), `tests/test_agent_journal.py` (extend)

**Interfaces:**
- Consumes: Task 1's `ImagePart`, `Message.images`.
- Produces: `_messages` emits, for a history message WITH images, `{"role": ..., "content": [{"type": "text", "text": ...}, {"type": "image_url", "image_url": {"url": "data:<media_type>;base64,<data>"}}, ...]}` and the unchanged flat string otherwise; `_journal_conversation` journals entries with `images` excluded and a ` [shared N image(s)]` text marker when images were present.

- [ ] **Step 1: Write the failing _messages tests**

Create `tests/test_agent_vision.py` (reuse `make_agent` from `tests/test_agent.py` and `ScriptedLLM` from `tests/fakes.py` -- the same construction the rest of the agent tests use; no asyncio markers, pyproject sets asyncio_mode auto):

```python
"""Vision plumbing: a Message with images becomes multipart LLM content;
one without stays a plain string."""

from daedalus.task import ImagePart, Message, Task

from tests.fakes import ScriptedLLM
from tests.test_agent import make_agent


async def test_message_with_images_becomes_multipart_content():
    llm = ScriptedLLM("seen")
    agent = make_agent(llm=llm)
    task = Task.from_text("what is this?")
    task.history[0].images = [ImagePart(data="aGVsbG8=", media_type="image/png")]

    await agent.handle_task(task)

    sent = llm.calls[0]
    user_turn = next(m for m in sent if m["role"] == "user")
    assert isinstance(user_turn["content"], list)
    assert user_turn["content"][0] == {"type": "text", "text": "what is this?"}
    assert user_turn["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,aGVsbG8="},
    }


async def test_message_without_images_stays_a_plain_string():
    llm = ScriptedLLM("plain")
    agent = make_agent(llm=llm)

    await agent.handle_task(Task.from_text("no pictures here"))

    sent = llm.calls[0]
    user_turn = next(m for m in sent if m["role"] == "user")
    assert user_turn["content"] == "no pictures here"


async def test_two_images_produce_two_image_parts_in_order():
    llm = ScriptedLLM("ok")
    agent = make_agent(llm=llm)
    task = Task.from_text("compare")
    task.history[0].images = [
        ImagePart(data="Zmlyc3Q=", media_type="image/png"),
        ImagePart(data="c2Vjb25k", media_type="image/jpeg"),
    ]

    await agent.handle_task(task)

    user_turn = next(m for m in llm.calls[0] if m["role"] == "user")
    urls = [p["image_url"]["url"] for p in user_turn["content"] if p["type"] == "image_url"]
    assert urls == [
        "data:image/png;base64,Zmlyc3Q=",
        "data:image/jpeg;base64,c2Vjb25k",
    ]
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/Documents/GitHub/daedalus && python -m pytest tests/test_agent_vision.py -x -q`
Expected: FAIL -- content is the flat string, not a list

- [ ] **Step 3: Implement the _messages branch**

In `src/daedalus/agent.py`, `_messages`'s history loop becomes:

```python
        for message in task.history:
            role = "assistant" if message.role == "agent" else "user"
            if message.images:
                # Vision (wave 3): OpenAI-style content parts -- litellm
                # translates per provider. Text part first, then every
                # image as a base64 data URI.
                parts: list[dict] = [{"type": "text", "text": message.text}]
                for image in message.images:
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{image.media_type};base64,{image.data}"
                            },
                        }
                    )
                messages.append({"role": role, "content": parts})
            else:
                messages.append({"role": role, "content": message.text})
```

- [ ] **Step 4: Run the vision tests**

Run: `cd ~/Documents/GitHub/daedalus && python -m pytest tests/test_agent_vision.py -x -q`
Expected: 3 passed

- [ ] **Step 5: Write the failing journal-strip test**

Append to `tests/test_agent_journal.py` (reuse its existing `FakeMemory` and construction pattern):

```python
async def test_journal_strips_image_bytes_and_leaves_a_marker(tmp_path):
    agent = Agent.load(
        GOLDEN, llm=ScriptedLLM(["nice photo"]), memory=FakeMemory(tmp_path / "techne.db")
    )
    task = Task.from_text("look at this")
    task.history[0].images = [ImagePart(data="QUFB" * 1000, media_type="image/png")]

    result = await agent.handle_task(task)

    assert result.state is TaskState.COMPLETED
    rows = recent(tmp_path / "journal.db")
    entry = rows[0]["history"][0]
    assert "images" not in entry
    assert "QUFB" not in str(rows[0]["history"])
    assert entry["text"] == "look at this [shared 1 image(s)]"
```

(Adapt the `Agent.load(GOLDEN, ...)` construction line to whatever `tests/test_agent_journal.py` actually uses -- construction only, never assertions. Add the `ImagePart` import.)

- [ ] **Step 6: Run to verify failure, then implement the strip**

Run: `cd ~/Documents/GitHub/daedalus && python -m pytest tests/test_agent_journal.py -x -q`
Expected: the new test FAILS (base64 present in the journal)

In `_journal_conversation`, replace the history dump line:

```python
        entries = []
        for message in task.history:
            # The journal is the learnings summarizer's TEXT source: base64
            # image bytes would balloon storage (megabytes per row) and mean
            # nothing to the summarizer. Strip them; a marker keeps the fact
            # that images were shared visible to the day's summary.
            entry = message.model_dump(exclude={"images"})
            if message.images:
                entry["text"] = f"{entry['text']} [shared {len(message.images)} image(s)]".strip()
            entries.append(entry)
        try:
            journal.record(
                journal.journal_path(Path(db_path)),
                task.context_id or task.id,
                entries,
            )
```

(The `entries` construction sits above the existing try so a model_dump error also lands in the except-return path -- move it inside the `try` if that reads cleaner; either way the method still returns `str(exc)` on any failure.)

- [ ] **Step 7: Run journal tests + full suite + lint**

Run: `cd ~/Documents/GitHub/daedalus && python -m pytest tests/test_agent_journal.py tests/test_agent_vision.py -x -q && python -m pytest tests/ -x -q && ruff check src tests`
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git -C ~/Documents/GitHub/daedalus add src/daedalus/agent.py tests/test_agent_vision.py tests/test_agent_journal.py
git -C ~/Documents/GitHub/daedalus commit -m "feat(agent): multimodal content for image messages, journal strips bytes"
```

---

### Task 3: Slack transport gathers images (daedalus)

**Files:**
- Modify: `src/daedalus/transports/slack.py` (SlackApi ~127-182, handle_event ~279-343), `pyproject.toml` (slack extra, line ~23)
- Test: `tests/test_transport_slack.py` (extend)

**Interfaces:**
- Consumes: Task 1's `ImagePart`; `SlackApi._web.token` (AsyncWebClient exposes `.token`).
- Produces: `SlackApi.download_file(url: str) -> bytes` (async, authenticated GET via httpx); module constants `_IMAGE_MAX_BYTES = 20 * 1024 * 1024`, `_IMAGE_CAP = 4`, `_IMAGE_MIMETYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}`; `SlackTransport._gather_images(files: list[dict]) -> tuple[list[ImagePart], list[str]]`.

- [ ] **Step 1: Read the test file's fakes**

Read `tests/test_transport_slack.py` fully: `FakeApi` (records posts/updates/reacts), the `make()` helper injecting it, and the `event(...)` fixture builder. The new tests extend `FakeApi` with a `download_file` that records URLs and returns scripted bytes (raising when scripted to), and extend `event(...)` with a `files=` kwarg.

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_transport_slack.py` (adapt constructions to the file's helpers; a `_file` fixture helper keeps them short):

```python
def _file(name="pic.png", mimetype="image/png", size=1024, url="https://files.example/pic"):
    return {"name": name, "mimetype": mimetype, "size": size, "url_private": url}


async def test_image_attachment_reaches_the_task_as_base64():
    transport, api, handler = make()
    api.downloads_return = b"hello"
    await transport.handle_event(event("what is this?", ts="200.1", files=[_file()]), handler)

    message = handler.tasks[-1].history[0]
    assert message.text == "what is this?"
    assert message.images is not None
    assert message.images[0].media_type == "image/png"
    assert message.images[0].data == "aGVsbG8="  # base64("hello")
    assert api.downloaded == ["https://files.example/pic"]


async def test_image_only_message_still_reaches_the_agent():
    transport, api, handler = make()
    api.downloads_return = b"x"
    await transport.handle_event(event("", ts="200.2", files=[_file()]), handler)

    message = handler.tasks[-1].history[0]
    assert message.text == "(image)"
    assert message.images is not None


async def test_non_image_file_is_skipped_with_a_note():
    transport, api, handler = make()
    await transport.handle_event(
        event("see attached", ts="200.3", files=[_file(name="report.pdf", mimetype="application/pdf")]),
        handler,
    )

    message = handler.tasks[-1].history[0]
    assert message.images is None
    assert "(attached file report.pdf skipped -- not an image)" in message.text
    assert api.downloaded == []


async def test_oversized_image_is_skipped_before_download():
    transport, api, handler = make()
    await transport.handle_event(
        event("big one", ts="200.4", files=[_file(size=21 * 1024 * 1024)]), handler
    )

    message = handler.tasks[-1].history[0]
    assert message.images is None
    assert "(image pic.png skipped -- exceeds 20 MB limit)" in message.text
    assert api.downloaded == []


async def test_fifth_image_is_dropped_with_a_count_note():
    transport, api, handler = make()
    api.downloads_return = b"x"
    files = [_file(name=f"p{i}.png", url=f"https://files.example/p{i}") for i in range(5)]
    await transport.handle_event(event("album", ts="200.5", files=files), handler)

    message = handler.tasks[-1].history[0]
    assert message.images is not None and len(message.images) == 4
    assert "(1 additional image(s) skipped -- max 4 per message)" in message.text


async def test_download_failure_skips_that_image_with_a_note():
    transport, api, handler = make()
    api.downloads_raise = RuntimeError("403")
    await transport.handle_event(event("broken", ts="200.6", files=[_file()]), handler)

    message = handler.tasks[-1].history[0]
    assert message.images is None
    assert "(image pic.png could not be fetched)" in message.text


async def test_message_with_neither_text_nor_images_is_ignored():
    transport, api, handler = make()
    await transport.handle_event(
        event("", ts="200.7", files=[_file(name="notes.txt", mimetype="text/plain")]), handler
    )
    # A skipped non-image with no text is still a contentless message... except
    # the skip NOTE is content the sender should see. Ruling: notes alone DO
    # form a message (the agent explains what it could not read), so the task
    # exists and its text is the note.
    message = handler.tasks[-1].history[0]
    assert "(attached file notes.txt skipped -- not an image)" in message.text
```

Note on the `handler.tasks` accessor and `event(files=...)`: extend the file's existing fake handler/`event` helper accordingly (the fake handler already receives the task; record it if it doesn't). Extend `FakeApi` with:

```python
    downloads_return: bytes = b""
    downloads_raise: Exception | None = None

    async def download_file(self, url):
        self.downloaded.append(url)
        if self.downloads_raise is not None:
            raise self.downloads_raise
        return self.downloads_return
```

(and `self.downloaded = []` in its `__init__`).

- [ ] **Step 3: Run to verify failure**

Run: `cd ~/Documents/GitHub/daedalus && python -m pytest tests/test_transport_slack.py -x -q`
Expected: new tests FAIL (no files handling; `event()` may need the `files=` kwarg added first so collection succeeds)

- [ ] **Step 4: Implement**

1. `pyproject.toml`: the slack extra becomes `["slack-sdk>=3.27", "aiohttp>=3.9", "httpx>=0.27"]` -- httpx is this repo's convention for raw authenticated GETs (a2a_auth.py, github.py, colleagues.py all use it).

2. `src/daedalus/transports/slack.py` constants (near `_SEEN_CAP`):

```python
# Vision (wave 3): bounds for inbound Slack image attachments. Size is
# checked against the event's own file metadata BEFORE any download.
_IMAGE_MAX_BYTES = 20 * 1024 * 1024
_IMAGE_CAP = 4
_IMAGE_MIMETYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
```

and import `ImagePart` alongside the existing `from daedalus.task import ...`.

3. `SlackApi` gains:

```python
    async def download_file(self, url: str) -> bytes:
        """Fetch a Slack file's bytes. files.url_private requires the bot
        token as a bearer header -- slack_sdk has no download helper, so
        this is a raw authenticated GET (httpx, same as every other
        outbound integration in this repo)."""
        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                url, headers={"Authorization": f"Bearer {self._web.token}"}
            )
            response.raise_for_status()
            return response.content
```

4. `SlackTransport` gains the gatherer:

```python
    async def _gather_images(self, files: list[dict]) -> tuple[list["ImagePart"], list[str]]:
        """Filter, bound, and fetch the event's image attachments. Every
        skip is a note the sender sees appended to their message text --
        bounds are announced, never silent (wave 3 spec)."""
        import base64

        images: list[ImagePart] = []
        notes: list[str] = []
        skipped_over_cap = 0
        for file in files:
            name = file.get("name") or "file"
            if file.get("mimetype") not in _IMAGE_MIMETYPES:
                notes.append(f"(attached file {name} skipped -- not an image)")
                continue
            if (file.get("size") or 0) > _IMAGE_MAX_BYTES:
                notes.append(f"(image {name} skipped -- exceeds 20 MB limit)")
                continue
            if len(images) >= _IMAGE_CAP:
                skipped_over_cap += 1
                continue
            url = file.get("url_private")
            if not url:
                notes.append(f"(image {name} could not be fetched)")
                continue
            try:
                raw = await self._api.download_file(url)
            except Exception:
                notes.append(f"(image {name} could not be fetched)")
                continue
            images.append(
                ImagePart(
                    data=base64.b64encode(raw).decode("ascii"),
                    media_type=file["mimetype"],
                )
            )
        if skipped_over_cap:
            notes.append(
                f"({skipped_over_cap} additional image(s) skipped -- max {_IMAGE_CAP} per message)"
            )
        return images, notes
```

5. `handle_event`: replace the text-extraction block

```python
        text = _MENTION.sub("", event.get("text") or "").strip()
        if not text:
            return
```

with

```python
        text = _MENTION.sub("", event.get("text") or "").strip()
        images, notes = await self._gather_images(event.get("files") or [])
        if notes:
            text = "\n".join(filter(None, [text, *notes]))
        if not text and not images:
            return
        if not text:
            # An image with no words is still an ask.
            text = "(image)"
```

and the Message append becomes:

```python
        task.history.append(Message(role="user", text=text, images=images or None))
```

- [ ] **Step 5: Run the transport tests, full suite, lint**

Run: `cd ~/Documents/GitHub/daedalus && python -m pytest tests/test_transport_slack.py -x -q && python -m pytest tests/ -x -q && ruff check src tests`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git -C ~/Documents/GitHub/daedalus add src/daedalus/transports/slack.py pyproject.toml tests/test_transport_slack.py
git -C ~/Documents/GitHub/daedalus commit -m "feat(slack): image attachments reach the model -- bounded, announced, authenticated"
```

---

### Task 4: files:read in the kobo manifest (kobo)

**Files:**
- Modify: `api/kobo_api/slack_setup.py` (`_BOT_SCOPES`, line ~66-86)
- Test: `api/tests/test_slack_setup.py` (the scope pin, line ~84 area)

**Interfaces:** none new -- the manifest generator emits one more bot scope.

- [ ] **Step 1: Create the branch**

```bash
git -C ~/Documents/GitHub/kobo checkout main
git -C ~/Documents/GitHub/kobo pull --ff-only
git -C ~/Documents/GitHub/kobo checkout -b feat/vision-scope
```

- [ ] **Step 2: Update the failing test pin first**

In `api/tests/test_slack_setup.py`, the exact-scope-list assertion gains `"files:read"` in the position matching the implementation below (alphabetical-ish placement after `chat:write` matches the list's existing grouping -- mirror wherever you insert it in the source).

Run: `cd ~/Documents/GitHub/kobo/api && uv run python -m pytest tests/test_slack_setup.py -x -q`
Expected: FAIL (scope list mismatch)

- [ ] **Step 3: Add the scope**

In `api/kobo_api/slack_setup.py`, `_BOT_SCOPES` gains (after `"chat:write"`):

```python
    "chat:write",
    # Vision (wave 3): downloading a message's image attachments via
    # files.url_private requires this read scope. A SCOPE addition --
    # unlike the tagged-only event change -- requires each existing
    # agent's Slack app to be reinstalled to its workspace after the
    # manifest re-apply; new agents get it from this generator.
    "files:read",
```

- [ ] **Step 4: Run the setup tests, then the full API suite**

Run: `cd ~/Documents/GitHub/kobo/api && uv run python -m pytest tests/test_slack_setup.py -x -q && uv run python -m pytest tests/ -q`
Expected: all pass (the engine racing test is known load-flaky; if it is the only failure, rerun it in isolation and say so)

- [ ] **Step 5: Commit**

```bash
git -C ~/Documents/GitHub/kobo add api/kobo_api/slack_setup.py api/tests/test_slack_setup.py
git -C ~/Documents/GitHub/kobo commit -m "feat(slack): manifest carries files:read -- vision needs the download scope"
```

---

## Rollout (operator-driven, NOT part of task execution)

1. daedalus: coworker merges #26 (learnings), THEN the feat/vision PR; rebuild runtime image from merged main (`<sha>-t<techne_ver>` tag); re-pin `runtime_image` in kobo-config.
2. kobo: merge feat/vision-scope -> main -> build/roll (can ride any kobo roll; the scope only matters when agents are recreated or manifests re-applied).
3. Existing agents: re-apply the manifest at api.slack.com AND reinstall the app to the workspace (scope change) -- coordinate with agent owners (Leslie for clio).
4. Wire acceptance: DM an image to a rebuilt agent, watch it describe the image; drop a 25 MB file and a PDF in one message, confirm both notes appear in the reply thread's context.

## Self-review notes

- Spec coverage: ImagePart/Message (Task 1), multimodal _messages (Task 2), event parsing + download + caps + notes (Task 3), scopes correction (Task 4), checkpoint-size note (accepted per spec -- caps bound it; the optional strip-after-processing optimization stays a follow-up), A2A images out of scope (constraint + ImagePart docstring), journal ballooning (Task 2's strip -- an interaction the spec's checkpoint section anticipated, resolved here).
- Deviation from the spec's sketch: the spec said an image-only message was not addressed; ruled here that it reaches the agent with text "(image)", and that a skipped-file note alone still forms a message (the sender deserves the explanation). Flag to the operator at handoff.
- Known adaptation points: test_transport_slack.py's fake handler may need a `tasks` recording list (Step 1 reads it first); test_agent_journal.py construction lines.
