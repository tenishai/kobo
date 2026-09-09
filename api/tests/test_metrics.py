"""RED metrics: every request lands in http_requests_total, /metrics serves them."""


def test_requests_land_in_the_red_series(client):
    client.get("/healthz")
    body = client.get("/metrics").text
    # The observability layer's web profile queries these exact names.
    assert 'http_requests_total{method="GET"' in body
    assert "http_request_duration_seconds_bucket" in body


def test_metrics_endpoint_does_not_count_itself(client):
    client.get("/metrics")
    body = client.get("/metrics").text
    assert 'http_requests_total{method="GET",status="200"}' not in body
