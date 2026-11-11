import argparse
import base64
import hashlib
import json
import os
import pathlib
import secrets
import sys
import time
import urllib.parse

import httpx

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
