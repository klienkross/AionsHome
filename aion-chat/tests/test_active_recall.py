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

async def test_expand_memory():
    db_path = await setup_test_db()
    try:
        from memory_cards import create_card, create_link, create_aggregate_for_chain
        from active_recall import expand_memory

        c1 = await create_card(content="感冒了", card_type="event", embed=False)
        c2 = await create_card(content="发烧了", card_type="event", embed=False)
        c3 = await create_card(content="好了", card_type="event", embed=False)
        await create_link(c1["id"], c2["id"], "follow_up")
        await create_link(c2["id"], c3["id"], "follow_up")
        agg = await create_aggregate_for_chain([c1, c2, c3], "感冒事件")

        result = await expand_memory(agg["id"])
        assert len(result) == 3
        assert result[0]["content"] == "感冒了"
    finally:
        os.unlink(db_path)

async def test_get_timeline():
    db_path = await setup_test_db()
    try:
        from memory_cards import create_card, create_link
        from active_recall import get_timeline

        c1 = await create_card(content="计划A", card_type="plan", embed=False)
        c2 = await create_card(content="执行A", card_type="event", embed=False)
        await create_link(c1["id"], c2["id"], "follow_up")

        timeline = await get_timeline(c1["id"])
        assert len(timeline) == 2
        assert timeline[0]["content"] == "计划A"
        assert timeline[1]["content"] == "执行A"
    finally:
        os.unlink(db_path)

if __name__ == "__main__":
    asyncio.run(test_expand_memory())
    print("PASS: test_expand_memory")
    asyncio.run(test_get_timeline())
    print("PASS: test_get_timeline")
