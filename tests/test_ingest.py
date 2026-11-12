import pytest

URL = "/api/v1/projects/test/measurements"


def body(sha, run_id="run-1", value=100.0, **extra):
    return {
        "sha": sha,
        "run_id": run_id,
        "metrics": [
            {
                "name": "image.size_bytes",
                "value": value,
                "labels": {"machine": "amd64", "image": "base"},
            },
            {"name": "test.duration_seconds", "value": 4.5},
        ],
        **extra,
    }


def rows(client):
    conn = client.app.state.revtrend.db._writer
    return [dict(r) for r in conn.execute("SELECT * FROM measurement ORDER BY id")]


def test_ingest(client, graph):
    response = client.post(URL, json=body(graph["c3"]), headers=client.as_ci())
    assert response.status_code == 201
    assert response.json() == {"created": 2, "existing": 0}
    first, second = rows(client)
    assert first["labels"] == '{"image":"base","machine":"amd64"}'
    assert first["source"] == "ci"
    assert first["submitted_by"] == "jenkins:job/test/master"
    assert second["labels"] == "{}"


def test_replay_is_idempotent(client, graph):
    client.post(URL, json=body(graph["c3"]), headers=client.as_ci())
    response = client.post(URL, json=body(graph["c3"]), headers=client.as_ci())
    assert response.status_code == 200
    assert response.json() == {"created": 0, "existing": 2}
    assert len(rows(client)) == 2


def test_replay_with_different_value(client, graph):
    client.post(URL, json=body(graph["c3"]), headers=client.as_ci())
    response = client.post(
        URL, json=body(graph["c3"], value=101), headers=client.as_ci()
    )
    assert response.status_code == 409
    assert len(rows(client)) == 2


def test_manual_source_for_users(client, graph):
    response = client.post(
        URL, json=body(graph["c3"]), headers=client.as_user("writer")
    )
    assert response.status_code == 201
    assert {r["source"] for r in rows(client)} == {"manual"}
    assert rows(client)[0]["submitted_by"] == "entra:alice"
    assert rows(client)[0]["submitted_name"] is None


def test_submitted_name_from_display_claim(client, graph):
    token = client.entra.token(
        "client-id",
        subject="alice",
        roles=["writer"],
        preferred_username="alice@example.com",
    )
    response = client.post(
        URL, json=body(graph["c3"]), headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 201
    assert {r["submitted_name"] for r in rows(client)} == {"alice@example.com"}


def test_no_display_claims_for_ci(client, graph):
    client.post(URL, json=body(graph["c3"]), headers=client.as_ci())
    assert {r["submitted_name"] for r in rows(client)} == {None}


def test_source_field_rejected(client, graph):
    response = client.post(
        URL, json=body(graph["c3"], source="ci"), headers=client.as_user("writer")
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "metric",
    [
        {"name": "Image.Size", "value": 1},
        {"name": "x", "value": "NaN"},
        {"name": "x", "value": 1, "labels": {"Bad-Key": "v"}},
        {"name": "x", "value": 1, "labels": {"k": "v" * 300}},
    ],
)
def test_invalid_metric(client, graph, metric):
    response = client.post(
        URL,
        json={"sha": graph["c3"], "run_id": "r", "metrics": [metric]},
        headers=client.as_ci(),
    )
    assert response.status_code == 400


def test_duplicate_series_rejected(client, graph):
    metrics = [
        {"name": "x", "value": 1, "labels": {"a": "1"}},
        {"name": "x", "value": 2, "labels": {"a": "1"}},
    ]
    response = client.post(
        URL,
        json={"sha": graph["c3"], "run_id": "r", "metrics": metrics},
        headers=client.as_ci(),
    )
    assert response.status_code == 400


def test_short_sha_rejected(client, graph):
    response = client.post(URL, json=body(graph["c3"][:12]), headers=client.as_ci())
    assert response.status_code == 400


def test_unknown_commit(client):
    response = client.post(URL, json=body("0" * 40), headers=client.as_ci())
    assert response.status_code == 409


def test_new_commit_triggers_fetch(client, remote):
    sha = remote.commit("new")
    response = client.post(URL, json=body(sha), headers=client.as_ci())
    assert response.status_code == 201


def test_unauthenticated(client, graph):
    assert client.post(URL, json=body(graph["c3"])).status_code == 401
    response = client.post(
        URL, json=body(graph["c3"]), headers={"Authorization": "Bearer garbage"}
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


def test_reader_cannot_write(client, graph):
    response = client.post(
        URL, json=body(graph["c3"]), headers=client.as_user("reader")
    )
    assert response.status_code == 403


def test_other_jenkins_job_cannot_write(client, graph):
    response = client.post(
        URL, json=body(graph["c3"]), headers=client.as_ci(job="job/other/master")
    )
    assert response.status_code == 403


def test_unknown_project(client, graph):
    response = client.post(
        "/api/v1/projects/nope/measurements",
        json=body(graph["c3"]),
        headers=client.as_ci(),
    )
    assert response.status_code == 404
