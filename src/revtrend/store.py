import dataclasses
import json
import statistics


class ConflictError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Run:
    project_id: int
    sha: str
    run_id: str
    source: str
    submitted_by: str
    measured_at: int


def canonical_labels(labels):
    return json.dumps(labels, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def insert_measurements(conn, run, metrics):
    """Inserts (name, labels, value) tuples of one run. Returns the number of new rows.

    Re-submitting identical values is a no-op; different values for an
    existing (sha, metric, labels, run_id) raise ConflictError.
    """
    created = 0
    for name, labels, value in metrics:
        labels = canonical_labels(labels)
        cursor = conn.execute(
            "INSERT INTO measurement"
            " (project_id, sha, metric, labels, value, run_id, source, submitted_by, measured_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT DO NOTHING",
            (
                run.project_id,
                run.sha,
                name,
                labels,
                value,
                run.run_id,
                run.source,
                run.submitted_by,
                run.measured_at,
            ),
        )
        if cursor.rowcount:
            created += 1
            continue
        existing = conn.execute(
            "SELECT value FROM measurement WHERE project_id = ? AND sha = ? AND metric = ? AND labels = ? AND run_id = ?",
            (run.project_id, run.sha, name, labels, run.run_id),
        ).fetchone()["value"]
        if existing != value:
            raise ConflictError(
                f"run {run.run_id!r} already has {name}{labels} = {existing}, got {value}"
            )
    return created


@dataclasses.dataclass(frozen=True)
class Point:
    value: float
    runs: int
    source: str


def measurements(conn, project_id, shas, metrics=None, label_filters=None):
    """Measurements of the given commits, optionally filtered.

    `label_filters` maps label key to accepted values: values of one key are
    alternatives, different keys must all match.
    """
    sql = (
        "SELECT id, sha, metric, labels, value, source, measured_at FROM measurement"
        " WHERE project_id = ? AND sha IN (SELECT value FROM json_each(?))"
    )
    params = [project_id, json.dumps(list(shas))]
    if metrics:
        sql += " AND metric IN (SELECT value FROM json_each(?))"
        params.append(json.dumps(list(metrics)))
    for key, values in (label_filters or {}).items():
        sql += " AND json_extract(labels, ?) IN (SELECT value FROM json_each(?))"
        params.extend([f"$.{key}", json.dumps(list(values))])
    return conn.execute(sql, params).fetchall()


def aggregate(rows, agg):
    """Aggregates runs per (sha, metric, labels).

    CI runs take precedence: manual runs are only used for series/commits
    without any CI run.
    """
    groups = {}
    for row in rows:
        groups.setdefault((row["sha"], row["metric"], row["labels"]), []).append(row)
    points = {}
    for key, group in groups.items():
        ci = [r for r in group if r["source"] == "ci"]
        used = ci or group
        points[key] = Point(
            value=AGGREGATIONS[agg](used), runs=len(used), source=used[0]["source"]
        )
    return points


def _latest(rows):
    return max(rows, key=lambda r: (r["measured_at"], r["id"]))["value"]


def _values(fn):
    return lambda rows: fn([r["value"] for r in rows])


AGGREGATIONS = {
    "median": _values(statistics.median),
    "mean": _values(statistics.fmean),
    "min": _values(min),
    "max": _values(max),
    "latest": _latest,
}
