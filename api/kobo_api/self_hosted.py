"""Self-hosted models (kobo#390): a provider that is only models.

An endpoint the person runs themselves (vLLM, Ollama, a LiteLLM proxy, any
OpenAI-compatible server) at an address they give the room. One row per
endpoint, named by the person; a room may hold many. Nothing here goes
through the permission service or the tool gateway: nothing is leased, the
models are standing, held by the room, and served to agents through the
room's model gateway like a cloud's.

The connect form carries the base address (the OpenAI-compatible root; the
room appends `/v1` when it is missing), an optional key, the model names
the endpoint serves (one per line, `name` or `name = label`) and one
optional block of request options (YAML or JSON) sent with every call to
every model of the row (`chat_template_kwargs: {enable_thinking: false}`
for a Qwen-style model whose thinking would otherwise eat a small reply
budget). The options are checked for shape only; the server is the judge of
their meaning.

The probe (`probe`) is the room's own httpx call: `GET <base>/models` with
the key, then one tiny chat completion per named model with the row's
options, so an entry that points at an engine that no longer exists is
caught at connect (the live finding on kobo#390) and marked on the row
rather than registered on the gateway. The row connects as long as one
model answers; Check models runs the same probe again.

The key, when there is one, is held in the room's own Secret
(`self-hosted-<row id>` in the room's namespace, the same place the room
keeps its Slack and GitHub values), because the gateway filler has to read
it back to register the models; the secrets add-on has no read-back lane.
It is never logged, never in a row, a sentence or an audit body.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import yaml

from .clients import ClientError

log = logging.getLogger(__name__)

VENDOR = "self-hosted"
AUTH = "self_hosted_endpoint"
LABEL = "Self-hosted models"
DESCRIPTION = "Models you run yourself, on your own machines or clusters, at an address you give the room."

# The form's secret field, upper-cased by `admin_integrations.connect`.
KEY_FIELD = "API_KEY"
# What the gateway is told when the endpoint takes no key: LiteLLM's OpenAI
# client insists on some string, and this one says what it is.
NO_AUTH_KEY = "no-auth"
# The Secret the key lives in, in the room's namespace, and its one key.
SECRET_PREFIX = "self-hosted-"
CREDENTIAL_REF_PREFIX = "secret:"

# A model name as servers report them: no whitespace, no `=` (the label separator).
_MODEL_NAME = re.compile(r"^[^\s=]+$")
# The chat completion the probe sends: eight tokens is enough for a server to
# prove it can route to the model; the answer itself is thrown away.
PROBE_MAX_TOKENS = 8
PROBE_MESSAGE = "Say hi."
PROBE_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class EndpointRefused(ValueError):
    """A field or an answer the room cannot take, with the sentence that says why."""


def normalize_base_url(raw: str) -> str:
    """The OpenAI-compatible root the room calls: http or https, a host, an
    optional path, and `/v1` at the end whether or not the person typed it.
    `http://10.0.0.5` -> `http://10.0.0.5/v1`; `https://m.example.internal/v1`
    stays as it is."""
    text = (raw or "").strip()
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise EndpointRefused("the base address must start with http:// or https:// and name a host, "
                              "such as http://10.0.0.5 or https://models.example.internal/v1")
    if parts.query or parts.fragment:
        raise EndpointRefused("the base address takes no query or fragment; give the root the server answers /v1/models at")
    path = parts.path.rstrip("/")
    if not path.endswith("/v1"):
        path = f"{path}/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def host_of(base_url: str) -> str:
    """What the row shows: the host (and port) alone, never a key, never a path."""
    return urlsplit(base_url).netloc


def parse_models(text: str) -> list[dict[str, str]]:
    """One model per line, `name` or `name = label`. Blank lines are skipped,
    a repeated name is kept once, and a name with spaces is refused since no
    server reports one."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, label = line.partition("=")
        name, label = name.strip(), label.strip()
        if not _MODEL_NAME.match(name):
            raise EndpointRefused(f"{name or line!r} is not a model name; one per line, as the server reports it, "
                                  "with an optional label after an equals sign (qwen3-32b = Qwen3 32B)")
        if name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "label": label or name})
    if not out:
        raise EndpointRefused("name at least one model the endpoint serves, one per line")
    return out


def parse_options(text: str) -> dict[str, Any]:
    """The request options block, YAML or JSON, as a mapping; empty is fine.
    Shape only: the server decides what the keys mean."""
    if not (text or "").strip():
        return {}
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise EndpointRefused(f"the options block is not YAML or JSON: {str(exc).splitlines()[0]}") from None
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise EndpointRefused("the options block must be a mapping of request fields, "
                              "such as chat_template_kwargs: {enable_thinking: false}")
    return {str(k): v for k, v in loaded.items()}


def model_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "model"


def gateway_model_name(row_id: str, model_name: str) -> str:
    """What agents ask the gateway for: the row's slug, then the model's."""
    return f"{row_id}-{model_slug(model_name)}"


def catalogue_id(row_id: str, model_name: str) -> str:
    """The id an agent's `model` field carries for a self-hosted model:
    `self-hosted/<row id>/<model name>`; the gateway name is derived from
    it alone (`split_catalogue_id`), so a compose never needs the row."""
    return f"{VENDOR}/{row_id}/{model_name}"


def split_catalogue_id(model_id: str) -> tuple[str, str] | None:
    """(`row id`, `model name`) of a self-hosted catalogue id, else None.
    A model name may itself carry a slash (`org/model`), so the split is on
    the first two only."""
    parts = model_id.split("/", 2)
    if len(parts) != 3 or parts[0] != VENDOR or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def secret_name(row_id: str) -> str:
    return f"{SECRET_PREFIX}{row_id}"


