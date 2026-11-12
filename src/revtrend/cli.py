import argparse
import base64
import getpass
import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import subprocess
import sys
import time
import urllib.parse

import httpx

import revtrend.store as store

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LOGIN_TIMEOUT = 600
POLL_TIMEOUT = 60
# refresh access tokens that expire within this many seconds
EXPIRY_MARGIN = 60


class CliError(Exception):
    pass


def main(argv=None, http=None):
    parser = argparse.ArgumentParser(prog="revtrend")
    parser.add_argument(
        "--url",
        default=os.environ.get("REVTREND_URL"),
        help="service URL (default: $REVTREND_URL)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    login_parser = commands.add_parser("login", help="log in with the browser")
    login_parser.set_defaults(func=login)
    push_parser = commands.add_parser(
        "push", help="push measurements of one run for a commit"
    )
    push_parser.add_argument("--project", required=True)
    push_parser.add_argument(
        "--sha", default="HEAD", help="commit, resolved in the current repository"
    )
    push_parser.add_argument(
        "--run-id", help="default: manual-<user>-<timestamp>; reuse it to retry"
    )
    push_parser.add_argument("metric", nargs="+", type=metric_arg, metavar="NAME=VALUE")
    push_parser.add_argument(
        "--label",
        action="append",
        type=label_arg,
        metavar="KEY:VALUE",
        help="repeatable, applies to all metrics",
    )
    push_parser.set_defaults(func=push)
    compare_parser = commands.add_parser(
        "compare", help="compare a commit with its merge base on a base ref"
    )
    compare_parser.add_argument("--project", required=True)
    compare_parser.add_argument(
        "--head", default="HEAD", help="commit, resolved in the current repository"
    )
    compare_parser.add_argument(
        "--base", default="default", help="ref on the service (default: default)"
    )
    compare_parser.add_argument("--metric", action="append", help="repeatable")
    compare_parser.add_argument(
        "--label", action="append", metavar="KEY:VALUE", help="repeatable"
    )
    compare_parser.add_argument(
        "--agg",
        choices=list(store.AGGREGATIONS),
        default="median",
        help="per-commit aggregation",
    )
    compare_parser.add_argument("--json", action="store_true", help="raw response")
    compare_parser.set_defaults(func=compare)
    args = parser.parse_args(argv)
    if not args.url:
        parser.error("--url or REVTREND_URL required")
    args.url = args.url.rstrip("/")
    http = http or httpx.Client(timeout=httpx.Timeout(10, read=POLL_TIMEOUT + 10))
    try:
        with http:
            args.func(args, http)
    except CliError as e:
        parser.exit(1, f"revtrend: {e}\n")


def login(args, http):
    """Authorization code flow with PKCE; the service relays the code to us.

    The redirect goes to the service, so the browser can run on any device.
    The code is useless without the verifier, which never leaves this process.
    """
    cli_config = check(http.get(f"{args.url}/auth/cli/config")).json()
    oidc = check(
        http.get(f"{cli_config['authority']}/.well-known/openid-configuration")
    ).json()
    scope = " ".join(cli_config["scopes"])
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    query = {
        "client_id": cli_config["client_id"],
        "response_type": "code",
        "redirect_uri": cli_config["redirect_uri"],
        "scope": scope,
        "state": state,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    show_login_url(f"{oidc['authorization_endpoint']}?{urllib.parse.urlencode(query)}")
    code = wait_for_code(http, args.url, state)
    session = {
        "token_endpoint": oidc["token_endpoint"],
        "client_id": cli_config["client_id"],
        "scope": scope,
    }
    session |= token_request(
        http,
        session,
        grant_type="authorization_code",
        code=code,
        redirect_uri=cli_config["redirect_uri"],
        code_verifier=verifier,
    )
    save_session(args.url, session)
    sys.stderr.write("Logged in.\n")


def check(response):
    if response.is_success:
        return response
    try:
        body = response.json()
        detail = body.get("detail") or body.get("error_description") or body
    except (ValueError, AttributeError):
        detail = response.text
    raise CliError(
        f"{response.request.method} {response.url} failed "
        f"({response.status_code}): {detail}"
    )


def pkce_challenge(verifier):
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def show_login_url(url):
    sys.stderr.write(f"Open this URL in a browser to log in:\n\n  {url}\n\n")


def wait_for_code(http, url, state):
    deadline = time.monotonic() + LOGIN_TIMEOUT
    while time.monotonic() < deadline:
        response = check(
            http.get(
                f"{url}/auth/cli/poll",
                params={"state": state, "timeout": POLL_TIMEOUT},
            )
        )
        if response.status_code == httpx.codes.NO_CONTENT:
            continue
        result = response.json()
        if "error" in result:
            raise CliError(
                f"login failed: {result['error']} "
                f"{result.get('error_description') or ''}".strip()
            )
        return result["code"]
    raise CliError("login timed out")


def token_request(http, session, **form):
    response = check(
        http.post(
            session["token_endpoint"],
            data={"client_id": session["client_id"], "scope": session["scope"], **form},
        )
    )
    tokens = response.json()
    return {
        "access_token": tokens["access_token"],
        "expires_at": time.time() + tokens.get("expires_in", 0),
        "refresh_token": tokens.get("refresh_token"),
    }


def save_session(url, session):
    path = sessions_file()
    sessions = load_sessions()
    sessions[url] = session
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with open(fd, "w") as f:
        json.dump(sessions, f, indent=2)
    tmp.replace(path)


def sessions_file():
    cache = os.environ.get("XDG_CACHE_HOME") or pathlib.Path.home() / ".cache"
    return pathlib.Path(cache) / "revtrend" / "sessions.json"


def load_sessions():
    try:
        return json.loads(sessions_file().read_text())
    except FileNotFoundError:
        return {}


def push(args, http):
    metrics = build_metrics(args.metric, args.label or [])
    sha = resolve_commit(args.sha)
    run_id = args.run_id or default_run_id()
    result = api_request(
        http,
        args,
        "POST",
        "measurements",
        json={"sha": sha, "run_id": run_id, "metrics": metrics},
    )
    sys.stdout.write(
        f"{sha[:8]} {run_id}: {result['created']} created, "
        f"{result['existing']} already present\n"
    )


def metric_arg(value):
    name, sep, number = value.partition("=")
    try:
        parsed = float(number)
    except ValueError:
        parsed = None
    if not (name and sep and parsed is not None and math.isfinite(parsed)):
        raise argparse.ArgumentTypeError(
            f"{value!r}, expected NAME=VALUE with a finite number"
        )
    return name, parsed


def label_arg(value):
    key, sep, label_value = value.partition(":")
    if not (key and sep):
        raise argparse.ArgumentTypeError(f"{value!r}, expected KEY:VALUE")
    return key, label_value


def build_metrics(metrics, labels):
    for kind, pairs in (("metric", metrics), ("label", labels)):
        keys = [key for key, _ in pairs]
        if duplicates := sorted({k for k in keys if keys.count(k) > 1}):
            raise CliError(f"duplicate {kind}: {', '.join(duplicates)}")
    return [
        {"name": name, "value": value, "labels": dict(labels)}
        for name, value in metrics
    ]


def resolve_commit(rev):
    if SHA_RE.match(rev):
        return rev
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise CliError(f"cannot resolve {rev!r}: {result.stderr.strip()}")
    return result.stdout.strip()


def default_run_id():
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"manual-{getpass.getuser()}-{timestamp}"


def compare(args, http):
    params = {
        "head": resolve_commit(args.head),
        "base": args.base,
        "metric": args.metric or [],
        "labels": args.label or [],
        "agg": args.agg,
    }
    result = api_request(http, args, "GET", "compare", params=params)
    if args.json:
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
    else:
        sys.stdout.write(format_comparison(result))


def format_comparison(result):
    base = result["base"]
    lines = [
        f"{result['head']['short']} vs. {base['ref']} "
        f"(merge base {base['fork_sha'][:8]}, tip {base['tip_sha'][:8]})",
        "",
    ]
    rows = [("series", "baseline", "head", "delta", "delta %")]
    for r in result["results"]:
        baseline = number(r["baseline"])
        if r["baseline_distance"]:
            baseline += f" ({r['baseline_distance']} back)"
        rows.append(
            (
                series_name(r["metric"], r["labels"]),
                baseline,
                number(r["head"]),
                number(r["delta"], sign=True),
                number(r["delta_pct"], sign=True),
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        cells = [row[0].ljust(widths[0])]
        cells += [
            cell.rjust(width) for cell, width in zip(row[1:], widths[1:], strict=True)
        ]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines) + "\n"


def number(value, sign=False):
    if value is None:
        return "-"
    spec = "+" if sign else ""
    if value == int(value):
        return f"{int(value):{spec}d}"
    return f"{round(value, 4):{spec}}"


def series_name(metric, labels):
    if not labels:
        return metric
    return f"{metric}{{{','.join(f'{k}={v}' for k, v in sorted(labels.items()))}}}"


def api_request(http, args, method, path, **kwargs):
    project = urllib.parse.quote(args.project, safe="")
    response = http.request(
        method,
        f"{args.url}/api/v1/projects/{project}/{path}",
        headers={"Authorization": f"Bearer {access_token(http, args.url)}"},
        **kwargs,
    )
    return check(response).json()


def access_token(http, url):
    """$REVTREND_TOKEN, or the cached session's token, refreshed if needed."""
    if token := os.environ.get("REVTREND_TOKEN"):
        return token
    session = load_sessions().get(url)
    if session is None:
        raise CliError(f"not logged in to {url}, run 'revtrend login'")
    if session["expires_at"] - EXPIRY_MARGIN > time.time():
        return session["access_token"]
    if not session["refresh_token"]:
        raise CliError("session expired, run 'revtrend login'")
    try:
        tokens = token_request(
            http,
            session,
            grant_type="refresh_token",
            refresh_token=session["refresh_token"],
        )
    except CliError as e:
        raise CliError(f"session expired, run 'revtrend login' ({e})") from e
    session |= {k: v for k, v in tokens.items() if v}
    save_session(url, session)
    return session["access_token"]
