import time

import pytest

import revtrend.api as api


def test_projects(client, remote):
    response = client.get("/api/v1/projects", headers=client.as_user("reader"))
    assert response.json() == [
        {"name": "test", "default_branch": "master", "remote_url": remote.url}
    ]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://git.example/scm/proj/repo.git",
            "https://git.example/scm/proj/repo.git",
        ),
        ("https://user:secret@git.example/repo.git", "https://git.example/repo.git"),
        (
            "ssh://git@git.example:7999/proj/repo.git",
            "ssh://git.example:7999/proj/repo.git",
        ),
        ("git@git.example:proj/repo.git", "git@git.example:proj/repo.git"),
    ],
)
def test_public_remote_url(url, expected):
    assert api.public_remote_url(url) == expected


def test_projects_filtered_by_role(client):
    response = client.get("/api/v1/projects", headers=client.as_user())
    assert response.json() == []


def test_refs(client, graph):
    response = client.get(
        "/api/v1/projects/test/refs", headers=client.as_user("reader")
    )
    refs = response.json()
    assert [r["name"] for r in refs] == [
        "master",
        "feature/x",
        "release/1.0",
        "feature/a",
    ]
    assert refs[0]["default"] is True
    assert refs[0]["sha"] == graph["c3"]
    assert not any(r["default"] for r in refs[1:])


def test_refs_match(client):
    response = client.get(
        "/api/v1/projects/test/refs",
        params={"match": "feature/*"},
        headers=client.as_user("reader"),
    )
    assert [r["name"] for r in response.json()] == ["feature/x", "feature/a"]


def test_refs_active_since_keeps_default(client, remote):
    # commit times of the test repo are in 2023
    age = int(time.time()) - remote.time + 86400
    params = {"active_since": f"{age}s"}
    response = client.get(
        "/api/v1/projects/test/refs", params=params, headers=client.as_user("reader")
    )
    assert len(response.json()) == 4
    response = client.get(
        "/api/v1/projects/test/refs",
        params={"active_since": "1d"},
        headers=client.as_user("reader"),
    )
    assert [r["name"] for r in response.json()] == ["master"]


@pytest.mark.parametrize("value", ["90", "1w", "-1d"])
def test_refs_invalid_active_since(client, value):
    response = client.get(
        "/api/v1/projects/test/refs",
        params={"active_since": value},
        headers=client.as_user("reader"),
    )
    assert response.status_code == 400


def test_metrics(client, graph):
    body = {
        "sha": graph["c3"],
        "run_id": "r",
        "metrics": [
            {
                "name": "image.size_bytes",
                "value": 1,
                "labels": {"machine": "amd64", "image": "a"},
            },
            {
                "name": "image.size_bytes",
                "value": 1,
                "labels": {"machine": "arm64", "image": "a"},
            },
            {"name": "test.duration_seconds", "value": 1},
        ],
    }
    client.post("/api/v1/projects/test/measurements", json=body, headers=client.as_ci())
    response = client.get(
        "/api/v1/projects/test/metrics", headers=client.as_user("reader")
    )
    assert response.json() == [
        {
            "name": "image.size_bytes",
            "labels": {"image": ["a"], "machine": ["amd64", "arm64"]},
        },
        {"name": "test.duration_seconds", "labels": {}},
    ]
