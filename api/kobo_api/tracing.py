"""OTLP tracing, env-gated: spans flow only where a collector is declared.

`OTEL_EXPORTER_OTLP_ENDPOINT` (the standard variable, set by the deployment)
turns this on; unset - every test, local run, and un-wired deploy - it is a
no-op and the app carries zero tracing overhead. Probe and scrape paths are
excluded so the trace stream is requests people made, not kubelet noise.
"""

import os

from fastapi import FastAPI

_EXCLUDED_URLS = "healthz,metrics"


def install(app: FastAPI, service_name: str = "kobo") -> None:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=provider, excluded_urls=_EXCLUDED_URLS
    )
