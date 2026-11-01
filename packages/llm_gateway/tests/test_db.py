import asyncio
import sqlite3
from pathlib import Path

from llm_gateway.db import Database


async def test_opening_waits_for_another_process_writing_to_a_new_file(tmp_path: Path) -> None:
    path = tmp_path / "busy.db"
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("CREATE TABLE notes (body TEXT)")
    # Still in rollback mode, so the switch to WAL is refused until this write ends.
    other.execute("BEGIN IMMEDIATE")
    opening = asyncio.create_task(Database.open(str(path)))
    await asyncio.sleep(0.3)
    assert not opening.done()
    other.execute("COMMIT")
    other.close()
    db = await opening
    try:
        assert await db.fetch_all("PRAGMA journal_mode") == [("wal",)]
    finally:
        await db.close()
