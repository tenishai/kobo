"""The build-log scrubber (issue #200): well-known token shapes and values after
secret-ish keys are replaced, ordinary log lines are left alone."""

from kobo_api.redact import REDACTED, redact_secrets


def test_vendor_token_shapes_are_redacted():
    text = (
        "pushing with ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345 done\n"
        "slack xoxb-1234567890-abcdefghij ok\n"
        "aws AKIAABCDEFGHIJKLMNOP\n"
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZ2VudCJ9.c2lnbmF0dXJlLXNpZ25hdHVyZQ\n"
    )
    out = redact_secrets(text)
    assert "ghp_" not in out and "xoxb-" not in out and "AKIA" not in out and "eyJ" not in out
    assert out.count(REDACTED) == 4
    assert out.startswith("pushing with [redacted] done")


def test_values_after_secret_keys_and_bearers_are_redacted_but_the_key_stays():
    out = redact_secrets('GITHUB_TOKEN=abcdefgh12345678 API_KEY: "s3cr3tvalue99" Authorization: Bearer abcdefghijklmnop.qrst')
    assert out == f'GITHUB_TOKEN={REDACTED} API_KEY: "{REDACTED}" Authorization: Bearer {REDACTED}'


def test_private_key_blocks_go_as_one():
    out = redact_secrets("before\n-----BEGIN RSA PRIVATE KEY-----\nMIIE\nxyz\n-----END RSA PRIVATE KEY-----\nafter")
    assert out == f"before\n{REDACTED}\nafter"


def test_ordinary_lines_are_untouched():
    text = "Step 3/7 : RUN uv sync\n ---> Using cache\nSuccessfully built 0a1b2c3d\nports: 8080, token count 12\n"
    assert redact_secrets(text) == text
