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
import ssl
import subprocess
import sys
import time
import urllib.parse

import httpx
import truststore

import revtrend.store as store

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# [user@]host:path, which git treats as SSH when there is no slash before the colon
SCP_LIKE_RE = re.compile(r"^[^/:]+:")
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
    project_parser = argparse.ArgumentParser(add_help=False)
    project_parser.add_argument(
        "-p",
        "--project",
        help="default: the project whose remote URL matches a git remote",
    )
    push_parser = commands.add_parser(
        "push",
        parents=[project_parser],
        help="push measurements of one run for a commit",
    )
    push_parser.add_argument(
        "--sha", default="HEAD", help="commit, resolved in the current repository"
    )
    push_parser.add_argument(
        "--run-id",
        default=os.environ.get("REVTREND_RUN_ID"),
        help="default: $REVTREND_RUN_ID or manual-<user>-<timestamp>; "
        "reuse it to retry",
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
        "compare",
        parents=[project_parser],
        help="compare a commit with its merge base on a base ref",
    )
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
    metrics_parser = commands.add_parser(
        "metrics",
        parents=[project_parser],
        help="list metrics and how much data there is",
    )
    metrics_parser.add_argument("--json", action="store_true", help="raw response")
    metrics_parser.set_defaults(func=list_metrics)
    runs_parser = commands.add_parser(
        "runs", parents=[project_parser], help="list the latest runs"
    )
    runs_parser.add_argument(
        "--sha", help="only runs of this commit, resolved in the current repository"
    )
    runs_parser.add_argument(
        "-n", "--limit", type=int, default=20, help="number of runs (default: 20)"
    )
    runs_parser.add_argument("--json", action="store_true", help="raw response")
    runs_parser.set_defaults(func=list_runs)
    delete_parser = commands.add_parser(
        "delete",
        parents=[project_parser],
        help="delete the measurements of a run or metric (admin)",
    )
    delete_what = delete_parser.add_mutually_exclusive_group(required=True)
    delete_what.add_argument("--run-id", help="all measurements of this run")
    delete_what.add_argument("--metric", help="all measurements of this metric")
    delete_parser.add_argument(
        "-y", "--yes", action="store_true", help="don't ask for confirmation"
    )
    delete_parser.set_defaults(func=delete)
    args = parser.parse_args(argv)
    if not args.url:
        parser.error("--url or REVTREND_URL required")
    args.url = args.url.rstrip("/")
    http = http or httpx.Client(
        verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        timeout=httpx.Timeout(10, read=POLL_TIMEOUT + 10),
    )
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
    return "\n".join(lines) + "\n" + table(rows, "<>>>>")


def table(rows, align):
    """Aligned columns, `align` has one '<' (left) or '>' (right) per column."""
    widths = [max(len(row[i]) for row in rows) for i in range(len(align))]
    lines = []
    for row in rows:
        cells = [
            cell.ljust(width) if a == "<" else cell.rjust(width)
            for cell, width, a in zip(row, widths, align, strict=True)
        ]
        lines.append("  ".join(cells).rstrip() + "\n")
    return "".join(lines)


def list_metrics(args, http):
    result = api_request(http, args, "GET", "metrics")
    if args.json:
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
        return
    if not result:
        sys.stderr.write(f"no measurements in project {args.project}\n")
        return
    rows = [("metric", "series", "commits", "values", "last measured")]
    rows += [
        (
            m["name"],
            str(m["series"]),
            str(m["commits"]),
            str(m["values"]),
            local_time(m["last_measured_at"]),
        )
        for m in result
    ]
    sys.stdout.write(table(rows, "<>>><"))


def list_runs(args, http):
    params = {"limit": args.limit}
    if args.sha:
        params["sha"] = resolve_commit(args.sha)
    result = api_request(http, args, "GET", "runs", params=params)
    if args.json:
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
        return
    if not result:
        sys.stderr.write(f"no runs in project {args.project}\n")
        return
    rows = [("measured", "sha", "source", "values", "submitted by", "run id")]
    rows += [
        (
            local_time(r["measured_at"]),
            r["short"],
            r["source"],
            str(r["values"]),
            r["submitted_name"] or r["submitted_by"],
            r["run_id"],
        )
        for r in result
    ]
    sys.stdout.write(table(rows, "<<<><<"))


def delete(args, http):
    if args.project is None:
        args.project = infer_project(http, args.url)
    if args.run_id is not None:
        what, params = f"run {args.run_id!r}", {"run_id": args.run_id}
    else:
        what, params = f"metric {args.metric!r}", {"metric": args.metric}
    if not args.yes:
        confirm(f"Delete all measurements of {what} in project {args.project}?")
    result = api_request(http, args, "DELETE", "measurements", params=params)
    sys.stdout.write(f"deleted {result['deleted']} measurements\n")


def confirm(question):
    if not sys.stdin.isatty():
        raise CliError("not a terminal, pass --yes to delete without confirmation")
    sys.stderr.write(f"{question} [y/N] ")
    sys.stderr.flush()
    if sys.stdin.readline().strip().lower() not in {"y", "yes"}:
        raise CliError("aborted")


def local_time(ms):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


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
    if args.project is None:
        args.project = infer_project(http, args.url)
    project = urllib.parse.quote(args.project, safe="")
    response = http.request(
        method,
        f"{args.url}/api/v1/projects/{project}/{path}",
        headers=auth_headers(http, args.url),
        **kwargs,
    )
    return check(response).json()


def auth_headers(http, url):
    return {"Authorization": f"Bearer {access_token(http, url)}"}


def infer_project(http, url):
    """The one project whose remote URL matches a remote of the current repository."""
    keys = {repo_key(remote) for remote in git_remotes()}
    response = http.get(f"{url}/api/v1/projects", headers=auth_headers(http, url))
    matches = sorted(
        p["name"]
        for p in check(response).json()
        if p.get("remote_url") and repo_key(p["remote_url"]) in keys
    )
    if len(matches) > 1:
        raise CliError(
            f"several projects match the git remotes ({', '.join(matches)}), "
            "pass --project"
        )
    if not matches:
        raise CliError(
            "no project you can read matches the git remotes, pass --project"
        )
    return matches[0]


def git_remotes():
    result = subprocess.run(
        ["git", "remote", "-v"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    remotes = {line.split()[1] for line in result.stdout.splitlines() if line.strip()}
    if result.returncode or not remotes:
        raise CliError("no git remotes to infer the project from, pass --project")
    return remotes


def repo_key(url):
    """Host and path of a git URL, ignoring user, port, scheme and .git suffix.

    Also drops the scm/ prefix of Bitbucket Server HTTP URLs, so they match
    the SSH URLs of the same repository.
    """
    if "://" in url:
        parts = urllib.parse.urlsplit(url)
        host, path = parts.hostname or "", parts.path
    elif SCP_LIKE_RE.match(url):
        host, _, path = url.partition(":")
        host = host.rpartition("@")[2]
    else:
        host, path = "", url
    path = path.strip("/").removesuffix(".git").removeprefix("scm/")
    return host.lower(), path.lower()


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
