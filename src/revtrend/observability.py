import datetime
import json
import logging
import time

import prometheus_client

REQUESTS = prometheus_client.Counter(
    "revtrend_http_requests_total", "HTTP requests", ["method", "route", "status"]
)
REQUEST_DURATION = prometheus_client.Histogram(
    "revtrend_http_request_duration_seconds",
    "HTTP request duration",
    ["method", "route"],
)
FETCH_DURATION = prometheus_client.Histogram(
    "revtrend_fetch_duration_seconds",
    "Duration of git fetches from the remote",
    ["project"],
    buckets=(0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600),
)
FETCH_FAILURES = prometheus_client.Counter(
    "revtrend_fetch_failures_total", "Failed git fetches from the remote", ["project"]
)
INGESTED = prometheus_client.Counter(
    "revtrend_ingested_measurements_total",
    "Newly stored measurements",
    ["project", "source"],
)

# Attributes of every LogRecord (plus uvicorn's ANSI-colored duplicate of the
# message); everything else was passed via `extra`.
_RECORD_ATTRIBUTES = set(vars(logging.makeLogRecord({}))) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}


def setup_logging(level=logging.INFO):
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "time": datetime.datetime.fromtimestamp(
                record.created, datetime.UTC
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update(
            {k: v for k, v in vars(record).items() if k not in _RECORD_ATTRIBUTES}
        )
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class MetricsMiddleware:
    """Counts requests and their duration per route template."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = 500

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            path = getattr(route, "path", "unmatched")
            REQUESTS.labels(scope["method"], path, status).inc()
            REQUEST_DURATION.labels(scope["method"], path).observe(
                time.perf_counter() - start
            )


def metrics_response():
    return prometheus_client.generate_latest(), prometheus_client.CONTENT_TYPE_LATEST
