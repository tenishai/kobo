def test_healthz(client):
    response = client.get("/api/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_catalog_models(client, config):
    response = client.get("/api/catalog/models")

    assert response.status_code == 200
    body = response.json()
    assert body["default"] == config.models[0].id
    first = config.models[0]
    assert {
        "id": first.id, "label": first.label, "family": first.family, "tier": first.tier,
    } in body["models"]
