import dataclasses

import conftest
import pytest

import revtrend.api as api

URL = "/api/v1/projects/test/compare"


def get(client, **params):
    response = client.get(URL, params=params, headers=client.as_user("reader"))
    assert response.status_code == 200, response.text
    return response.json()


def test_both(client, graph):
    conftest.push(client, graph["c3"], 40)
    conftest.push(client, graph["x2"], 50, run_id="r1")
    conftest.push(client, graph["x2"], 52, run_id="r2")
    result = get(client, head=graph["x2"])
    assert result["head"] == {"sha": graph["x2"], "short": graph["x2"][:8]}
    assert result["base"] == {
        "ref": "master",
        "tip_sha": graph["c3"],
        "fork_sha": graph["c3"],
    }
    assert result["results"] == [
        {
            "metric": "image.size_bytes",
            "labels": {"machine": "amd64"},
            "status": "both",
            "head": 51,
            "head_runs": 2,
            "baseline": 40,
            "baseline_sha": graph["c3"],
            "baseline_distance": 0,
            "base_tip": 40,
            "delta": 11,
            "delta_pct": 27.5,
        }
    ]


def test_baseline_walks_back(client, graph):
    conftest.push(client, graph["c2"], 20)
    conftest.push(client, graph["x2"], 50)
    (result,) = get(client, head=graph["x2"])["results"]
    assert result["baseline_sha"] == graph["c2"]
    assert result["baseline_distance"] == 2
    assert result["base_tip"] is None


def test_walk_limit(client, graph):
    project = client.app.state.revtrend.projects["test"]
    project.config = dataclasses.replace(project.config, compare_max_walk=1)
    conftest.push(client, graph["c2"], 20)
    conftest.push(client, graph["x2"], 50)
    (result,) = get(client, head=graph["x2"])["results"]
    assert result["status"] == "head_only"
    assert result["baseline"] is None
    assert result["delta"] is None


def test_base_only(client, graph):
    conftest.push(client, graph["c3"], 40, metric="old.metric")
    conftest.push(client, graph["x2"], 50)
    results = get(client, head=graph["x2"])["results"]
    assert [(r["metric"], r["status"]) for r in results] == [
        ("image.size_bytes", "head_only"),
        ("old.metric", "base_only"),
    ]
    assert results[1]["head"] is None
    assert results[1]["baseline"] == 40


def test_release_base_uses_fork_point(client, graph):
    conftest.push(client, graph["c2"], 20)
    conftest.push(client, graph["r1"], 25)
    conftest.push(client, graph["x2"], 50)
    result = get(client, head=graph["x2"], base="release/1.0")
    assert result["base"] == {
        "ref": "release/1.0",
        "tip_sha": graph["r1"],
        "fork_sha": graph["c2"],
    }
    (series,) = result["results"]
    assert series["baseline"] == 20
    assert series["base_tip"] == 25


def test_filters(client, graph):
    conftest.push(client, graph["c3"], 40)
    conftest.push(client, graph["c3"], 41, machine="arm64")
    conftest.push(client, graph["c3"], 1, metric="other")
    conftest.push(client, graph["x2"], 50)
    results = get(
        client, head=graph["x2"], metric="image.size_bytes", labels="machine:amd64"
    )["results"]
    assert [(r["metric"], r["labels"]) for r in results] == [
        ("image.size_bytes", {"machine": "amd64"})
    ]


def test_zero_baseline(client, graph):
    conftest.push(client, graph["c3"], 0)
    conftest.push(client, graph["x2"], 5)
    (result,) = get(client, head=graph["x2"])["results"]
    assert result["delta"] == 5
    assert result["delta_pct"] is None


@pytest.mark.parametrize("head", ["master", "abc123"])
def test_head_must_be_full_sha(client, head):
    response = client.get(URL, params={"head": head}, headers=client.as_user("reader"))
    assert response.status_code == 400


def test_unknown_head(client):
    response = client.get(
        URL, params={"head": "0" * 40}, headers=client.as_user("reader")
    )
    assert response.status_code == 404


def test_no_merge_base(client, remote, graph, monkeypatch):
    monkeypatch.setattr(api, "ON_DEMAND_FETCH_INTERVAL", 0)
    remote.git("checkout", "--quiet", "--orphan", "unrelated")
    remote.commit("orphan")
    response = client.get(
        URL,
        params={"head": graph["x2"], "base": "unrelated"},
        headers=client.as_user("reader"),
    )
    assert response.status_code == 409
