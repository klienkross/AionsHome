"""
数据库初始化与连接
"""

import aiosqlite
from config import DB_PATH


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT 'gemini-3-flash',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (conv_id) REFERENCES conversations(id) ON DELETE CASCADE
            )
        """)
        await db.execute("PRAGMA foreign_keys = ON")
        try:
            await db.execute("ALTER TABLE messages ADD COLUMN attachments TEXT DEFAULT ''")
        except:
            pass
        try:
            await db.execute("ALTER TABLE messages ADD COLUMN starred INTEGER DEFAULT 0")
        except:
            pass
        # 性能索引
        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv_id ON messages(conv_id, created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC)")
        # messages 表新增字段
        try:
            await db.execute("ALTER TABLE messages ADD COLUMN surfaced_memory_ids TEXT")
        except:
            pass
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                type TEXT DEFAULT 'event',
                created_at REAL NOT NULL,
                source_conv TEXT,
                embedding BLOB
            )
        """)
        # memories 表新增字段（向后兼容迁移）
        for col, defn in [
            ("keywords", "TEXT DEFAULT ''"),
            ("importance", "REAL DEFAULT 0.5"),
            ("source_start_ts", "REAL"),
            ("source_end_ts", "REAL"),
            ("unresolved", "INTEGER DEFAULT 0"),
            ("source_msg_id", "TEXT"),
            ("valence", "REAL DEFAULT 0.0"),
            ("arousal", "REAL DEFAULT 0.0"),
        ]:
            try:
                await db.execute(f"ALTER TABLE memories ADD COLUMN {col} {defn}")
            except:
                pass
        # ── 日程/闹铃表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                trigger_at TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_schedules_status ON schedules(status, trigger_at)")
        try:
            await db.execute("ALTER TABLE schedules ADD COLUMN repeat TEXT DEFAULT NULL")
        except:
            pass
        # schedules 表新增字段（向后兼容迁移）
        for col, defn in [
            ("origin", "TEXT DEFAULT 'aion'"),        # 'aion' | 'connor'
            ("origin_room_id", "TEXT DEFAULT ''"),     # 创建时所在的群聊/Connor私聊 room_id（空=Aion私聊）
        ]:
            try:
                await db.execute(f"ALTER TABLE schedules ADD COLUMN {col} {defn}")
            except:
                pass
        # ── 背景思考表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS background_thoughts (
                id TEXT PRIMARY KEY,
                conv_id TEXT,
                msg_id TEXT,
                instruction TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at REAL NOT NULL,
                used INTEGER DEFAULT 0
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_bg_thoughts_used ON background_thoughts(used, created_at DESC)")
        # ── 心语表（保留兼容，不再写入新数据） ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS heart_whispers (
                id TEXT PRIMARY KEY,
                conv_id TEXT,
                msg_id TEXT,
                author TEXT DEFAULT '',
                source_conv TEXT DEFAULT '',
                source_msg_id TEXT DEFAULT '',
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_heart_whispers_created ON heart_whispers(created_at DESC)")
        try:
            await db.execute("ALTER TABLE heart_whispers ADD COLUMN author TEXT DEFAULT ''")
        except:
            pass
        try:
            await db.execute("ALTER TABLE heart_whispers ADD COLUMN source_conv TEXT DEFAULT ''")
        except:
            pass
        try:
            await db.execute("ALTER TABLE heart_whispers ADD COLUMN source_msg_id TEXT DEFAULT ''")
        except:
            pass
        # ── 朋友圈表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS moments (
                id TEXT PRIMARY KEY,
                author TEXT NOT NULL,
                content TEXT NOT NULL,
                source_conv TEXT,
                source_msg_id TEXT,
                expect_reply INTEGER DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_moments_created ON moments(created_at DESC)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS moment_comments (
                id TEXT PRIMARY KEY,
                moment_id TEXT NOT NULL,
                author TEXT NOT NULL,
                content TEXT NOT NULL,
                reply_to_id TEXT,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_moment_comments_moment ON moment_comments(moment_id, created_at)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS moment_reactions (
                id TEXT PRIMARY KEY,
                moment_id TEXT NOT NULL,
                author TEXT NOT NULL,
                type TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(moment_id, author)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_moment_reactions_moment ON moment_reactions(moment_id)")
        # ── 朋友圈已读锚点 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS moment_read_anchor (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_read_at REAL NOT NULL DEFAULT 0
            )
        """)
        # ── 书籍表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS books (
                book_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                author TEXT DEFAULT '未知作者',
                cover_path TEXT,
                total_chapters INTEGER DEFAULT 0,
                current_chapter INTEGER DEFAULT 0,
                current_paragraph INTEGER DEFAULT 0,
                import_time REAL NOT NULL
            )
        """)
        # ── 书籍章节表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS book_chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id TEXT NOT NULL,
                chapter_index INTEGER NOT NULL,
                title TEXT,
                html_content TEXT,
                text_content TEXT,
                paragraphs TEXT,
                char_count INTEGER DEFAULT 0,
                segment_count INTEGER DEFAULT 0,
                segments_meta TEXT DEFAULT '[]',
                FOREIGN KEY (book_id) REFERENCES books(book_id) ON DELETE CASCADE,
                UNIQUE(book_id, chapter_index)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_book_chapters_book ON book_chapters(book_id, chapter_index)")
        # ── 书籍批注表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS book_annotations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id TEXT NOT NULL,
                chapter_index INTEGER NOT NULL,
                segment_index INTEGER NOT NULL,
                annotations TEXT DEFAULT '[]',
                summary TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL,
                FOREIGN KEY (book_id) REFERENCES books(book_id) ON DELETE CASCADE,
                UNIQUE(book_id, chapter_index, segment_index)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_book_annotations_ch ON book_annotations(book_id, chapter_index)")
        # 迁移：为 book_annotations 添加 annotator 字段（aion/connor）
        try:
            await db.execute("ALTER TABLE book_annotations ADD COLUMN annotator TEXT DEFAULT 'aion'")
        except:
            pass
        # 迁移：去掉旧的 UNIQUE(book_id, chapter_index, segment_index) 约束
        # SQLite 不支持 DROP CONSTRAINT，需要重建表
        try:
            cur = await db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='book_annotations'")
            row = await cur.fetchone()
            if row:
                create_sql = row[0] if isinstance(row, (list, tuple)) else row['sql']
                if 'UNIQUE(book_id,chapter_index,segment_index)' in create_sql.replace(' ', ''):
                    await db.execute("PRAGMA foreign_keys = OFF")
                    await db.commit()
                    await db.execute("ALTER TABLE book_annotations RENAME TO _book_annotations_old")
                    await db.execute("""
                        CREATE TABLE book_annotations (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            book_id TEXT NOT NULL,
                            chapter_index INTEGER NOT NULL,
                            segment_index INTEGER NOT NULL,
                            annotations TEXT DEFAULT '[]',
                            summary TEXT DEFAULT '',
                            created_at REAL NOT NULL,
                            updated_at REAL,
                            annotator TEXT DEFAULT 'aion',
                            FOREIGN KEY (book_id) REFERENCES books(book_id) ON DELETE CASCADE
                        )
                    """)
                    await db.execute("""
                        INSERT INTO book_annotations (id, book_id, chapter_index, segment_index,
                            annotations, summary, created_at, updated_at, annotator)
                        SELECT id, book_id, chapter_index, segment_index,
                            annotations, summary, created_at, updated_at, COALESCE(annotator, 'aion')
                        FROM _book_annotations_old
                    """)
                    await db.execute("DROP TABLE _book_annotations_old")
                    await db.execute("CREATE INDEX IF NOT EXISTS idx_book_annotations_ch ON book_annotations(book_id, chapter_index)")
                    await db.commit()
                    await db.execute("PRAGMA foreign_keys = ON")
        except Exception:
            pass
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_book_anno_unique ON book_annotations(book_id, chapter_index, segment_index, annotator)")
        # ── 书籍高亮（用户框选提问）表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS book_highlights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id TEXT NOT NULL,
                chapter_index INTEGER NOT NULL,
                selected_text TEXT NOT NULL,
                start_p INTEGER NOT NULL,
                start_offset INTEGER NOT NULL,
                end_p INTEGER NOT NULL,
                end_offset INTEGER NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (book_id) REFERENCES books(book_id) ON DELETE CASCADE
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_book_highlights_ch ON book_highlights(book_id, chapter_index)")
        # 迁移：为 book_highlights 添加 annotator 和 connor_answer 字段
        try:
            await db.execute("ALTER TABLE book_highlights ADD COLUMN annotator TEXT DEFAULT 'aion'")
        except:
            pass
        try:
            await db.execute("ALTER TABLE book_highlights ADD COLUMN connor_answer TEXT DEFAULT ''")
        except:
            pass
        # ── 小剧场对话表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS theater_conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                persona_id TEXT,
                model TEXT NOT NULL DEFAULT 'gemini-3-flash',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_theater_conv_updated ON theater_conversations(updated_at DESC)")
        # ── 小剧场消息表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS theater_messages (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                attachments TEXT DEFAULT '[]',
                FOREIGN KEY (conv_id) REFERENCES theater_conversations(id) ON DELETE CASCADE
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_theater_msg_conv ON theater_messages(conv_id, created_at)")
        # ── 礼物表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS gifts (
                id TEXT PRIMARY KEY,
                image_path TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                received_at REAL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_gifts_status ON gifts(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_gifts_created ON gifts(created_at DESC)")
        # ── Memory V2: 原子卡片表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memory_cards (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                type TEXT DEFAULT 'event',
                status TEXT DEFAULT 'open',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                source_conv TEXT,
                source_start_ts REAL,
                source_end_ts REAL,
                embedding BLOB,
                keywords TEXT DEFAULT '',
                importance REAL DEFAULT 0.5,
                unresolved INTEGER DEFAULT 0,
                valence REAL DEFAULT 0.0,
                arousal REAL DEFAULT 0.0,
                intensity_score REAL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memory_cards_status ON memory_cards(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memory_cards_created ON memory_cards(created_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memory_cards_type ON memory_cards(type)")
        # Memory Evolution: 新增字段
        for col, defn in [
            ("activation_count", "INTEGER DEFAULT 0"),
            ("last_activated", "REAL"),
            ("source", "TEXT DEFAULT 'both'"),
            ("verified", "INTEGER DEFAULT 1"),
        ]:
            try:
                await db.execute(f"ALTER TABLE memory_cards ADD COLUMN {col} {defn}")
            except:
                pass
        # 初始化已有行的 last_activated 字段
        await db.execute(
            "UPDATE memory_cards SET last_activated = created_at "
            "WHERE last_activated IS NULL"
        )
        # ── Memory V2: 卡片关联表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memory_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_id TEXT NOT NULL,
                to_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (from_id) REFERENCES memory_cards(id) ON DELETE CASCADE,
                FOREIGN KEY (to_id) REFERENCES memory_cards(id) ON DELETE CASCADE
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memory_links_from ON memory_links(from_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memory_links_to ON memory_links(to_id)")
        # Rename old memories table for reference (one-time migration)
        try:
            await db.execute("ALTER TABLE memories RENAME TO memories_v1")
        except:
            pass
        # gifts 表新增字段（向后兼容迁移）
        try:
            await db.execute("ALTER TABLE gifts ADD COLUMN sender TEXT DEFAULT 'aion'")
        except:
            pass
        # ── 基金持仓表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS fund_holdings (
                id TEXT PRIMARY KEY,
                fund_code TEXT NOT NULL,
                fund_name TEXT DEFAULT '',
                shares REAL DEFAULT 0,
                avg_cost REAL DEFAULT 0,
                total_cost REAL DEFAULT 0,
                warn_down REAL DEFAULT -3.0,
                warn_up REAL DEFAULT 15.0,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_fund_holdings_code ON fund_holdings(fund_code)")
        # ── 娱乐室日志表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS playground_logs (
                id TEXT PRIMARY KEY,
                server TEXT NOT NULL,
                instruction TEXT NOT NULL,
                events TEXT NOT NULL DEFAULT '[]',
                summary TEXT DEFAULT '',
                created_at REAL NOT NULL
            )
        """)
        try:
            await db.execute("ALTER TABLE playground_logs ADD COLUMN summary TEXT DEFAULT ''")
        except:
            pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_playground_logs_created ON playground_logs(created_at DESC)")
        # ── 聊天室房间表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chatroom_rooms (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'group',
                aion_persona TEXT DEFAULT '',
                connor_persona TEXT DEFAULT '',
                context_minutes INTEGER DEFAULT 30,
                ai_chat_rounds INTEGER DEFAULT 3,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_chatroom_rooms_updated ON chatroom_rooms(updated_at DESC)")
        # ── 聊天室消息表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chatroom_messages (
                id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                content TEXT NOT NULL,
                attachments TEXT DEFAULT '[]',
                created_at REAL NOT NULL,
                FOREIGN KEY (room_id) REFERENCES chatroom_rooms(id) ON DELETE CASCADE
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_chatroom_msg_room ON chatroom_messages(room_id, created_at)")
        for _tbl in ("messages", "chatroom_messages"):
            try:
                await db.execute(f"ALTER TABLE {_tbl} ADD COLUMN chain_hash TEXT DEFAULT ''")
            except:
                pass
        # ── 聊天室记忆表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chatroom_memories (
                id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                scope TEXT DEFAULT 'group',
                content TEXT NOT NULL,
                keywords TEXT DEFAULT '',
                importance REAL DEFAULT 0.5,
                embedding BLOB,
                source_start_ts REAL,
                source_end_ts REAL,
                created_at REAL NOT NULL,
                unresolved INTEGER DEFAULT 0
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_chatroom_mem_room ON chatroom_memories(room_id, created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_chatroom_mem_scope ON chatroom_memories(scope)")
        for _col, _defn in [("valence", "REAL DEFAULT 0.0"), ("arousal", "REAL DEFAULT 0.0")]:
            try:
                await db.execute(f"ALTER TABLE chatroom_memories ADD COLUMN {_col} {_defn}")
            except:
                pass
        # ── 聊天室总结锚点表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chatroom_digest_anchors (
                room_id TEXT PRIMARY KEY,
                anchor_ts REAL NOT NULL DEFAULT 0
            )
        """)
        # ── 活动轨迹表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS life_trajectory (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_life_trajectory_created ON life_trajectory(created_at DESC)")
        # ── 记账与生理期表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bookkeeping (
                id TEXT PRIMARY KEY,
                record_type TEXT NOT NULL,
                amount REAL DEFAULT 0,
                description TEXT DEFAULT '',
                created_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_bookkeeping_created ON bookkeeping(created_at DESC)")
        await db.commit()


def get_db():
    return aiosqlite.connect(DB_PATH)
