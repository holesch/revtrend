# revtrend

Track numeric metrics per git commit, such as image size, binary size, test
duration or coverage, and watch them change over time in Grafana.

Measurements are keyed by commit SHA. Which commits belong to a branch is
answered from git, not from timestamps: a branch shows its own commits plus
the history it was forked from, so a commit on the default branch counts for
every branch forked after it. CI can compare a pull request against its target
to make changes visible in reviews. revtrend never fails a build; thresholds
are up to you.

## Quick start

Install the CLI and point it at your revtrend service:

```sh
pipx install revtrend
export REVTREND_URL=https://revtrend.example
revtrend login
```

`login` prints a URL to open in any browser, so it also works over ssh. The
session is cached in `~/.cache/revtrend/sessions.json` and refreshed
automatically.

Inside a checkout of your project, push a measurement for a commit and compare
it with a base branch:

```sh
revtrend push --label machine:amd64 --label image:core-image \
    image.size_bytes=81234432 image.files=5321
revtrend compare --base release/1.0
```

```
3f2a91c0 vs. release/1.0 (merge base 8d0e4b12, tip c71a5e09)

series                                            baseline      head     delta  delta %
image.files{image=core-image,machine=amd64}           5298      5321       +23  +0.4341
image.size_bytes{image=core-image,machine=amd64}  80123904  81234432  +1110528   +1.386
```

The project is found by matching the git remotes of the current repository
against the projects on the service. Pass `--project` to choose one
explicitly.

### Pushing

```
revtrend push [--sha REV] [--label KEY:VALUE]... [--run-id ID] NAME=VALUE...
```

- `--sha` defaults to `HEAD` and is resolved in the current repository. The
  commit must exist in the remote; local-only commits are rejected.
- `--label` applies to all metrics of the push. For metrics with different
  labels, push once per label set.
- All values of one push form a *run*. The run id defaults to
  `$REVTREND_RUN_ID`, or `manual-<user>-<timestamp>` if that isn't set. Pushing
  the same run id again is a no-op, so pass `--run-id` to make a retry safe.

To backfill history, check out an old commit, measure, and push.

### Comparing

```
revtrend compare [--head REV] [--base REF] [--metric NAME]... [--label KEY:VALUE]... [--json]
```

