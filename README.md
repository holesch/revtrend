# revtrend

Track numeric metrics per git commit (image size, binary size, test duration,
coverage, …) and visualize them over time in Grafana.

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Entry points:

- `revtrend-server` — HTTP API (FastAPI + uvicorn)
- `revtrend` — CLI for pushing measurements and comparing commits
