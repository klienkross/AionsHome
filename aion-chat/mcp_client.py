"""
MCP 连接管理器：管理多个 MCP Server 的连接生命周期
支持 Streamable HTTP（远程）和 stdio（本地进程）两种传输
"""

import json, logging
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.stdio import stdio_client, StdioServerParameters

from config import DATA_DIR

logger = logging.getLogger("mcp_client")

MCP_SERVERS_PATH = DATA_DIR / "mcp_servers.json"

# ── 默认配置 ──────────────────────────────────────
_DEFAULT_CONFIG = {
    "servers": [
        {
            "name": "AI小镇",
            "type": "http",
            "url": "https://aisay.top/chatroom/mcp",
            "headers": {},
            "enabled": True
        }
    ]
}


def _load_config() -> dict:
    if MCP_SERVERS_PATH.exists():
        try:
            return json.loads(MCP_SERVERS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    # 首次使用，自动创建默认配置
    MCP_SERVERS_PATH.write_text(
        json.dumps(_DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return dict(_DEFAULT_CONFIG)


def _save_config(cfg: dict):
    MCP_SERVERS_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


class MCPManager:
    """管理所有 MCP Server 连接"""

    def __init__(self):
        # server_name -> { session, read_stream, write_stream, tools, cm, transport_cm }
        self._connections: dict[str, dict] = {}

    # ── 列出所有配置 ────────────────────────
    def list_servers(self) -> list[dict]:
        cfg = _load_config()
        result = []
        for s in cfg.get("servers", []):
            if s.get("visible", True) is False:
                continue
            result.append({
                "name": s["name"],
                "type": s.get("type", "http"),
                "url": s.get("url", ""),
                "enabled": s.get("enabled", True),
                "connected": s["name"] in self._connections,
                "tool_count": len(self._connections[s["name"]]["tools"]) if s["name"] in self._connections else 0,
            })
        return result

    # ── 连接 ────────────────────────────────
    async def connect(self, server_name: str) -> list[dict]:
        """连接指定 server，返回工具列表"""
        if server_name in self._connections:
            return self._connections[server_name]["tools"]

        cfg = _load_config()
        server_cfg = None
        for s in cfg.get("servers", []):
            if s["name"] == server_name:
                server_cfg = s
                break
        if not server_cfg:
            raise ValueError(f"未找到 MCP Server 配置: {server_name}")

        srv_type = server_cfg.get("type", "http")

        if srv_type == "http":
            return await self._connect_http(server_name, server_cfg)
        elif srv_type == "stdio":
            return await self._connect_stdio(server_name, server_cfg)
        else:
            raise ValueError(f"不支持的传输类型: {srv_type}")

    async def _connect_http(self, name: str, cfg: dict) -> list[dict]:
        url = cfg["url"]
        headers = cfg.get("headers", {})

        # streamablehttp_client 是一个 async context manager
        transport_cm = streamablehttp_client(url=url, headers=headers)
        streams = await transport_cm.__aenter__()
        read_stream, write_stream, _ = streams

        session = ClientSession(read_stream, write_stream)
        await session.__aenter__()
        await session.initialize()

        tools_result = await session.list_tools()
        tools = [self._tool_to_dict(t) for t in tools_result.tools]

        self._connections[name] = {
            "session": session,
            "transport_cm": transport_cm,
            "tools": tools,
        }
        logger.info(f"[MCP] 已连接 {name}，{len(tools)} 个工具可用")
        return tools

    async def _connect_stdio(self, name: str, cfg: dict) -> list[dict]:
        cmd = cfg.get("command", "")
        args = cfg.get("args", [])

        params = StdioServerParameters(command=cmd, args=args)
        transport_cm = stdio_client(params)
        streams = await transport_cm.__aenter__()
        read_stream, write_stream = streams

        session = ClientSession(read_stream, write_stream)
        await session.__aenter__()
        await session.initialize()

        tools_result = await session.list_tools()
        tools = [self._tool_to_dict(t) for t in tools_result.tools]

        self._connections[name] = {
            "session": session,
            "transport_cm": transport_cm,
            "tools": tools,
        }
        logger.info(f"[MCP] 已连接 {name} (stdio)，{len(tools)} 个工具可用")
        return tools

    # ── 断开 ────────────────────────────────
    async def disconnect(self, server_name: str):
        conn = self._connections.pop(server_name, None)
        if not conn:
            return
        try:
            await conn["session"].__aexit__(None, None, None)
        except Exception as e:
            logger.warning(f"[MCP] 关闭 session 异常: {e}")
        try:
            await conn["transport_cm"].__aexit__(None, None, None)
        except Exception as e:
            logger.warning(f"[MCP] 关闭 transport 异常: {e}")
        logger.info(f"[MCP] 已断开 {server_name}")

    # ── 调用工具 ────────────────────────────
    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> Any:
        conn = self._connections.get(server_name)
        if not conn:
            raise RuntimeError(f"MCP Server {server_name} 未连接")
        result = await conn["session"].call_tool(tool_name, arguments)
        # 将 result.content 序列化为可 JSON 化的格式
        contents = []
        for item in result.content:
            if hasattr(item, "text"):
                contents.append({"type": "text", "text": item.text})
            elif hasattr(item, "data"):
                contents.append({"type": "blob", "data": str(item.data)[:500]})
            else:
                contents.append({"type": "unknown", "value": str(item)[:500]})
        return contents

    # ── 工具列表 → OpenAI function calling 格式 ──
    def get_tools_for_ai(self, server_name: str) -> list[dict]:
        conn = self._connections.get(server_name)
        if not conn:
            return []
        result = []
        for tool in conn["tools"]:
            result.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                }
            })
        return result

    # ── 辅助 ────────────────────────────────
    @staticmethod
    def _tool_to_dict(tool) -> dict:
        return {
            "name": tool.name,
            "description": getattr(tool, "description", "") or "",
            "input_schema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
        }

    def is_connected(self, server_name: str) -> bool:
        return server_name in self._connections

    def get_tools(self, server_name: str) -> list[dict]:
        conn = self._connections.get(server_name)
        return conn["tools"] if conn else []


# 全局单例
mcp_manager = MCPManager()
