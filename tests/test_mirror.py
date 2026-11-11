import asyncio
import pathlib
import time

import pytest

import revtrend.mirror as mirror


@pytest.fixture
async def repo(tmp_path, remote, graph):
    m = mirror.Mirror(tmp_path / "repos" / "test.git", remote.url)
    await m.init()
    return m


async def test_init_detects_default_branch(repo):
    assert repo.ready
    assert repo.default_branch == "master"


async def test_resolve(repo, graph):
    assert await repo.resolve("master") == graph["c3"]
    assert await repo.resolve("feature/x") == graph["x2"]
    assert await repo.resolve("v1") == graph["c2"]
    assert await repo.resolve(graph["a1"]) == graph["a1"]


@pytest.mark.parametrize(
    "ref", ["nope", "master~1", "master^", "-h", "a..b", "master@{1}", "0" * 40]
)
async def test_resolve_unknown(repo, ref):
    with pytest.raises(mirror.UnknownRefError):
        await repo.resolve(ref)


async def test_history_is_first_parent(repo, graph):
    history = await repo.history(graph["x2"])
    assert [c.subject for c in history] == [
        "x2",
        "x1",
        "c3",
        "Merge feature/a",
        "c2",
        "c1",
    ]
    assert history[0].sha == graph["x2"]
    assert history[0].time > history[1].time


async def test_history_is_cached(repo, graph):
    first = await repo.history(graph["c3"])
    assert await repo.history(graph["c3"]) is first


async def test_branch_only(repo, graph):
    assert await repo.branch_only(graph["c3"], graph["x2"]) == {
        graph["x1"],
        graph["x2"],
    }
    assert await repo.branch_only(graph["c3"], graph["c3"]) == frozenset()


async def test_merge_base(repo, graph):
    assert await repo.merge_base(graph["r1"], graph["x2"]) == graph["c2"]


async def test_first_parents(repo, graph):
    assert await repo.first_parents(graph["m1"], 3) == [
        graph["m1"],
        graph["c2"],
        graph["c1"],
    ]


async def test_refs(repo, graph):
    refs = {r.name: r for r in await repo.refs()}
    assert set(refs) == {"master", "feature/a", "feature/x", "release/1.0"}
    assert refs["feature/x"].sha == graph["x2"]
    assert refs["feature/x"].time > refs["master"].time


async def test_fetch_picks_up_new_commits_and_default_branch(repo, remote):
    new = remote.commit("c4")
    remote.git("symbolic-ref", "HEAD", "refs/heads/release/1.0")
    remote.git("branch", "--quiet", "-D", "feature/a")
    await repo.fetch()
    assert await repo.resolve("master") == new
    assert repo.default_branch == "release/1.0"
    assert "feature/a" not in {r.name for r in await repo.refs()}


async def test_fetch_coalesces(repo, monkeypatch):
    calls = []
    original = repo._git_remote

    async def counting(*args):
        calls.append(args[0])
        return await original(*args)

    monkeypatch.setattr(repo, "_git_remote", counting)
    since = time.monotonic()
    await asyncio.gather(repo.fetch(not_before=since), repo.fetch(not_before=since))
    assert calls.count("fetch") == 1


async def test_not_ready(tmp_path):
    m = mirror.Mirror(tmp_path / "x.git", "file:///nonexistent")
    with pytest.raises(mirror.NotReadyError):
        await m.resolve("master")


async def test_credentials_passed_via_env(tmp_path):
    token = pathlib.Path(tmp_path / "token")
    token.write_text("secret\n")
    m = mirror.Mirror(
        tmp_path / "x.git", "https://example.invalid/x.git", credentials_file=token
    )
    env = m._env(remote=True)
    assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer secret"
    assert "GIT_CONFIG_COUNT" not in m._env(remote=False)
