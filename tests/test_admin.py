import conftest

URL = "/api/v1/projects/test/measurements"


def count(client):
    return client.app.state.revtrend.db._writer.execute(
        "SELECT count(*) FROM measurement"
    ).fetchone()[0]


def setup_data(client, graph):
    conftest.push(client, graph["c3"], 1, run_id="good")
    conftest.push(client, graph["c3"], 2, run_id="bad")
    conftest.push(client, graph["c3"], 3, run_id="good", metric="old.name")


def test_delete_run(client, graph):
    setup_data(client, graph)
    response = client.delete(
        URL, params={"run_id": "bad"}, headers=client.as_user("admin")
    )
    assert response.json() == {"deleted": 1}
    assert count(client) == 2


def test_delete_metric(client, graph):
    setup_data(client, graph)
    response = client.delete(
        URL, params={"metric": "old.name"}, headers=client.as_user("admin")
    )
    assert response.json() == {"deleted": 1}
    assert count(client) == 2


def test_delete_requires_filter(client, graph):
    setup_data(client, graph)
    assert client.delete(URL, headers=client.as_user("admin")).status_code == 400
    assert count(client) == 3


def test_delete_requires_admin(client, graph):
    setup_data(client, graph)
    response = client.delete(
        URL, params={"run_id": "bad"}, headers=client.as_user("writer")
    )
    assert response.status_code == 403
    assert count(client) == 3
