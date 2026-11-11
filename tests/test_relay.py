import asyncio
import concurrent.futures
import time

import pytest

import revtrend.relay as relay

STATE = "s" * 43


async def test_deliver_then_wait():
    r = relay.Relay()
    r.deliver(STATE, {"code": "abc"})
    assert await r.wait(STATE, 1) == {"code": "abc"}
    assert await r.wait(STATE, 0.01) is None


async def test_wait_then_deliver():
    r = relay.Relay()
    waiter = asyncio.create_task(r.wait(STATE, 5))
    await asyncio.sleep(0)
    r.deliver(STATE, {"code": "abc"})
    assert await waiter == {"code": "abc"}


async def test_first_delivery_wins():
    r = relay.Relay()
    r.deliver(STATE, {"code": "first"})
    r.deliver(STATE, {"code": "second"})
    assert await r.wait(STATE, 1) == {"code": "first"}


async def test_expiry():
    r = relay.Relay(ttl=0)
    r.deliver(STATE, {"code": "abc"})
    await asyncio.sleep(0.01)
    assert await r.wait(STATE, 0.01) is None


async def test_max_entries():
    r = relay.Relay(max_entries=1)
    r.deliver(STATE, {"code": "abc"})
    with pytest.raises(relay.RelayFullError):
        r.deliver("t" * 43, {"code": "abc"})


def test_cli_config(client):
    assert client.get("/auth/cli/config").json() == {
        "authority": client.entra.url,
        "client_id": "client-id",
        "scopes": ["api://revtrend/Metrics.Write", "offline_access"],
        "redirect_uri": "https://revtrend.example/auth/cli/callback",
    }


def test_callback_and_poll(client):
    response = client.get(
        "/auth/cli/callback", params={"state": STATE, "code": "the-code"}
    )
    assert response.status_code == 200
    assert "Login complete" in response.text
    response = client.get("/auth/cli/poll", params={"state": STATE, "timeout": 1})
    assert response.json() == {"code": "the-code"}
    response = client.get("/auth/cli/poll", params={"state": STATE, "timeout": 0.01})
    assert response.status_code == 204


def test_poll_waits_for_callback(client):
    with concurrent.futures.ThreadPoolExecutor() as pool:
        poll = pool.submit(
            client.get, "/auth/cli/poll", params={"state": STATE, "timeout": 5}
        )
        while not client.app.state.revtrend.relay._entries:
            time.sleep(0.001)
        client.get("/auth/cli/callback", params={"state": STATE, "code": "the-code"})
        assert poll.result().json() == {"code": "the-code"}


def test_callback_error_is_escaped(client):
    params = {"state": STATE, "error": "access_denied", "error_description": "<script>"}
    response = client.get("/auth/cli/callback", params=params)
    assert "&lt;script&gt;" in response.text
    response = client.get("/auth/cli/poll", params={"state": STATE, "timeout": 1})
    assert response.json() == {
        "error": "access_denied",
        "error_description": "<script>",
    }


@pytest.mark.parametrize("state", ["short", "x" * 22 + "/", "x" * 200])
def test_invalid_state(client, state):
    assert (
        client.get(
            "/auth/cli/callback", params={"state": state, "code": "c"}
        ).status_code
        == 400
    )
    assert client.get("/auth/cli/poll", params={"state": state}).status_code == 400


def test_poll_timeout_bounded(client):
    response = client.get("/auth/cli/poll", params={"state": STATE, "timeout": 3600})
    assert response.status_code == 400
