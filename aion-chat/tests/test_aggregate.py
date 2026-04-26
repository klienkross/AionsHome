import asyncio
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

async def setup_test_db():
    import tempfile, config
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = f.name
    f.close()
    config.DB_PATH = db_path
    from database import init_db
    await init_db()
    return db_path

async def test_get_follow_up_chain():
    db_path = await setup_test_db()
    try:
        from memory_cards import create_card, create_link, get_follow_up_chain

        c1 = await create_card(content="感冒了", card_type="event", embed=False)
        c2 = await create_card(content="还在发烧", card_type="event", embed=False)
        c3 = await create_card(content="感冒好了", card_type="event", embed=False)
        await create_link(c1["id"], c2["id"], "follow_up")
        await create_link(c2["id"], c3["id"], "follow_up")

        chain = await get_follow_up_chain(c1["id"])
        assert len(chain) == 3
        assert chain[0]["id"] == c1["id"]
        assert chain[2]["id"] == c3["id"]
    finally:
        os.unlink(db_path)

async def test_should_aggregate():
    db_path = await setup_test_db()
    try:
        from memory_cards import create_card, create_link, should_generate_aggregate

        c1 = await create_card(content="事件A", card_type="event", embed=False)
        c2 = await create_card(content="事件B", card_type="event", embed=False)
        await create_link(c1["id"], c2["id"], "follow_up")
        # Only 2 in chain — not enough
        assert not await should_generate_aggregate(c1["id"])

        c3 = await create_card(content="事件C", card_type="event", embed=False)
        await create_link(c2["id"], c3["id"], "follow_up")
        # Now 3 — should aggregate
        assert await should_generate_aggregate(c1["id"])
    finally:
        os.unlink(db_path)

if __name__ == "__main__":
    asyncio.run(test_get_follow_up_chain())
    print("PASS: test_get_follow_up_chain")
    asyncio.run(test_should_aggregate())
    print("PASS: test_should_aggregate")
