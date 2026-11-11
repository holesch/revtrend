import dataclasses
import json


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
