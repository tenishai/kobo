"""Connecting an integration from the admin panel (#173 step 3): the row,
the credential to the secrets add-on, the pull request on the gateway repo,
and the sentences the card shows when a piece is missing."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import ClientError, Clients
from kobo_api.config import Config
from kobo_api.main import create_app
from kobo_api.models import Base
from tests.fakes import (
    FakeAgent,
    FakeBab,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeSero,
)
from tests.test_api_org_status import (
    ASSERTION_HEADER,
    WHOAMI_AUDIENCE,
    _fetcher,
    _generate_jwk,
    _sign_whoami,
)

GATEWAY_REPO = "example-org/gateway-config"
TARGETS = '[[target]]\nname = "existing"\ntransport = "http"\naddress = "https://old.example"\n'


def _client(tmp_path, fake_storage, config: Config, *, github: FakeGitHub, sero: FakeSero | None):
    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    private_key, jwk = _generate_jwk()
    client = TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=github, gojira=FakeGojira(), cloud=FakeCloud(), agent=FakeAgent(),
                kubectl=FakeKubectl(), storage=fake_storage, sero=sero,
            ),
            iap_jwks_fetcher=_fetcher(jwk),
        )
    )
    return client, {ASSERTION_HEADER: _sign_whoami(private_key, email="ops@example.com")}


def _config(tmp_path, **overrides) -> Config:
    values = {
        "workdir": tmp_path / "work", "iap_audience": WHOAMI_AUDIENCE, "operators": ["ops@example.com"],
        "admin_panel": True, "sero_url": "http://sero.internal", "gateway_config_repo": GATEWAY_REPO,
        "gateway_namespace": "liminal",
    }
    values.update(overrides)
    return Config(**values)


BODY = {"vendor": "datadog", "name": "Datadog", "base_access": "read", "credential": "dd-secret-token",
        "address": "https://mcp.datadoghq.com", "trust": "untrusted"}


def test_connect_stores_the_credential_opens_the_gateway_pr_and_records_the_row(tmp_path, fake_storage):
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"targets.toml": TARGETS}, track_pushed_content=True)
    sero = FakeSero()
    client, headers = _client(tmp_path, fake_storage, _config(tmp_path), github=github, sero=sero)

    response = client.post("/api/admin/integrations", json=BODY, headers=headers)

    assert response.status_code == 201, response.text
    row = response.json()
    assert row["id"] == "datadog" and row["state"] == "pending"
    assert row["credential_ref"] == "apps/kobo/integrations/datadog"
    assert "pull request open on the gateway" in row["state_detail"]
    # The credential went to the vault under the room's path and was synced into the gateway's namespace.
    assert sero.puts == [("ops@example.com", "apps/kobo/integrations/datadog", {"TOKEN": "dd-secret-token"})]
    assert sero.syncs[0][1:] == ("kobo", "apps/kobo/integrations/datadog", "liminal", "integration-datadog")
    # The pull request adds the target without touching the one already there.
    targets = github.pushed_file("targets.toml")
    assert 'name = "existing"' in targets and 'name = "datadog"' in targets
    assert 'bearer_token_env = "INTEGRATION_DATADOG_TOKEN"' in targets
    assert row["spec"]["pull_request"].startswith("https://")
    # And nothing secret is in the row or the audit log.
    listing = client.get("/api/admin/integrations", headers=headers).json()["integrations"]
    assert "dd-secret-token" not in str(listing)
    audit = client.get("/api/admin/audit", headers=headers).json()["events"]
    assert audit[0]["action"] == "integration.connect" and "dd-secret-token" not in str(audit)


def test_connect_without_the_secrets_addon_is_refused_in_one_sentence(tmp_path, fake_storage):
    github = FakeGitHub(existing_repos={GATEWAY_REPO})
    client, headers = _client(tmp_path, fake_storage, _config(tmp_path, sero_url=""), github=github, sero=None)

    response = client.post("/api/admin/integrations", json=BODY, headers=headers)

    assert response.status_code == 409
    assert response.json() == {"error": "the Secrets add-on is off, so the room has nowhere to hold the credential; turn it on first"}
    assert client.get("/api/admin/integrations", headers=headers).json() == {"integrations": []}


def test_connect_with_no_gateway_repo_records_the_row_and_says_what_is_left(tmp_path, fake_storage):
    github = FakeGitHub()
    client, headers = _client(tmp_path, fake_storage, _config(tmp_path, gateway_config_repo="", gateway_namespace=""), github=github, sero=FakeSero())

    response = client.post("/api/admin/integrations", json=BODY, headers=headers)

    assert response.status_code == 201
    detail = response.json()["state_detail"]
    assert "not synced to the gateway yet" in detail and "add the target by hand" in detail
    assert github.pushes == []


def test_disconnect_reverses_the_three_steps(tmp_path, fake_storage):
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"targets.toml": TARGETS}, track_pushed_content=True)
    sero = FakeSero()
    client, headers = _client(tmp_path, fake_storage, _config(tmp_path), github=github, sero=sero)
    client.post("/api/admin/integrations", json=BODY, headers=headers)

    response = client.delete("/api/admin/integrations/datadog", headers=headers)

    assert response.status_code == 200
    assert response.json()["secret"] == "removed"
    assert sero.deletes == [("ops@example.com", "apps/kobo/integrations/datadog")]
    assert sero.unsyncs[0][1:] == ("kobo", "liminal", "integration-datadog")
    assert client.get("/api/admin/integrations", headers=headers).json() == {"integrations": []}
    assert client.delete("/api/admin/integrations/datadog", headers=headers).status_code == 404


# --- per-vendor credential kinds (2026-10-25) ---------------------------------


class FakeAwsAdmin:
    """The gateway's admin plane as the AWS flow sees it: who the gateway is,
    and whether AWS lets it assume a role."""

    def __init__(self, *, refuse: str | None = None, identity_error: str | None = None):
        self.probes: list[tuple[str, str]] = []
        self._refuse = refuse
        self._identity_error = identity_error

    def aws_identity(self, audience: str) -> dict[str, str]:
        if self._identity_error:
            raise ClientError(self._identity_error)
        return {"issuer": "https://accounts.google.com", "subject": "1234567890", "email": "gw@p.iam.gserviceaccount.com", "audience": audience}

    def aws_probe(self, role_arn: str, audience: str) -> dict[str, str]:
        self.probes.append((role_arn, audience))
        if self._refuse:
            raise ClientError(self._refuse)
        return {"account": role_arn.split(":")[4], "arn": f"arn:aws:sts::{role_arn.split(':')[4]}:assumed-role/KoboAccess-Role/probe", "role_arn": role_arn}

    def list_grants(self):
        return []


REGISTRY = 'service_audience = "liminal"\n\n[tool_extras]\n\n[tool_extras."gateway.clusters"."dev/a"]\nproject_number = "1"\n'
AWS_ROLE = "arn:aws:iam::123456789012:role/KoboAccess-Role"


def test_an_aws_role_is_checked_with_aws_then_written_into_the_trust_registry(tmp_path, fake_storage):
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"registry.toml": REGISTRY, "targets.toml": TARGETS}, track_pushed_content=True)
    sero, admin = FakeSero(), FakeAwsAdmin()
    client, headers = _client(tmp_path, fake_storage, _config(tmp_path), github=github, sero=sero)
    client.app.state.clients.liminal_admin = admin
    body = {"vendor": "aws", "name": "AWS", "base_access": "read", "auth": "aws_role", "identity": {"role_arn": AWS_ROLE}}
    response = client.post("/api/admin/integrations", json=body, headers=headers)
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["credential_ref"] is None and sero.puts == [] and sero.syncs == []
    assert admin.probes == [(AWS_ROLE, "sts.amazonaws.com")]
    assert "AWS agreed" in row["state_detail"] and "123456789012" in row["state_detail"]
    assert row["spec"]["identity"] == {"role_arn": AWS_ROLE, "audience": "sts.amazonaws.com"}
    registry = github.pushed_file("registry.toml")
    assert '[tool_extras."aws.roles".aws]' in registry and f'role_arn = "{AWS_ROLE}"' in registry
    assert registry.startswith('service_audience = "liminal"') and '"gateway.clusters"' in registry, "the rest of the registry is kept"
    assert github.pushed_file("targets.toml") is None or 'name = "aws"' not in (github.pushed_file("targets.toml") or "")


def test_a_role_aws_refuses_is_not_written_anywhere(tmp_path, fake_storage):
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"registry.toml": REGISTRY}, track_pushed_content=True)
    client, headers = _client(tmp_path, fake_storage, _config(tmp_path), github=github, sero=FakeSero())
    client.app.state.clients.liminal_admin = FakeAwsAdmin(refuse="AWS refused the role: AccessDenied Not authorized to perform sts:AssumeRoleWithWebIdentity")
    body = {"vendor": "aws", "name": "AWS", "base_access": "read", "auth": "aws_role", "identity": {"role_arn": AWS_ROLE}}
    response = client.post("/api/admin/integrations", json=body, headers=headers)
    assert response.status_code == 409
    assert "AccessDenied" in response.json()["error"] and "connect again" in response.json()["error"]
    assert client.get("/api/admin/integrations", headers=headers).json()["integrations"] == []
    assert not github._pushed_trees, "nothing was pushed to the gateway repo"


def test_without_an_admin_plane_the_role_is_taken_at_its_word(tmp_path, fake_storage):
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"registry.toml": REGISTRY}, track_pushed_content=True)
    client, headers = _client(tmp_path, fake_storage, _config(tmp_path), github=github, sero=FakeSero())
    body = {"vendor": "aws", "name": "AWS", "base_access": "read", "auth": "aws_role", "identity": {"role_arn": AWS_ROLE}}
    row = client.post("/api/admin/integrations", json=body, headers=headers).json()
    assert "not checked with AWS" in row["state_detail"] and "pull request" in row["state_detail"]


def test_the_setup_page_renders_the_role_three_ways_around_the_gateways_identity(tmp_path, fake_storage):
    client, headers = _client(tmp_path, fake_storage, _config(tmp_path), github=FakeGitHub(existing_repos={GATEWAY_REPO}), sero=FakeSero())
    client.app.state.clients.liminal_admin = FakeAwsAdmin()
    body = client.get("/api/admin/integrations/aws/setup?packs=logs,bogus", headers=headers).json()
    assert body["identity"]["subject"] == "1234567890" and body["checked"] is True
    assert body["chosen"] == ["inventory", "logs"] and body["version"].startswith("v0.")
    assert {p["id"] for p in body["packs"]} == {"inventory", "logs", "security-audit", "cost"}
    assert 'gateway_service_account_unique_id = "1234567890"' in body["terraform"] and 'packs                             = ["inventory", "logs"]' in body["terraform"]
    assert "cloud-access.git//aws/terraform?ref=v0." in body["terraform"]
    assert body["cloudformation"]["parameters"] == {
        "IntegrationId": "aws", "GatewayServiceAccountUniqueId": "1234567890", "Audience": "sts.amazonaws.com",
        "RoleName": "KoboAccess-Role", "SecurityAudit": "Disabled", "Cost": "Disabled", "Logs": "Enabled",
    }
    assert '"RoleName"' in body["cloudformation"]["template"]
    assert '"accounts.google.com:sub": "1234567890"' in body["trust_policy"]
    # No admin plane: the page still renders, and says what to fill in.
    client.app.state.clients.liminal_admin = None
    body = client.get("/api/admin/integrations/aws/setup", headers=headers).json()
    assert body["identity"] is None and "by hand" in body["identity_note"] and "<room subject>" in body["trust_policy"]


def test_a_malformed_aws_role_is_refused_in_one_sentence(tmp_path, fake_storage):
    client, headers = _client(tmp_path, fake_storage, _config(tmp_path), github=FakeGitHub(existing_repos={GATEWAY_REPO}), sero=FakeSero())
    body = {"vendor": "aws", "name": "AWS", "base_access": "read", "auth": "aws_role", "identity": {"role_arn": "not-an-arn"}}
    response = client.post("/api/admin/integrations", json=body, headers=headers)
    assert response.status_code == 409
    assert "arn:aws:iam" in response.json()["error"]


def test_named_keys_are_stored_together_and_reach_the_gateway_as_one_env_each(tmp_path, fake_storage):
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"targets.toml": TARGETS}, track_pushed_content=True)
    sero = FakeSero()
    client, headers = _client(tmp_path, fake_storage, _config(tmp_path), github=github, sero=sero)
    body = {
        "vendor": "datadog", "name": "Datadog", "base_access": "read", "auth": "api_keys",
        "secrets": {"API_KEY": "dd-api", "APP_KEY": "dd-app"},
    }
    response = client.post("/api/admin/integrations", json=body, headers=headers)
    assert response.status_code == 201, response.text
    assert sero.puts == [("ops@example.com", "apps/kobo/integrations/datadog", {"API_KEY": "dd-api", "APP_KEY": "dd-app"})]
    targets = github.pushed_file("targets.toml")
    assert 'auth_method = "api_keys"' in targets
    assert 'api_key_env = "INTEGRATION_DATADOG_API_KEY"' in targets and 'app_key_env = "INTEGRATION_DATADOG_APP_KEY"' in targets
    assert "dd-api" not in targets and "dd-app" not in targets


def test_gcp_workload_identity_needs_nothing_and_a_service_account_must_be_an_email(tmp_path, fake_storage):
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"targets.toml": TARGETS}, track_pushed_content=True)
    client, headers = _client(tmp_path, fake_storage, _config(tmp_path), github=github, sero=FakeSero())
    ok = client.post("/api/admin/integrations", json={"vendor": "gcp", "name": "Google Cloud", "base_access": "read", "auth": "gcp_identity"}, headers=headers)
    assert ok.status_code == 201, ok.text
    assert 'auth_method = "gcp_identity"' in github.pushed_file("targets.toml")
    bad = client.post("/api/admin/integrations", json={"vendor": "gcp2", "name": "GCP two", "base_access": "read", "auth": "gcp_identity", "identity": {"service_account": "nope"}}, headers=headers)
    assert bad.status_code == 409 and "email" in bad.json()["error"]


# ---- with the permission service on (Access through bab, 2026-10-29) -----------------


def _access_client(tmp_path, fake_storage, *, github=None, bab=None):
    client, headers = _client(tmp_path, fake_storage, _config(tmp_path, bab_url="http://bab.bab.svc:8080"),
                              github=github or FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"registry.toml": REGISTRY}, track_pushed_content=True),
                              sero=FakeSero())
    client.app.state.clients.bab = bab or FakeBab(integrations=[])
    return client, headers


def test_with_access_on_the_setup_page_names_the_permission_service_as_the_principal(tmp_path, fake_storage):
    client, headers = _access_client(tmp_path, fake_storage)
    page = client.get("/api/admin/integrations/aws/setup", headers=headers).json()
    assert page["principal"] == "access" and page["checked"] is True
    assert page["identity"]["subject"] == "102000000000000000000"
    assert "102000000000000000000" in page["terraform"] and "102000000000000000000" in page["trust_policy"]


def test_with_access_on_an_aws_role_is_probed_by_bab_and_connected_there_not_on_the_gateway(tmp_path, fake_storage):
    bab = FakeBab(integrations=[])
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"registry.toml": REGISTRY}, track_pushed_content=True)
    client, headers = _access_client(tmp_path, fake_storage, github=github, bab=bab)
    body = {"vendor": "aws", "name": "acme-audit", "base_access": "read", "auth": "aws_role", "identity": {"role_arn": AWS_ROLE}}
    row = client.post("/api/admin/integrations", json=body, headers=headers).json()
    assert bab.probes == [(AWS_ROLE, "sts.amazonaws.com")]
    assert row["state"] == "applied" and "the permission service assumed the role" in row["state_detail"]
    assert "connected in the permission service" in row["state_detail"]
    (integ,) = bab.integrations("x")
    assert integ["integration"] == row["id"] and integ["vendor"] == "aws"
    assert integ["spec"] == {"role_arn": AWS_ROLE, "audience": "sts.amazonaws.com", "name": "acme-audit"}
    # The ceiling is every read permission the catalogue offers for the vendor.
    assert integ["ceiling"] == ["aws_cloudtrail_lake_query", "aws_inventory_read"]
    assert not github._pushed_trees, "the gateway has nothing to learn: leases come from the permission service"
    # A refused role is saved nowhere.
    r = client.post("/api/admin/integrations", json={**body, "name": "bad", "identity": {"role_arn": "arn:aws:iam::123456789012:role/refuse-me"}}, headers=headers)
    assert r.status_code == 409 and "AccessDenied" in r.json()["error"]
    assert len(bab.integrations("x")) == 1
    # Disconnect takes it out of the permission service too.
    out = client.delete(f"/api/admin/integrations/{row['id']}", headers=headers).json()
    assert out["access"].startswith("removed")
    assert bab.integrations("x") == []


def test_connections_the_permission_service_holds_show_in_the_list_and_can_be_removed(tmp_path, fake_storage):
    bab = FakeBab()  # its default integration, connected straight into bab, never through the panel
    client, headers = _access_client(tmp_path, fake_storage, bab=bab)
    rows = client.get("/api/admin/integrations", headers=headers).json()["integrations"]
    (row,) = rows
    assert row["id"] == "aws-prod" and row["spec"]["source"] == "access" and row["state"] == "applied"
    assert "connected in the permission service" in row["state_detail"]
    out = client.delete("/api/admin/integrations/aws-prod", headers=headers).json()
    assert out["access"].startswith("removed") and bab.integrations("x") == []


HUB = "arn:aws:iam::999999999999:role/KoboAccess-Hub"
SPOKE = "arn:aws:iam::111111111111:role/KoboAccess-Role"


def test_an_organization_then_its_accounts_register_themselves(tmp_path, fake_storage):
    bab = FakeBab(integrations=[])
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"registry.toml": REGISTRY}, track_pushed_content=True)
    client, headers = _access_client(tmp_path, fake_storage, github=github, bab=bab)

    # The organization's set-up page: the hub role, trusting the room's identity (oaud, not aud),
    # and the one spoke template every member account gets, with the organization's external id.
    page = client.get("/api/admin/integrations/aws/setup?mode=org&integration=acme-aws", headers=headers).json()
    assert page["mode"] == "org" and page["role_name"] == "KoboAccess-Hub" and page["spoke_role_name"] == "KoboAccess-Role"
    assert 'room_service_account_unique_id = "102000000000000000000"' in page["terraform"]
    assert '"accounts.google.com:oaud": "sts.amazonaws.com"' in page["trust_policy"]
    assert "sts:AssumeRole" in page["permissions_policy"] and "organizations:ListAccounts" in page["permissions_policy"]
    assert page["external_id"] == "ext-acme-aws" and page["hub_role_arn"] is None
    assert 'external_id    = "ext-acme-aws"' in page["spoke_terraform"]
    assert "create-stack-set" in page["stackset"] and "ParameterKey=ExternalId,ParameterValue=ext-acme-aws" in page["stackset"]
    assert page["cloudformation"]["parameters"]["ExternalId"] == "ext-acme-aws"

    # Connect the organization: probed, a bab row of kind org with no ceiling and the external
    # id, then its accounts checked through the hub: the one carrying the role is connected.
    row = client.post("/api/admin/integrations", json={"vendor": "aws", "name": "acme-aws", "base_access": "read", "auth": "aws_hub",
                                                        "identity": {"hub_role_arn": HUB}}, headers=headers).json()
    assert row["id"] == "acme-aws" and row["state"] == "applied" and row["spec"]["kind"] == "org", row
    assert "1 account(s) carry the role and are connected, 1 do not yet" in row["state_detail"]
    assert bab.probes[0] == (HUB, "sts.amazonaws.com") and bab.discoveries == [("acme-aws", "KoboAccess-Role")]
    rows = {i["integration"]: i for i in bab.integrations("x")}
    assert rows["acme-aws"]["kind"] == "org" and rows["acme-aws"]["ceiling"] == [] and rows["acme-aws"]["hub_role_arn"] == HUB
    assert rows["acme-aws"]["spec"]["external_id"] == "ext-acme-aws"
    acct = rows["acme-prod"]
    assert acct["parent"] == "acme-aws" and acct["kind"] == "account" and acct["account_id"] == "111111111111"
    assert acct["spec"]["spoke_role_arn"] == "arn:aws:iam::111111111111:role/KoboAccess-Role"
    assert "external_id" not in acct["spec"], "a discovered account leases with the organization's external id"
    assert acct["ceiling"] == ["aws_cloudtrail_lake_query", "aws_inventory_read"]
    assert not github._pushed_trees, "nothing goes to the gateway: leases come from the permission service"

    # Now the page knows the hub, and the organization's accounts list one connected, one waiting.
    page = client.get("/api/admin/integrations/aws/setup?mode=org&integration=acme-aws", headers=headers).json()
    assert page["hub_role_arn"] == HUB and f'hub_role_arn   = "{HUB}"' in page["spoke_terraform"]
    accts = {a["id"]: a for a in client.get("/api/admin/integrations/acme-aws/accounts", headers=headers).json()["accounts"]}
    assert accts["111111111111"]["connected"] is True and accts["111111111111"]["integration"] == "acme-prod"
    assert accts["222222222222"]["connected"] is False
    panel = {r["id"]: r for r in client.get("/api/admin/integrations", headers=headers).json()["integrations"]}
    assert panel["acme-aws"]["spec"]["kind"] == "org" and panel["acme-prod"]["spec"]["parent"] == "acme-aws"
    assert panel["acme-prod"]["spec"]["account_id"] == "111111111111" and panel["acme-prod"]["state"] == "applied"

    # Check again later: the second account gained the role; it is connected, the first left alone.
    bab.reachable_accounts = {"111111111111", "222222222222"}
    found = client.post("/api/admin/integrations/acme-aws/accounts/check", headers=headers).json()
    assert [a["id"] for a in found["already"]] == ["111111111111"]
    assert [(a["id"], a["integration"]) for a in found["connected"]] == [("222222222222", "acme-data")]
    assert found["without_role"] == []
    bad = client.post("/api/admin/integrations/acme-prod/accounts/check", headers=headers)
    assert bad.status_code == 409

    # The picker offers both accounts under their organization; the organization itself never.
    picker = {v["id"]: v for v in client.get("/api/access/picker", headers=headers).json()["vendors"]}["aws"]
    assert picker["organizations"] == [{"id": "acme-aws", "name": "acme-aws"}]
    by_id = {a["id"]: a for a in picker["accounts"]}
    assert by_id["acme-prod"]["connected"] is True and by_id["acme-prod"]["organization"] == "acme-aws"
    assert by_id["acme-data"]["connected"] is True
    assert "acme-aws" not in by_id, "an organization itself is never offered for a lease"

    # An account's own set-up page still exists for a role made by hand: it names the organization's id.
    page = client.get("/api/admin/integrations/aws/setup?mode=account&integration=acme-x&parent=acme-aws&account_id=333333333333", headers=headers).json()
    assert page["external_id"] == "ext-acme-aws" and '"sts:ExternalId": "ext-acme-aws"' in page["trust_policy"]
    bad = client.get("/api/admin/integrations/aws/setup?mode=account&integration=x&parent=nope&account_id=111111111111", headers=headers)
    assert bad.status_code == 409