"""WS 协议一致性审计：对比服务端发送的消息类型与前端处理的消息类型"""
import re, sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

# WS 发送相关的模式
WS_SEND = re.compile(r'(?:broadcast|send_text|send_to_\w+|ws\.send)\s*\([^)]*"type"\s*:\s*"(\w+)"', re.DOTALL)


def extract_server_types():
    types = set()
    for f in ROOT.rglob("*.py"):
        text = f.read_text(encoding="utf-8", errors="ignore")
        for m in WS_SEND.finditer(text):
            types.add(m.group(1))
    return types


def extract_client_types():
    types = set()
    js_dir = ROOT / "static"
    if js_dir.is_dir():
        for f in js_dir.rglob("*.js"):
            text = f.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r'type\s*===\s*"(\w+)"', text):
                types.add(m.group(1))
    return types


def main():
    server = extract_server_types()
    client = extract_client_types()

    both = server & client
    server_only = server - client
    client_only = client - server

    print("=== WS message type cross-reference ===\n")

    print(f"[OK] Both sides ({len(both)}):")
    for t in sorted(both):
        print(f"    {t}")

    print(f"\n[!!] Server sends, client NOT handling ({len(server_only)}):")
    for t in sorted(server_only):
        print(f"    {t}")

    print(f"\n[??] Client handles, server NOT sending ({len(client_only)}):")
    for t in sorted(client_only):
        print(f"    {t}")

    if server_only:
        print(f"\n*** {len(server_only)} server message types missing client handler! ***")
        return 1
    else:
        print("\nAll server message types have client handlers.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
