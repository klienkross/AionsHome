# 阿里云接管哨兵与向量模型 - 设计文档

**日期**:2026-04-21
**范围**:将项目内所有 Gemini 小模型调用(哨兵/摘要/话题切点/日记提取/对话压缩)与 Gemini embedding 全部切换为阿里云百炼(DashScope)OpenAI 兼容端点
**动机**:Gemini free tier RPM 限制严苛(现有节流被迫 4 秒/次),且直连 `generativelanguage.googleapis.com` 需要科学上网。阿里云百炼国内直连 + RPM 宽松(qwen-flash 1200 RPM)+ 中文质量不差。

---

## 一、选型

| 角色 | 模型 | 端点 | 维度/说明 |
|---|---|---|---|
| 所有哨兵调用(RAG 路由、话题切点、摘要生成、视频哨兵、位置哨兵、日记提取) | `qwen-flash` | `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions` | JSON mode |
| 对话压缩(ghost_forest) | `qwen-flash` | 同上 | 普通文本 |
| 向量模型 | `text-embedding-v4` | `https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings` | 1024 维 |

调用协议:OpenAI 兼容,`Authorization: Bearer <dashscope_key>`。

哨兵请求体:
```json
{
  "model": "qwen-flash",
  "messages": [{"role": "user", "content": "<prompt>"}],
  "response_format": {"type": "json_object"}
}
```

Embedding 请求体:
```json
{
  "model": "text-embedding-v4",
  "input": "<text>",
  "dimensions": 1024,
  "encoding_format": "float"
}
```

---

## 二、Key 管理

[aion-chat/config.py](../../../aion-chat/config.py) 中 `DEFAULT_SETTINGS` 的 `keys` 默认字典新增:

```python
"dashscope_key": ""
```

`get_key("dashscope")` 返回该 key。

现有 `gemini_free_key` / `gemini_key` / `aipro_key` / `siliconflow_key` 全部保留,不动(主聊天 Gemini provider 仍可用,前台用户自选是否挂梯子)。

用户在 `data/settings.json` 手动填写一次 `dashscope_key` 即可。

---

## 三、抽出公共 helper:`sentinel.py`

新建 [aion-chat/sentinel.py](../../../aion-chat/sentinel.py),对外暴露两个函数:

```python
async def call_sentinel(
    prompt: str,
    *,
    timeout: int = 30,
    max_retries: int = 2,
    model: str = "qwen-flash",
) -> dict | None:
    """DashScope OpenAI 兼容 chat + JSON mode + 重试 + JSON 解析。失败返回 None。
    内置全局软节流(见第四节)。"""

async def call_sentinel_text(
    prompt: str,
    *,
    timeout: int = 30,
    max_retries: int = 2,
    model: str = "qwen-flash",
) -> str | None:
    """同上,但返回纯文本(用于 ghost_forest 对话压缩等非 JSON 场景)。"""

async def get_embedding(text: str) -> list[float] | None:
    """DashScope text-embedding-v4,1024 维。失败返回 None。"""
```

