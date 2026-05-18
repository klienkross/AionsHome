import sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding='utf-8')
from config import DB_PATH
db = sqlite3.connect(str(DB_PATH))
print("chatroom_memories:", db.execute("SELECT COUNT(*) FROM chatroom_memories").fetchone()[0])
row = db.execute("SELECT anchor_ts FROM chatroom_digest_anchors WHERE room_id='connor_unified'").fetchone()
print("connor_unified anchor:", row[0] if row else "无")
print("chatroom_messages:", db.execute("SELECT COUNT(*) FROM chatroom_messages WHERE sender != 'system'").fetchone()[0])
db.close()
