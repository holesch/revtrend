CREATE TABLE project (
    id              INTEGER PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    default_branch  TEXT
);

CREATE TABLE measurement (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES project(id),
    sha          TEXT NOT NULL,
    metric       TEXT NOT NULL,
    labels       TEXT NOT NULL DEFAULT '{}',
    value        REAL NOT NULL,
    run_id       TEXT NOT NULL,
    source       TEXT NOT NULL CHECK (source IN ('ci', 'manual')),
    submitted_by TEXT NOT NULL,
    measured_at  INTEGER NOT NULL,
    UNIQUE (project_id, sha, metric, labels, run_id)
);

CREATE INDEX measurement_lookup ON measurement (project_id, metric, sha);
