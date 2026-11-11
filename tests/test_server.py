import fastapi.testclient

import revtrend.server as server


def test_healthz():
    client = fastapi.testclient.TestClient(server.app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
