import time


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "projects": {"test": {"ready": True}}}


def test_default_branch_stored(client):
    db = client.app.state.revtrend.db
    deadline = time.monotonic() + 5
    while (
        db._writer.execute("SELECT default_branch FROM project").fetchone()[
            "default_branch"
        ]
        != "master"
    ):
        assert time.monotonic() < deadline
        time.sleep(0.02)
