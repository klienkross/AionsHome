import asyncio
import importlib
import sys
import tempfile
import os
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


async def setup_test_db():
    """Initialize a fresh test DB, reload database module so get_db() uses new path."""
    import config
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = f.name
    f.close()
    config.DB_PATH = db_path
    import database
    importlib.reload(database)
    await database.init_db()
    # Also reload memory_cards so it picks up the reloaded database module
    if "memory_cards" in sys.modules:
        importlib.reload(sys.modules["memory_cards"])
    return db_path


async def test_create_and_get_card():
    db_path = await setup_test_db()
    try:
        from memory_cards import create_card, get_card

        card = await create_card(
            content="喜欢拿铁",
            card_type="preference",
            keywords=["拿铁", "咖啡"],
            importance=0.6,
            source_conv="conv_001",
            source_start_ts=1000.0,
            source_end_ts=2000.0,
            embed=False,
        )
        assert card["id"].startswith("card_")
        assert card["content"] == "喜欢拿铁"
        assert card["type"] == "preference"
        assert card["status"] == "open"

        fetched = await get_card(card["id"])
        assert fetched is not None
        assert fetched["content"] == "喜欢拿铁"
    finally:
        os.unlink(db_path)


async def test_update_card_status():
    db_path = await setup_test_db()
    try:
        from memory_cards import create_card, update_card_status, get_card

        card = await create_card(content="计划去咖啡店", card_type="plan", embed=False)
        await update_card_status(card["id"], "closed")
        fetched = await get_card(card["id"])
        assert fetched["status"] == "closed"
    finally:
        os.unlink(db_path)


async def test_create_link():
    db_path = await setup_test_db()
    try:
        from memory_cards import create_card, create_link, get_links_from

        c1 = await create_card(content="计划去咖啡店", card_type="plan", embed=False)
        c2 = await create_card(content="去了咖啡店", card_type="event", embed=False)
        await create_link(c1["id"], c2["id"], "follow_up")
        links = await get_links_from(c1["id"])
        assert len(links) == 1
        assert links[0]["to_id"] == c2["id"]
        assert links[0]["relation"] == "follow_up"
    finally:
        os.unlink(db_path)


async def test_list_cards_filtered():
    db_path = await setup_test_db()
    try:
        from memory_cards import create_card, list_cards, update_card_status

        await create_card(content="事件A", card_type="event", embed=False)
        await create_card(content="偏好B", card_type="preference", embed=False)
        c3 = await create_card(content="事件C", card_type="event", embed=False)
        await update_card_status(c3["id"], "closed")

        all_cards = await list_cards()
        assert len(all_cards) == 3

        open_only = await list_cards(status="open")
        assert len(open_only) == 2

        events = await list_cards(card_type="event")
        assert len(events) == 2
    finally:
        os.unlink(db_path)


if __name__ == "__main__":
    for name, fn in [
        ("test_create_and_get_card", test_create_and_get_card),
        ("test_update_card_status", test_update_card_status),
        ("test_create_link", test_create_link),
        ("test_list_cards_filtered", test_list_cards_filtered),
    ]:
        asyncio.run(fn())
        print(f"PASS: {name}")
