import time

import conftest
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
    conftest.push(client, graph["c2"], 1, run_id="other")
    response = client.get(
        "/api/v1/projects/test/metrics", headers=client.as_user("reader")
    )
    result = response.json()
    for metric in result:
        assert abs(metric.pop("last_measured_at") / 1000 - time.time()) < 60
    assert result == [
        {
            "name": "image.size_bytes",
            "labels": {"image": ["a"], "machine": ["amd64", "arm64"]},
            "series": 3,
            "commits": 2,
            "values": 3,
        },
        {
            "name": "test.duration_seconds",
            "labels": {},
            "series": 1,
            "commits": 1,
            "values": 1,
        },
    ]


def get_runs(client, **params):
    return client.get(
        "/api/v1/projects/test/runs", params=params, headers=client.as_user("reader")
    )


def test_runs(client, graph):
    conftest.push(client, graph["c2"], 1, run_id="first")
    conftest.push(client, graph["c3"], 2, run_id="second")
    conftest.push(client, graph["c3"], 3, run_id="second", metric="image.files")
    conftest.push(
        client, graph["c3"], 4, run_id="manual", headers=client.as_user("writer")
    )
    result = get_runs(client).json()
    for run in result:
        assert abs(run.pop("measured_at") / 1000 - time.time()) < 60
    assert result == [
        {
            "run_id": "manual",
            "sha": graph["c3"],
            "short": graph["c3"][:8],
            "source": "manual",
            "submitted_by": "entra:alice",
            "submitted_name": None,
            "values": 1,
        },
        {
            "run_id": "second",
            "sha": graph["c3"],
            "short": graph["c3"][:8],
            "source": "ci",
            "submitted_by": "jenkins:job/test/master",
            "submitted_name": None,
            "values": 2,
        },
        {
            "run_id": "first",
            "sha": graph["c2"],
            "short": graph["c2"][:8],
            "source": "ci",
            "submitted_by": "jenkins:job/test/master",
            "submitted_name": None,
            "values": 1,
        },
    ]


def test_runs_filtered(client, graph):
    conftest.push(client, graph["c2"], 1, run_id="first")
    conftest.push(client, graph["c3"], 2, run_id="second")
    conftest.push(client, graph["c3"], 3, run_id="third")
    runs = get_runs(client, sha=graph["c2"]).json()
    assert [r["run_id"] for r in runs] == ["first"]
    runs = get_runs(client, limit=2).json()
    assert [r["run_id"] for r in runs] == ["third", "second"]


@pytest.mark.parametrize(
    "params", [{"sha": "master"}, {"limit": 0}, {"limit": api.MAX_RUNS + 1}]
)
def test_runs_invalid(client, params):
    assert get_runs(client, **params).status_code == 400


def test_runs_requires_reader(client):
    response = client.get("/api/v1/projects/test/runs", headers=client.as_user())
    assert response.status_code == 403
