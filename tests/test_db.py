import revtrend.db as db


async def test_migrate_and_sync_projects(tmp_path):
    database = db.Database(tmp_path / "metrics.db")
    database.open()
    try:
        ids = await database.write(db.sync_projects, ["a", "b"])
        assert set(ids) == {"a", "b"}
        again = await database.write(db.sync_projects, ["a", "b"])
        assert again == ids
        version = await database.read(
            lambda c: c.execute("PRAGMA user_version").fetchone()[0]
        )
        assert version == len(db.migrations())
    finally:
        database.close()


async def test_migrate_is_idempotent(tmp_path):
    for _ in range(2):
        database = db.Database(tmp_path / "metrics.db")
        database.open()
        database.close()
