"""RED metrics over every request, prometheus-client exposition at /metrics.

The observability layer's web profile queries these exact series names
(http_requests_total by status, http_request_duration_seconds_bucket) - a panel
contract, not a naming choice. Labels stay low-cardinality on purpose: method and
status only, never path.
"""

import time

from fastapi import FastAPI, Request
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)


def install(app: FastAPI) -> None:
    # Per-app registry: create_app() runs once per test, and prometheus-client's
    # process-global default registry would raise Duplicated timeseries on the
    # second one.
    registry = CollectorRegistry()
    requests = Counter(
        "http_requests",  # exposition name: http_requests_total
        "HTTP requests served",
        ["method", "status"],
        registry=registry,
    )
    latency = Histogram(
        "http_request_duration_seconds",
        "HTTP request latency",
        ["method"],
        registry=registry,
    )

    @app.middleware("http")
    async def _record(request: Request, call_next):
        if request.url.path == "/metrics":
            return await call_next(request)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # An unhandled exception never produces a response object, so
            # without this the crash is invisible to http_requests_total -
            # and a crashing app looks healthy to any 5xx alert (found in
            # review of the gojira twin, gojira #44). Count it as the 500
            # the client receives.
            latency.labels(request.method).observe(time.perf_counter() - start)
            requests.labels(request.method, "500").inc()
            raise
        latency.labels(request.method).observe(time.perf_counter() - start)
        requests.labels(request.method, str(response.status_code)).inc()
        return response

    # An explicit route, not a mount: a mount only PARTIALLY matches the bare
    # /metrics path, and Starlette prefers any later full match - a SPA
    # catch-all route swallows the mount whole (found live on kobo).
    @app.get("/metrics", include_in_schema=False)
    def _metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
