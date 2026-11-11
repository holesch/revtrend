import asyncio
import collections
import dataclasses
import logging
import os
import re
import shutil
import time

import revtrend.observability as observability

log = logging.getLogger(__name__)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Stricter than git check-ref-format: no revision syntax (~ ^ : @{ ..), no
# leading dash, so a ref can never be interpreted as an option or expression.
REF_RE = re.compile(r"^(?!-)(?!.*\.\.)(?!.*@\{)[A-Za-z0-9._/+-]+$")
FIELD_SEP = "\x1f"
GIT_TIMEOUT = 60
REMOTE_TIMEOUT = 600


class GitError(Exception):
    pass


class UnknownRefError(Exception):
    pass


class NoMergeBaseError(Exception):
    pass


class NotReadyError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Commit:
    sha: str
    time: int
    subject: str


@dataclasses.dataclass(frozen=True)
class Ref:
    name: str
    sha: str
    time: int


class Mirror:
    """Bare mirror of a remote repository's branches and tags.

    Read-only towards the remote. Fetches are serialized by a lock.
    """

    def __init__(self, path, remote_url, credentials_file=None, cache_size=64):
        self.path = path
        self.name = path.name.removesuffix(".git")
        self.remote_url = remote_url
        self.credentials_file = credentials_file
        self.default_branch = None
        self.ready = False
        self.last_fetch_started = None
        self._fetch_lock = asyncio.Lock()
        self._history = LruCache(cache_size)
        self._branch_only = LruCache(cache_size)

    async def init(self):
        if not self.path.exists():
            await self._clone()
        await self._git("config", "remote.origin.url", self.remote_url)
        self.default_branch = await self._read_default_branch()
        self.ready = True

    async def fetch(self, not_before=None):
        """Fetch from the remote.

        Skipped if another fetch started at or after `not_before` (monotonic
        time), which coalesces concurrent requests and rate-limits on-demand
        fetches.
        """
        async with self._fetch_lock:
            if (
                not_before is not None
                and self.last_fetch_started is not None
                and self.last_fetch_started >= not_before
            ):
                return
            self.last_fetch_started = time.monotonic()
            try:
                with observability.FETCH_DURATION.labels(self.name).time():
                    await self._git_remote("fetch", "--prune", "--quiet", "origin")
                    await self._update_default_branch()
            except Exception:
                observability.FETCH_FAILURES.labels(self.name).inc()
                raise

    async def resolve(self, ref):
        self._check_ready()
        if SHA_RE.match(ref):
            if not await self.has_commit(ref):
                raise UnknownRefError(ref)
            return ref
        if not REF_RE.match(ref):
            raise UnknownRefError(ref)
        for prefix in ("refs/heads/", "refs/tags/"):
            sha = await self._rev_parse(f"{prefix}{ref}^{{commit}}")
            if sha:
                return sha
        raise UnknownRefError(ref)

    async def has_commit(self, sha):
        self._check_ready()
        return await self._rev_parse(f"{sha}^{{commit}}") == sha

    async def history(self, tip):
        """First-parent history of commit `tip`, tip first."""
        self._check_ready()
        return await self._history.get(tip, lambda: self._first_parent_log(tip))

    async def branch_only(self, base, ref):
        """Commits reachable from `ref` but not from `base` (base..ref)."""
        self._check_ready()
        return await self._branch_only.get(
            (base, ref), lambda: self._rev_list_range(base, ref)
        )

    async def merge_base(self, a, b):
        self._check_ready()
        proc = await self._run(["merge-base", a, b], check=False)
        if proc.returncode == 1:
            raise NoMergeBaseError(f"{a} and {b} have no common ancestor")
        _check(proc)
        return proc.stdout.strip()

    async def first_parents(self, sha, max_count):
        """Up to `max_count` commits along the first-parent line from `sha`."""
        self._check_ready()
        out = await self._git(
            "rev-list", "--first-parent", f"--max-count={max_count}", sha
        )
        return out.split()

    async def refs(self):
        self._check_ready()
        fmt = FIELD_SEP.join(
            ["%(refname:lstrip=2)", "%(objectname)", "%(committerdate:unix)"]
        )
        out = await self._git("for-each-ref", f"--format={fmt}", "refs/heads")
        refs = []
        for line in out.splitlines():
            name, sha, ctime = line.split(FIELD_SEP)
            refs.append(Ref(name=name, sha=sha, time=int(ctime or 0)))
        return refs

    async def _clone(self):
        tmp = self.path.with_name(self.path.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.parent.mkdir(parents=True, exist_ok=True)
        log.info("cloning %s into %s", self.remote_url, self.path)
        await self._run(["init", "--bare", "--quiet", str(tmp)], git_dir=None)
        settings = [
            ("remote.origin.url", self.remote_url),
            ("remote.origin.fetch", "+refs/heads/*:refs/heads/*"),
            ("remote.origin.fetch", "+refs/tags/*:refs/tags/*"),
            ("fetch.writeCommitGraph", "true"),
            ("core.logAllRefUpdates", "false"),
        ]
        for key, value in settings:
            await self._run(["config", "--add", key, value], git_dir=tmp)
        await self._run(["fetch", "--quiet", "origin"], git_dir=tmp, remote=True)
        tmp.rename(self.path)
        self.last_fetch_started = time.monotonic()
        await self._update_default_branch()

    async def _update_default_branch(self):
        out = await self._git_remote("ls-remote", "--symref", "origin", "HEAD")
        match = re.search(r"^ref: refs/heads/(\S+)\tHEAD$", out, re.MULTILINE)
        if not match:
            raise GitError(f"remote {self.remote_url} has no symbolic HEAD")
        branch = match[1]
        await self._git("symbolic-ref", "HEAD", f"refs/heads/{branch}")
        if branch != self.default_branch:
            log.info("default branch of %s is %s", self.remote_url, branch)
        self.default_branch = branch

    async def _read_default_branch(self):
        out = await self._git("symbolic-ref", "--short", "HEAD")
        return out.strip()

    async def _rev_parse(self, rev):
        proc = await self._run(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", rev], check=False
        )
        return proc.stdout.strip() if proc.returncode == 0 else None

    async def _first_parent_log(self, tip):
        fmt = FIELD_SEP.join(["%H", "%ct", "%s"])
        out = await self._git(
            "rev-list", "--first-parent", "--no-commit-header", f"--format={fmt}", tip
        )
        commits = []
        for line in out.splitlines():
            sha, ctime, subject = line.split(FIELD_SEP, 2)
            commits.append(Commit(sha=sha, time=int(ctime), subject=subject))
        return commits

    async def _rev_list_range(self, base, ref):
        out = await self._git("rev-list", ref, f"^{base}")
        return frozenset(out.split())

    def _check_ready(self):
        if not self.ready:
            raise NotReadyError(f"mirror of {self.remote_url} is not ready yet")

    async def _git(self, *args):
        proc = await self._run(list(args))
        return proc.stdout

    async def _git_remote(self, *args):
        proc = await self._run(list(args), remote=True)
        return proc.stdout

    async def _run(self, args, git_dir=..., remote=False, check=True):
        if git_dir is ...:
            git_dir = self.path
        cmd = ["git"]
        if git_dir is not None:
            cmd.append(f"--git-dir={git_dir}")
        cmd.extend(args)
        async with asyncio.timeout(REMOTE_TIMEOUT if remote else GIT_TIMEOUT):
            proc = await run(cmd, env=self._env(remote))
        if check:
            _check(proc)
        return proc

    def _env(self, remote):
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
        if remote and self.credentials_file is not None:
            # Passed via environment, not -c, so the token is not visible in
            # the process list.
            token = self.credentials_file.read_text().strip()
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
            env["GIT_CONFIG_VALUE_0"] = f"Authorization: Bearer {token}"
        return env


@dataclasses.dataclass
class Completed:
    args: list
    returncode: int
    stdout: str
    stderr: str


async def run(cmd, env):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await proc.communicate()
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    return Completed(
        cmd,
        proc.returncode,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


def _check(proc):
    if proc.returncode != 0:
        raise GitError(
            f"{' '.join(proc.args[1:])} failed ({proc.returncode}): {proc.stderr.strip()}"
        )


class LruCache:
    """Caches results of async computations; concurrent misses share one computation."""

    def __init__(self, size):
        self.size = size
        self._entries = collections.OrderedDict()

    async def get(self, key, compute):
        task = self._entries.get(key)
        if task is None:
            task = self._entries[key] = asyncio.ensure_future(compute())
            while len(self._entries) > self.size:
                self._entries.popitem(last=False)
        else:
            self._entries.move_to_end(key)
        try:
            return await asyncio.shield(task)
        except Exception:
            if self._entries.get(key) is task:
                del self._entries[key]
            raise
