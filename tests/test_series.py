import conftest
import pytest

import revtrend.api as api

URL = "/api/v1/projects/test/series"


@pytest.fixture
def data(client, graph):
    conftest.push(client, graph["c1"], 10)
    conftest.push(client, graph["c1"], 99, machine="arm64")
    conftest.push(client, graph["c2"], 20, headers=client.as_user("writer"))
    conftest.push(client, graph["a1"], 15)
    conftest.push(client, graph["m1"], 30)
    conftest.push(client, graph["c3"], 40, run_id="r1")
    conftest.push(client, graph["c3"], 42, run_id="r2")
    conftest.push(client, graph["c3"], 44, run_id="r3")
    conftest.push(
        client, graph["c3"], 1000, run_id="manual", headers=client.as_user("writer")
    )
    conftest.push(client, graph["c3"], 5, metric="test.duration_seconds")
    conftest.push(client, graph["x2"], 50)
    return graph


def get(client, **params):
    response = client.get(URL, params=params, headers=client.as_user("reader"))
    assert response.status_code == 200, response.text
    return response.json()


def test_default_branch(client, data):
    rows = get(client, metric="image.size_bytes", labels="machine:amd64")
    assert [(r["short"], r["value"]) for r in rows] == [
        (data["c1"][:8], 10),
        (data["c2"][:8], 20),
        (data["m1"][:8], 30),
        (data["c3"][:8], 42),
    ]
    tip = rows[-1]
    assert tip["idx"] == 0
    assert tip["runs"] == 3
    assert tip["source"] == "ci"
    assert tip["subject"] == "c3"
    assert tip["series"] == "image.size_bytes{machine=amd64}"
    assert tip["labels"] == {"machine": "amd64"}
    assert tip["on_branch"] is False
    assert rows[1]["source"] == "manual"
    assert rows[0]["time"] < rows[1]["time"]


def test_feature_branch_includes_ancestors(client, data):
    rows = get(
        client, metric="image.size_bytes", labels="machine:amd64", ref="feature/x"
    )
    assert [(r["value"], r["on_branch"]) for r in rows] == [
        (10, False),
        (20, False),
        (30, False),
        (42, False),
        (50, True),
    ]


def test_base_release_branch(client, data):
    rows = get(
        client,
        metric="image.size_bytes",
        labels="machine:amd64",
        ref="feature/x",
        base="release/1.0",
    )
    assert [(r["value"], r["on_branch"]) for r in rows] == [
        (10, False),
        (20, False),
        (30, True),
        (42, True),
        (50, True),
    ]


@pytest.mark.parametrize(
    ("agg", "value"),
    [("median", 42), ("mean", 42), ("min", 40), ("max", 44), ("latest", 44)],
)
def test_aggregations(client, data, agg, value):
    rows = get(
        client,
        metric="image.size_bytes",
        labels="machine:amd64",
        ref=data["c3"],
        agg=agg,
    )
    assert rows[-1]["value"] == value


def test_multiple_metrics_and_label_values(client, data):
    rows = get(client, metric="image.size_bytes,test.duration_seconds", ref=data["c3"])
    assert {r["series"] for r in rows} == {
        "image.size_bytes{machine=amd64}",
        "image.size_bytes{machine=arm64}",
        "test.duration_seconds{machine=amd64}",
    }
    rows = get(
        client, metric="image.size_bytes", labels=["machine:amd64", "machine:arm64"]
    )
    assert len(rows) == 5


def test_time_range(client, data):
    rows = get(client, metric="image.size_bytes", labels="machine:amd64")
    from_ms, to_ms = rows[1]["time"], rows[2]["time"]
    rows = get(
        client,
        metric="image.size_bytes",
        labels="machine:amd64",
        **{"from": from_ms, "to": to_ms},
    )
    assert [r["value"] for r in rows] == [20, 30]


def test_unknown_ref(client):
    response = client.get(
        URL, params={"metric": "x", "ref": "nope"}, headers=client.as_user("reader")
    )
    assert response.status_code == 404


def test_new_branch_triggers_fetch(client, remote, data, monkeypatch):
    monkeypatch.setattr(api, "ON_DEMAND_FETCH_INTERVAL", 0)
    remote.git("branch", "feature/new", data["c3"])
    rows = get(
        client, metric="image.size_bytes", labels="machine:amd64", ref="feature/new"
    )
    assert len(rows) == 4


@pytest.mark.parametrize(
    "params",
    [
        {"metric": "Bad"},
        {"metric": "x", "labels": "nocolon"},
        {"metric": "x", "agg": "sum"},
        {},
    ],
)
def test_invalid_params(client, params):
    response = client.get(URL, params=params, headers=client.as_user("reader"))
    assert response.status_code == 400


def test_requires_reader(client):
    response = client.get(
        URL, params={"metric": "x"}, headers=client.as_ci(job="job/other")
    )
    assert response.status_code == 403