错误处理:
- 429/503:指数退避重试(4s → 8s),最多 `max_retries` 次
- 连接错误(`httpx.ConnectError`/`ConnectTimeout`/`ReadTimeout`/`RemoteProtocolError`):同上重试
- 其他异常:直接返回 `None`
- JSON 解析失败:尝试从 ``` 包裹中提取,失败则返回 `None`

接入点改造:

| 文件 | 原调用 | 改为 |
|---|---|---|
| [memory.py](../../../aion-chat/memory.py) | `_call_flash_lite()` 内联 httpx | 删函数,改调 `sentinel.call_sentinel()` |
| [memory.py](../../../aion-chat/memory.py) | `instant_digest()` 内联 httpx | 改调 `sentinel.call_sentinel()` |
| [memory.py](../../../aion-chat/memory.py) | `get_embedding()` 内联 httpx | 删实现,从 `sentinel` 模块 re-export(避免影响外部 import 路径) |
| [camera.py:522](../../../aion-chat/camera.py) | 内联 httpx → flash-lite | `await call_sentinel(prompt)` |
| [location.py:679](../../../aion-chat/location.py) | 内联 httpx → flash-lite | `await call_sentinel(prompt)` |
| [obsidian.py:54](../../../aion-chat/obsidian.py) | 内联 httpx → flash-lite | `await call_sentinel(prompt)`(原 prompt 返回结构视实现而定,若返回纯文本则用 `call_sentinel_text`) |
| [ghost_forest.py:226](../../../aion-chat/ghost_forest.py) | `COMPRESS_MODEL = "gemini-3.1-flash-lite"` | 删常量,调用点改用 `call_sentinel_text()` |

**所有 prompt 文本保持不变**,只改调用底座。

`memory.py` 中保留 `get_embedding` 的公开 import(`from memory import get_embedding` 已有多处引用 [routes/chat.py:17](../../../aion-chat/routes/chat.py) 等),方式是 `from sentinel import get_embedding` 再 re-export。

---

## 四、节流

DashScope qwen-flash 共享配额 1200 RPM。

- 全局软节流 `_SENTINEL_MIN_INTERVAL = 0.3` 秒(原 Gemini 为 4.0)
- 节流逻辑沿用现有实现(模块级时间戳 + `asyncio.sleep` 补齐间隔)
- Embedding 不加节流(TPM 限制更宽)

---

## 五、向量维度变更与数据迁移

**变化**:
- `EMBEDDING_DIMS` 从 3072 → 1024
- [aion-chat/memory.py:15-16](../../../aion-chat/memory.py#L15) 相关常量同步更新(或移入 `sentinel.py`)
- 数据库 schema 不变(`embedding` 仍是 BLOB)

**迁移步骤**(手动,用户执行):

1. 确认备份存在:
   - `aion-chat/data/chat.db.bak-20260421-144240`
   - `aion-chat/data/digest_anchor.json.bak-20260421-144240`
   
   (用户已做,见 git status)
2. 在 `aion-chat/data/settings.json` 填入 `dashscope_key`
3. 切到新代码(本 spec 实现完成后)
4. 执行 `cd aion-chat && python rebuild_memories.py`
5. 脚本流程(无需修改逻辑):清空 `memories` 表 → 重置 `digest_anchor` → 调 `manual_digest()` 用新模型全量重建
6. 观察输出,确认新 memories 行数合理

[rebuild_memories.py](../../../aion-chat/rebuild_memories.py) 文案里 "flash-lite" 改为 "sentinel",不改逻辑。

**回滚**:重命名 `.bak` 文件覆盖即可。

---

## 六、不动的部分

- [ai_providers.py](../../../aion-chat/ai_providers.py) 主聊天 gemini/aipro/siliconflow/custom provider 全部保留
- [config.py](../../../aion-chat/config.py) 的 `MODELS` 字典不动(前台可选 gemini 模型仍在列表里)
- 数据库 schema 不变
- WebSocket / 路由 / 前端 UI 一律不动
- `ghost_forest.py:102` `create_session` 默认 `model="gemini-2.5-flash"` 这是用户可选的对话模型,不是后台哨兵调用,保留

---

## 七、风险与注意

1. **JSON 遵从率**:qwen-flash `response_format: json_object` 要求 prompt 中出现 "JSON" 字样。现有所有哨兵 prompt 末尾均有 "严格只输出一个 JSON 对象",满足。兜底保留现有的 ``` 包裹提取逻辑。
2. **Embedding 质量漂移**:v4 在中文 MTEB 榜单靠前,理论召回不差,但阈值敏感。`recall_memories` 的 `threshold=0.45`、`build_surfacing_memories` 的 `sim >= 0.50`:**先按原值跑**,如果发现召回过宽/过窄再手调。
3. **依赖项**:无新增依赖(复用 `httpx`)。
4. **成本**:qwen-flash 输入 ¥0.15/M tok、输出 ¥1.5/M tok,embedding-v4 ¥0.5/M tok。个人使用一天撑死几分钱。

---

## 八、成功标准

- 项目启动后 Gemini free key 可以设为空,全功能依然正常(除用户主动选的 gemini 主聊天模型外)
- `grep -r "generativelanguage.googleapis.com" aion-chat/` 不出现在哨兵/embedding 相关路径(`ai_providers.py` 的主聊天 gemini provider 路径除外)
- `rebuild_memories.py` 跑完后:memories 行数 > 0,且前端"记忆浮现"、RAG 召回、视频哨兵、位置哨兵、日记摘要功能均正常
- 不需要科学上网即可完整使用(除用户主动选的 gemini 主聊天模型外)
