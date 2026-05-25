"""CLI 入口：多设备同步推送/拉取。

Usage:
    python sync_to_cloud.py --push     # 推送本地增量到 GitHub
    python sync_to_cloud.py --pull     # 从 GitHub 拉取增量到本地
    python sync_to_cloud.py --status   # 查看同步状态
"""

import asyncio
import argparse
import sys


async def main():
    parser = argparse.ArgumentParser(description="Aion multi-device sync")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--push", action="store_true", help="Push local changes to cloud")
    group.add_argument("--pull", action="store_true", help="Pull cloud changes to local")
    group.add_argument("--status", action="store_true", help="Show sync status")
    args = parser.parse_args()

    from config import is_sync_configured, get_sync_config

    if not is_sync_configured():
        print("ERROR: Sync not configured.")
        print("Set 'github_sync_token' and 'sync_repo' in data/settings.json")
        print('Example: {"github_sync_token": "ghp_xxx", "sync_repo": "owner/Aions_memory"}')
        sys.exit(1)

    if args.status:
        cfg = get_sync_config()
        print(f"Device ID:   {cfg['device_id']}")
        print(f"Device Name: {cfg['device_name']}")
        print(f"Repo:        {cfg['sync_repo']}")
        token = cfg['github_sync_token']
        print(f"Token:       ***{token[-4:]}")
        return

    if args.push:
        from sync_engine import sync_push
        print("Pushing local changes to cloud...")
        result = await sync_push()
        if result["ok"]:
            print(f"Done! Pushed {result['conversations_pushed']} conversations, "
                  f"{result['memories_pushed']} memories, {result['schedules_pushed']} schedules.")
            print(f"Commit: {result['commit'][:12]}")
        else:
            print(f"ERROR: {result['error']}")
            sys.exit(1)

    if args.pull:
        from sync_engine import sync_pull
        print("Pulling cloud changes to local...")
        result = await sync_pull()
        if result["ok"]:
            print(f"Done! Imported:")
            print(f"  Conversations: {result['conversations']}")
            print(f"  Memories: {result['memories']}")
            print(f"  Schedules: {result['schedules']}")
        else:
            print(f"ERROR: {result['error']}")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
