# chain_hash 测试套件设计

**目标：** 为链式哈希功能添加全栈测试（核心逻辑 + 数据库集成 + API 端点）

**测试文件：** `aion-chat/tests/test_chain_hash.py`

## Group 1: CRC32 纯逻辑

- CRC32 标准向量 `b"123456789"` → `0xCBF43926`
- 空字节、中文 UTF-8、长字符串边界

## Group 2: 数据库集成

- 复用 `setup_test_db()` 模式（临时 SQLite）
- `init_db()` 后两表有 `chain_hash` 列
- INSERT 消息时 chain_hash 正确计算并入库
- 链式断裂验证：改前一条消息导致后续哈希全变

## Group 3: API 端点

- 用 `httpx.AsyncClient` + FastAPI TestClient
- `GET /api/conversations/{id}/hash` → 返回 hash + count
- `POST /api/conversations/{id}/hash/verify` → 比对逻辑
- 群聊对应端点

## 运行方式

```bash
python aion-chat/tests/test_chain_hash.py
```
