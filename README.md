# revtrend

Track numeric metrics per git commit (image size, binary size, test duration,
coverage, …) and visualize them over time in Grafana.

Data is keyed by commit SHA. Which commits belong to a view is answered from
a bare mirror of the project's repository: the first-parent history of the
selected ref, compared against a base ref.

## Service

`revtrend-server --config /etc/revtrend/config.toml`, see
[contrib/config.example.toml](contrib/config.example.toml) and
[contrib/revtrend.service](contrib/revtrend.service).

All `/api/v1` endpoints require a bearer JWT from one of the configured
issuers. Roles per project: `reader` < `writer` < `admin`.

| Endpoint | Role | |
|---|---|---|
| `POST /api/v1/projects/{project}/measurements` | writer | ingest one run for a commit |
| `GET /api/v1/projects/{project}/series` | reader | flat rows for Grafana (`metric`, `ref`, `base`, `from`, `to`, `labels`, `agg`) |
| `GET /api/v1/projects/{project}/compare` | reader | head vs. nearest measured commit from the merge base with `base` |
| `GET /api/v1/projects` | any | projects the caller can read |
| `GET /api/v1/projects/{project}/refs` | reader | branches (`active_since`, `match`) |
| `GET /api/v1/projects/{project}/metrics` | reader | metric names and label values |
| `DELETE /api/v1/projects/{project}/measurements` | admin | delete by `run_id` or `metric` |
| `GET /auth/cli/{config,callback,poll}` | none | PKCE code relay for the CLI |
| `GET /healthz`, `GET /metrics` | none | health and Prometheus metrics |

`/metrics` is unauthenticated; don't expose it through the reverse proxy.

Ingest example:

```sh
curl -fsS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    --data '{"sha": "<40 hex>", "run_id": "'"$BUILD_URL"'", "metrics": [
              {"name": "image.size_bytes", "value": 81234432,
               "labels": {"machine": "amd64", "image": "core-image"}}]}' \
    https://revtrend.example/api/v1/projects/myproject/measurements
```

The measurement `source` (`ci` or `manual`) is derived from the token issuer,
never from the request. Per commit, CI runs take precedence over manual runs.

## CLI

```sh
pipx install revtrend
export REVTREND_URL=https://revtrend.example

revtrend login
revtrend push --project myproject --sha HEAD metrics.json
revtrend compare --project myproject --head HEAD --base release/1.0
```

`login` prints a URL to open in any browser, also from an ssh session: the
service receives the login redirect and relays the authorization code to the
CLI, which redeems it with its PKCE verifier. Tokens are cached in
`~/.cache/revtrend/sessions.json` and refreshed automatically.
`$REVTREND_TOKEN` overrides the cached session.

`push` takes a JSON array of metrics (`-` reads stdin):

```json
[{"name": "image.size_bytes", "value": 81234432,
  "labels": {"machine": "amd64", "image": "core-image"}}]
```

`--sha` and `--head` are resolved in the current repository, so backfilling
means checking out an old commit, measuring and pushing. The commit must exist
in the remote. The run id defaults to `manual-<user>-<timestamp>`; pass
`--run-id` to make a retry idempotent.

`compare` prints head vs. baseline per series, or the raw response with
`--json`.

## Development

```sh
uv sync
```

Tests:

```sh
uv run pytest
```

Static checks:

```sh
uv run ruff check
uv run ruff format --check
```

Entry points:

- `revtrend-server` — HTTP API (FastAPI + uvicorn)
- `revtrend` — CLI for pushing measurements and comparing commits