`compare` finds where `--head` (default `HEAD`) forked from `--base` (default:
the remote's default branch) and compares head against the nearest measured
commit at or before that fork point. A `(N back)` note in the baseline column
means the fork point itself wasn't measured. `--json` prints the raw response
for scripts.

## In CI

The CLI reads its settings from the environment, so a CI job only needs three
variables:

| Variable | |
|---|---|
| `REVTREND_URL` | service URL |
| `REVTREND_TOKEN` | an OIDC token the service accepts, used instead of `login` |
| `REVTREND_RUN_ID` | a unique id for the build, e.g. the build URL |

Example for Jenkins with the
[OpenID Connect Provider](https://plugins.jenkins.io/oidc-provider/) plugin:

```groovy
environment {
    REVTREND_URL = 'https://revtrend.example'
    REVTREND_RUN_ID = "${env.BUILD_URL}"
}
steps {
    withCredentials([string(credentialsId: 'revtrend-oidc', variable: 'REVTREND_TOKEN')]) {
        sh '''
            pipx install revtrend
            revtrend push --label machine:amd64 --label image:core-image \
                image.size_bytes=$(stat -c %s core-image.wic)
            if [ -n "$CHANGE_TARGET" ]; then
                revtrend compare --base "$CHANGE_TARGET" --json > revtrend-compare.json
            fi
        '''
    }
}
```

What to do with the comparison, such as posting a PR comment or applying
thresholds, is up to the pipeline.

Measure the target branches too, after every merge, not only pull requests.
Otherwise `compare` has no baseline to compare against.

Runs from CI and from developers are kept apart: the service marks each run
`ci` or `manual` based on the token's issuer. If a commit has CI runs, manual
runs of the same series are ignored, so manual pushes can fill gaps but never
distort CI data.

## Concepts

**Metrics and labels.** Metric names use `[a-z0-9_.]` (e.g.
`image.size_bytes`), label keys use `[a-z0-9_]`, and label values are free text.
Use labels for dimensions such as machine or test suite instead of encoding
them into the metric name. A metric and its labels form a *series*. There is
nothing to declare up front: a series exists as soon as its first value is
pushed.

**Runs and aggregation.** A commit can have several runs, from CI retries or
rebuilds. Queries reduce them to one value per commit, using `median` by
default (`latest`, `min`, `max` and `mean` are also available).

**History.** A branch's history is its first-parent history, so with merge
commits the default branch shows one point per merged pull request. The x-axis
is the committer time.

## Grafana

Use the [Infinity](https://grafana.com/grafana/plugins/yesoreyeram-infinity-datasource/)
datasource with OAuth2 client credentials for a token with the `reader` role.
A time series panel can query:

```
/api/v1/projects/$project/series?metric=$metric&ref=$ref&base=$base&labels=machine:$machine&from=${__from}&to=${__to}
```

Each row has `time`, `sha`, `subject`, `series` (use it for grouping),
`value` and `on_branch`. `on_branch` is true for commits on `ref` but not on
`base`, so you can highlight a branch's own commits. Dashboard variables can be
filled from `/api/v1/projects`, `/api/v1/projects/$project/refs?active_since=90d`
and `/api/v1/projects/$project/metrics`.

## Running the service

```sh
pipx install 'revtrend[server]'
revtrend-server --config /etc/revtrend/config.toml
```

See
[contrib/config.example.toml](https://github.com/holesch/revtrend/blob/master/contrib/config.example.toml)
and
[contrib/revtrend.service](https://github.com/holesch/revtrend/blob/master/contrib/revtrend.service).
The service keeps a SQLite database and a bare mirror of each project's
repository, which it fetches periodically and whenever it sees an unknown
commit. It only reads from the remote.

Every API request needs a bearer JWT from one of the configured issuers. Rules
map token claims to a role per project:

| Role | Allows |
|---|---|
| `reader` | series, compare, discovery |
| `writer` | reader + push |
| `admin` | writer + delete |

Each issuer has a fixed `source` (`ci` or `manual`). Runs record the submitter
as `<issuer name>:<sub>`; set `display_claims` on an issuer (e.g.
`["preferred_username", "name"]`) to also store a readable name.

`/metrics` (Prometheus) is unauthenticated, so don't expose it through the
reverse proxy.

## HTTP API

| Endpoint | Role | |
|---|---|---|
| `POST /api/v1/projects/{project}/measurements` | writer | push one run for a commit |
| `GET /api/v1/projects/{project}/series` | reader | flat rows for Grafana (`metric`, `ref`, `base`, `from`, `to`, `labels`, `agg`) |
| `GET /api/v1/projects/{project}/compare` | reader | head vs. nearest measured commit from the merge base with `base` |
| `GET /api/v1/projects` | any | projects the caller can read |
| `GET /api/v1/projects/{project}/refs` | reader | branches (`active_since`, `match`) |
| `GET /api/v1/projects/{project}/metrics` | reader | metric names and label values |
| `DELETE /api/v1/projects/{project}/measurements` | admin | delete by `run_id` or `metric` |
| `GET /auth/cli/{config,callback,poll}` | none | PKCE code relay for `revtrend login` |
| `GET /healthz`, `GET /metrics` | none | health and Prometheus metrics |

Pushing without the CLI:

```sh
curl -fsS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    --data '{"sha": "<40 hex>", "run_id": "'"$BUILD_URL"'", "metrics": [
              {"name": "image.size_bytes", "value": 81234432,
               "labels": {"machine": "amd64", "image": "core-image"}}]}' \
    https://revtrend.example/api/v1/projects/myproject/measurements
```

## Development

```sh
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
```
