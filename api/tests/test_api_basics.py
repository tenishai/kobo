def test_healthz(client):
    response = client.get("/api/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_catalog_models(client, config):
    response = client.get("/api/catalog/models")

    assert response.status_code == 200
    body = response.json()
    assert body["default"] == config.models[0].id
    assert {"id": config.models[0].id, "label": config.models[0].label} in body["models"]