def credential_ref(namespace: str, row_id: str) -> str:
    return f"{CREDENTIAL_REF_PREFIX}{namespace}/{secret_name(row_id)}"


def is_credential_ref(ref: str | None) -> bool:
    return bool(ref) and str(ref).startswith(CREDENTIAL_REF_PREFIX)


def read_key(kubectl, ref: str | None) -> str | None:
    """The key the row's credential_ref names, read back from the room's
    Secret; None when the row holds none. Raises `ClientError` naming the
    Secret, never a value, when the read fails."""
    if not is_credential_ref(ref):
        return None
    namespace, _, name = ref[len(CREDENTIAL_REF_PREFIX):].partition("/")
    if kubectl is None:
        raise ClientError(f"this room cannot read the Secret {namespace}/{name}")
    literals = kubectl.secret_literals(namespace, name)
    key = literals.get(KEY_FIELD)
    if not key:
        raise ClientError(f"the Secret {namespace}/{name} holds no {KEY_FIELD}")
    return key


def http_client() -> httpx.Client:
    """The probe's client, one per probe. A module function so a test can
    hand the probe an `httpx.MockTransport` instead."""
    return httpx.Client(timeout=PROBE_TIMEOUT)


@dataclass
class ProbeResult:
    """What the endpoint said: the ids `GET /models` listed, and one entry
    per named model with `ok` and, when it failed, the server's sentence."""

    listed: list[str] = field(default_factory=list)
    models: list[dict[str, Any]] = field(default_factory=list)

    @property
    def answering(self) -> list[dict[str, Any]]:
        return [m for m in self.models if m.get("ok")]

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [m for m in self.models if not m.get("ok")]

    def sentence(self) -> str:
        """`3 of 3 models answer`, or `2 of 3 models answer; old-model failed: ...`."""
        text = f"{len(self.answering)} of {len(self.models)} model{'s' if len(self.models) != 1 else ''} answer"
        failed = [f"`{m['name']}` failed: {m.get('reason') or 'no answer'}" for m in self.failed]
        return f"{text}; {'; '.join(failed)}" if failed else text


def _redact(text: str, api_key: str | None) -> str:
    return text.replace(api_key, "[key]") if api_key else text


def _server_sentence(resp: httpx.Response, api_key: str | None) -> str:
    """The server's own words for a refusal: `error.message` when the body
    is the OpenAI error shape, else the status and the first line of text."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    message = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or "")
        elif isinstance(err, str):
            message = err
        elif body.get("detail"):
            message = str(body["detail"])
    if not message:
        message = (resp.text or "").strip().splitlines()[0][:200] if (resp.text or "").strip() else ""
    sentence = f"HTTP {resp.status_code}" + (f": {message[:300]}" if message else "")
    return _redact(sentence, api_key)


def probe(base_url: str, api_key: str | None, models: list[dict[str, str]], options: dict[str, Any], *,
          client: httpx.Client | None = None) -> ProbeResult:
    """Ask the endpoint what it serves, then ask each named model for a
    short answer with the row's options. Raises `EndpointRefused` when
    `/models` does not answer (nothing to connect); a model that fails is
    marked, not raised. The key travels only on the Authorization header
    and is cut from any sentence that could carry it."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    own = client is None
    client = client or http_client()
    try:
        try:
            resp = client.get(f"{base_url}/models", headers=headers)
        except httpx.HTTPError as exc:
            raise EndpointRefused(f"{host_of(base_url)} did not answer at {base_url}/models: {type(exc).__name__}") from None
        if resp.status_code >= 400:
            raise EndpointRefused(f"{host_of(base_url)} refused {base_url}/models: {_server_sentence(resp, api_key)}")
        result = ProbeResult(listed=_listed_ids(resp))
        for entry in models:
            body: dict[str, Any] = {
                "model": entry["name"],
                "messages": [{"role": "user", "content": PROBE_MESSAGE}],
                "max_tokens": PROBE_MAX_TOKENS,
                **options,
            }
            record = {"name": entry["name"], "label": entry.get("label") or entry["name"], "ok": False, "reason": None}
            try:
                answer = client.post(f"{base_url}/chat/completions", json=body, headers=headers)
            except httpx.HTTPError as exc:
                record["reason"] = f"no answer: {type(exc).__name__}"
            else:
                if answer.status_code >= 400:
                    record["reason"] = _server_sentence(answer, api_key)
                else:
                    record["ok"] = True
            result.models.append(record)
        return result
    finally:
        if own:
            client.close()


def _listed_ids(resp: httpx.Response) -> list[str]:
    try:
        body = resp.json()
    except ValueError:
        return []
    data = body.get("data") if isinstance(body, dict) else body
    if not isinstance(data, list):
        return []
    return [str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id")]


def litellm_params(*, model_name: str, base_url: str, api_key: str | None, options: dict[str, Any]) -> dict[str, Any]:
    """One gateway deployment's `litellm_params` for a self-hosted model:
    LiteLLM's `openai/` route at the endpoint's `/v1`, the key (or the
    no-auth marker), and the row's options as `extra_body`, which is where
    LiteLLM takes fields the OpenAI shape does not name (such as
    `chat_template_kwargs`) and merges them into every request body."""
    params: dict[str, Any] = {"model": f"openai/{model_name}", "api_base": base_url, "api_key": api_key or NO_AUTH_KEY}
    if options:
        params["extra_body"] = dict(options)
    return params
