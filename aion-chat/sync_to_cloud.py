"""同步聊天记录和记忆库到云端 Aions_memory 仓库"""

import subprocess
import shutil
from pathlib import Path
from datetime import datetime

AIONS_MEMORY_PATH = Path("D:/pyworks/Aions_memory")
DATA_DIR = Path(__file__).resolve().parent / "data"


def sync_to_cloud(commit_summary: str) -> bool:
    """将 data/chats/ 和 data/chat.db 同步到 Aions_memory 仓库并推送"""
    if not (AIONS_MEMORY_PATH / ".git").exists():
        print(f"[sync_to_cloud] Aions_memory repo not found at {AIONS_MEMORY_PATH}")
        return False
    try:
        src_chats = DATA_DIR / "chats"
        src_db = DATA_DIR / "chat.db"
        dst_chats = AIONS_MEMORY_PATH / "chats"
        dst_db = AIONS_MEMORY_PATH / "chat.db"

        # 复制聊天记录
        if src_chats.exists():
            if dst_chats.exists():
                shutil.rmtree(str(dst_chats))
            shutil.copytree(str(src_chats), str(dst_chats))
        # 复制记忆数据库
        if src_db.exists():
            shutil.copy2(str(src_db), str(dst_db))

        cwd = str(AIONS_MEMORY_PATH)

        # git add
        result = subprocess.run(["git", "add", "."], cwd=cwd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[sync_to_cloud] git add failed: {result.stderr}")
            return False

        # 没有变更则跳过
        status = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=cwd)
        if status.returncode == 0:
            print("[sync_to_cloud] No changes to commit")
            return True

        # git commit
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        full_msg = f"[{ts}] {commit_summary}"
        result = subprocess.run(["git", "commit", "-m", full_msg], cwd=cwd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[sync_to_cloud] git commit failed: {result.stderr}")
            return False

        # git push
        result = subprocess.run(["git", "push"], cwd=cwd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[sync_to_cloud] git push failed: {result.stderr}")
            return False

        print(f"[sync_to_cloud] Synced: {full_msg}")
        return True
    except Exception as e:
        print(f"[sync_to_cloud] Error: {e}")
        return False
