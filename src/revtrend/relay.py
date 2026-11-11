import asyncio
import dataclasses
import re
import time

# state doubles as the poll key, so it must carry at least 128 bits of entropy
STATE_RE = re.compile(r"^[A-Za-z0-9_-]{22,128}$")


class RelayFullError(Exception):
    pass


@dataclasses.dataclass
class _Entry:
    expires_at: float
    event: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    result: dict | None = None


class Relay:
    """Hands authorization codes from the OAuth redirect to the polling CLI.

    Codes are useless without the PKCE verifier, which only the CLI knows.
    Each code is delivered at most once and expires after `ttl` seconds.
    """

    def __init__(self, ttl=120, max_entries=1000):
        self.ttl = ttl
        self.max_entries = max_entries
        self._entries = {}

    def deliver(self, state, result):
        self._expire()
        entry = self._entries.get(state)
        if entry is None:
            entry = self._entries[state] = self._new_entry()
        elif entry.event.is_set():
            return
        entry.result = result
        entry.expires_at = time.monotonic() + self.ttl
        entry.event.set()

    async def wait(self, state, max_wait):
        self._expire()
        entry = self._entries.get(state)
        if entry is None:
            entry = self._entries[state] = self._new_entry(extra=max_wait)
        try:
            await asyncio.wait_for(entry.event.wait(), max_wait)
        except TimeoutError:
            return None
        result, entry.result = entry.result, None
        self._entries.pop(state, None)
        return result

    def _new_entry(self, extra=0):
        if len(self._entries) >= self.max_entries:
            raise RelayFullError("too many pending logins")
        return _Entry(expires_at=time.monotonic() + self.ttl + extra)

    def _expire(self):
        now = time.monotonic()
        for state in [s for s, e in self._entries.items() if e.expires_at < now]:
            del self._entries[state]
