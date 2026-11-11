import asyncio
import importlib.resources
import sqlite3
import threading


class Database:
    """SQLite access from async code.

    Queries run in worker threads. Reads use one connection per thread,
    writes go through a single connection serialized by an asyncio lock.
    """

    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        self._write_lock = asyncio.Lock()
        self._writer = None

    def open(self):
        self._writer = self._connect()
        migrate(self._writer)

    def close(self):
        self._writer.close()

    async def read(self, fn, *args):
        return await asyncio.to_thread(self._read, fn, *args)

    async def write(self, fn, *args):
        async with self._write_lock:
            return await asyncio.to_thread(self._write, fn, *args)

    def _read(self, fn, *args):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = self._connect()
        return fn(conn, *args)

    def _write(self, fn, *args):
        with self._writer:
            return fn(self._writer, *args)

    def _connect(self):
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn


def migrate(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, script in migrations():
        if number <= version:
            continue
        conn.executescript(f"BEGIN; {script}; PRAGMA user_version = {number}; COMMIT;")


def migrations():
    files = importlib.resources.files("revtrend.migrations").iterdir()
    scripts = [
        (int(f.name.split("_", 1)[0]), f.read_text())
        for f in files
        if f.name.endswith(".sql")
    ]
    return sorted(scripts)


def sync_projects(conn, names):
    conn.executemany(
        "INSERT OR IGNORE INTO project (name) VALUES (?)", [(n,) for n in names]
    )
    rows = conn.execute("SELECT name, id FROM project").fetchall()
    return {row["name"]: row["id"] for row in rows}
