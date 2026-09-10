"""Tracing is env-gated: absent the endpoint it is a no-op, present it instruments."""

from kobo_api import tracing


def test_no_endpoint_is_a_noop(monkeypatch):
    # The default test environment has no OTEL endpoint - every other test in
    # this suite already proves the app works untraced. Belt and suspenders:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    from fastapi import FastAPI

    app = FastAPI()
    tracing.install(app)
    assert not getattr(app, "_is_instrumented_by_opentelemetry", False)


def test_endpoint_instruments_the_app(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:1")
    from fastapi import FastAPI

    app = FastAPI()
    tracing.install(app)
    # FastAPIInstrumentor stamps the app; that is the observable effect.
    assert getattr(app, "_is_instrumented_by_opentelemetry", False)
