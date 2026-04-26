import asyncio
import aiosqlite
import tempfile, os
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

async def _init_test_db(path):
    """Replicate init_db logic for test DB"""
    import importlib
    import config
    # Modify config before importing database
    config.DB_PATH = path
    import database
    # Reload the database module so it uses the new DB_PATH
    importlib.reload(database)
    await database.init_db()
    return path

async def test_memory_cards_table():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        await _init_test_db(db_path)
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            # Verify memory_cards columns
            cur = await db.execute("PRAGMA table_info(memory_cards)")
            cols = {row[1] for row in await cur.fetchall()}
            expected = {"id", "content", "type", "status", "created_at", "updated_at",
                       "source_conv", "source_start_ts", "source_end_ts", "embedding",
                       "keywords", "importance", "unresolved", "valence", "arousal",
                       "intensity_score"}
            assert expected.issubset(cols), f"Missing columns: {expected - cols}"

            # Verify memory_links columns
            cur = await db.execute("PRAGMA table_info(memory_links)")
            cols = {row[1] for row in await cur.fetchall()}
            expected_links = {"id", "from_id", "to_id", "relation", "created_at"}
            assert expected_links.issubset(cols), f"Missing link columns: {expected_links - cols}"

            # Verify indexes exist
            cur = await db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_memory_%'")
            indexes = {row[0] for row in await cur.fetchall()}
            assert "idx_memory_cards_status" in indexes
            assert "idx_memory_cards_created" in indexes
            assert "idx_memory_cards_type" in indexes
            assert "idx_memory_links_from" in indexes
            assert "idx_memory_links_to" in indexes
    finally:
        os.unlink(db_path)

if __name__ == "__main__":
    asyncio.run(test_memory_cards_table())
    print("PASS: test_memory_cards_table")
