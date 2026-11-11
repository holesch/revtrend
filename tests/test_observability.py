import json
import logging

import revtrend.observability as observability


def test_prometheus_metrics(client, graph):
    body = {"sha": graph["c3"], "run_id": "r", "metrics": [{"name": "x", "value": 1}]}
    client.post("/api/v1/projects/test/measurements", json=body, headers=client.as_ci())
    text = client.get("/metrics").text
    assert (
        'revtrend_http_requests_total{method="POST",route="/api/v1/projects/{project}/measurements",status="201"}'
        in text
    )
    assert 'revtrend_ingested_measurements_total{project="test",source="ci"}' in text


def test_fetch_metrics(client, remote):
    sha = remote.commit("new")
    body = {"sha": sha, "run_id": "r", "metrics": [{"name": "x", "value": 1}]}
    client.post("/api/v1/projects/test/measurements", json=body, headers=client.as_ci())
    assert (
        'revtrend_fetch_duration_seconds_count{project="test"}'
        in client.get("/metrics").text
    )


def test_json_formatter():
    record = logging.makeLogRecord(
        {
            "name": "x",
            "levelname": "INFO",
            "msg": "ingested %d",
            "args": (3,),
            "submitted_by": "jenkins:job",
        }
    )
    entry = json.loads(observability.JsonFormatter().format(record))
    assert entry["message"] == "ingested 3"
    assert entry["submitted_by"] == "jenkins:job"
    assert entry["level"] == "INFO"
    assert "args" not in entry
