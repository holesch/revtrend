import os
import subprocess

import pytest

# Makes asyncio report slow callbacks, e.g. blocking calls in async code.
os.environ.setdefault("PYTHONASYNCIODEBUG", "1")


class Remote:
    """Builds a git repository with deterministic commit times."""

    def __init__(self, path):
        self.path = path
        self.time = 1_700_000_000
        self.git("init", "--quiet", "--initial-branch=master")

    @property
    def url(self):
        return f"file://{self.path}"

    def git(self, *args):
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_AUTHOR_DATE": f"{self.time} +0000",
            "GIT_COMMITTER_DATE": f"{self.time} +0000",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def commit(self, subject):
        self.time += 60
        self.git("commit", "--quiet", "--allow-empty", "-m", subject)
        return self.git("rev-parse", "HEAD")

    def merge(self, branch, subject):
        self.time += 60
        self.git("merge", "--quiet", "--no-ff", "-m", subject, branch)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def remote(tmp_path):
    path = tmp_path / "remote"
    path.mkdir()
    return Remote(path)
