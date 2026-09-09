"""RED metrics over every request, prometheus-client exposition at /metrics.

The observability layer's web profile queries these exact series names
(http_requests_total by status, http_request_duration_seconds_bucket) - a panel
contract, not a naming choice. Labels stay low-cardinality on purpose: method and
status only, never path.
"""

import time

from fastapi import FastAPI, Request
from prometheus_client import CollectorRegistry, Counter, Histogram, make_asgi_app


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
        # The mount answers /metrics with a redirect to /metrics/ - skip both,
        # or the follow-up request counts the metrics endpoint itself.
        if request.url.path in ("/metrics", "/metrics/"):
            return await call_next(request)
        start = time.perf_counter()
        response = await call_next(request)
        latency.labels(request.method).observe(time.perf_counter() - start)
        requests.labels(request.method, str(response.status_code)).inc()
        return response

    app.mount("/metrics", make_asgi_app(registry=registry))
