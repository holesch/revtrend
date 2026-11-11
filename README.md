# revtrend

Track numeric metrics per git commit (image size, binary size, test duration,
coverage, …) and visualize them over time in Grafana.

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
