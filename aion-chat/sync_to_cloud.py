"""同步聊天记录和记忆库到云端 Aions_memory 仓库"""

import os
import subprocess
import shutil
from pathlib import Path
from datetime import datetime

AIONS_MEMORY_PATH = Path("D:/pyworks/Aions_memory")
DATA_DIR = Path(__file__).resolve().parent / "data"
_GIT_PREFIX = ["-c", "credential.helper=manager"]
_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def _git(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        _GIT_PREFIX + ["git", *args],
        cwd=cwd, capture_output=True, text=True, env=_ENV,
    )


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

        if src_chats.exists():
            if dst_chats.exists():
                shutil.rmtree(str(dst_chats))
            shutil.copytree(str(src_chats), str(dst_chats))
        if src_db.exists():
            shutil.copy2(str(src_db), str(dst_db))

        cwd = str(AIONS_MEMORY_PATH)

        r = _git("add", ".", cwd=cwd)
        if r.returncode != 0:
            print(f"[sync_to_cloud] git add failed: {r.stderr}")
            return False

        r = _git("diff", "--cached", "--quiet", cwd=cwd)
        if r.returncode == 0:
            print("[sync_to_cloud] No changes to commit")
            return True

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        full_msg = f"[{ts}] {commit_summary}"
        r = _git("commit", "-m", full_msg, cwd=cwd)
        if r.returncode != 0:
            print(f"[sync_to_cloud] git commit failed: {r.stderr}")
            return False

        r = _git("push", cwd=cwd)
        if r.returncode != 0:
            print(f"[sync_to_cloud] git push failed: {r.stderr}")
            return False

        print(f"[sync_to_cloud] Synced: {full_msg}")
        return True
    except Exception as e:
        print(f"[sync_to_cloud] Error: {e}")
        return False
