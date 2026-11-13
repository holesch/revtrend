import logging
import sys

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


def test_text_formatter():
    record = logging.makeLogRecord(
        {
            "name": "x",
            "levelno": logging.INFO,
            "msg": "ingested %d",
            "args": (3,),
            "submitted_by": "jenkins:job",
            "submitted_name": "Jane Doe",
            "metric": None,
        }
    )
    line = observability.TextFormatter().format(record)
    assert line == '<6>x: ingested 3 submitted_by=jenkins:job submitted_name="Jane Doe"'


def test_text_formatter_prefixes_traceback_lines():
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.makeLogRecord(
            {"name": "x", "levelno": logging.ERROR, "msg": "failed"}
        )
        record.exc_info = sys.exc_info()
    lines = observability.TextFormatter().format(record).splitlines()
    assert lines[0] == "<3>x: failed"
    assert lines[-1] == "<3>ValueError: boom"
    assert all(line.startswith("<3>") for line in lines)
