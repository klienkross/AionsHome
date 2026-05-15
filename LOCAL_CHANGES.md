# 本地改动摘要

记录本 fork 相对于 upstream (`death34018-hue/AionsHome`) 的主要差异，**供合并上游时快速判断冲突归属和处理策略**。

上游同步进度：截止 `886874b`（2026-05-14），所有提交已评估/合并/跳过。

---

## 合并速查：上游改了某文件时怎么办

| 上游改动的文件 | 处理策略 |
|---|---|
| `memory.py` | 我们重构过（Ebbinghaus 衰减 + numpy 缓存 + 原子卡片），且哨兵/向量调用已迁移到 `sentinel.py`。需逐段判断上游改动是否涉及我们已迁移的部分 |
| `sentinel.py` | **我们独有**，上游没有此文件。上游的哨兵/向量改动（如 736d862）在我们这里对应 sentinel.py 的改动 |
| `camera.py` | 我们已改为 `from sentinel import call_sentinel`。上游若改哨兵调用方式 → 跳过；改监控逻辑 → 正常合 |
| `config.py` | 我们多了 `dashscope_key`、`custom_keys`。上游加 `get_sentinel_config()` / `get_embedding_config()` → 跳过（我们用 sentinel.py） |
| `ai_providers.py` | 我们加了重试、日志、CLI provider。上游改动需逐段 review |
| `routes/chat.py` | 双方都频繁改。我们多了 Memory V2、THINK、Obsidian、情绪标注。按"插槽"模式合（新指令加在管线对应位置） |
| `chat.html/js/css` | 我们改过 bug（033a05b 去硬编码）、重构过 UI。上游 UI 改动需手动 resolve |
| `routes/settings.py` | 我们多了 sentinel/embedding 配置字段、MiMo TTS、DEFAULT_MODEL。上游加新配置字段 → 正常加 |
| `context_builder.py` | 我们独有，上游同名文件结构不同。上游改能力声明 → 在我们的 context_builder.py 里也加一份 |

---

## 核心架构差异

### 哨兵 & 向量：sentinel.py（我们独有）
- 上游：哨兵/向量调用散落在 memory.py、camera.py 等，硬编码 Gemini → 后改为可配置
- 我们：统一抽成 `sentinel.py` 模块，走 DashScope OpenAI 兼容端点，前端 settings 可配置 base_url/api_key/model
- **合并规则**：上游对哨兵/向量的改动直接跳过，我们在 sentinel.py 里独立演进

### 记忆系统：Memory V2
- 上游：memory.py 原版（简单 embedding + recall）
- 我们：Ebbinghaus 衰减引擎 + numpy 向量缓存 + 原子卡片（memory_cards.py）+ digest_v2.py + active_recall.py
- **合并规则**：逐段对比，区分"哨兵/向量调用改动"（已在 sentinel.py 实现）和"记忆逻辑改动"（需要手动融合）

### TTS：MiMo 引擎
- 上游：硅基流动 TTS
- 我们：MiMo-V2.5 TTS + 朗读系统（reading.py、ReadingSession）
- **合并规则**：上游 TTS 相关改动跳过

---

## 我们独有的模块（上游没有）

- `sentinel.py` — 哨兵/向量统一调用
- `memory_cards.py` — 原子卡片 CRUD
- `digest_v2.py` — V2 Digest 引擎
- `active_recall.py` — 主动记忆检索
- `sensor.py` — 传感器事件驱动
- `location.py` — 地理围栏（大幅重写）
- `reading.py` — 朗读引擎
- `obsidian.py` — 日记读取/搜索
- `ghost_forest.py` — 鬼林（改用 DashScope）
- `tts.py` — MiMo TTS
- `ntfy_bridge.py` — ntfy.sh 中转
- `webhook_ai.py` — AI 消息生成管道
- `sync_to_cloud.py` — 云端同步
- `routes/webhooks.py` — Webhook 端点

---

## 上游已合并的功能改动

| 上游 commit | 内容 | 合并状态 |
|---|---|---|
| `2ecccaa` | Gemini CLI 工具调用开关 + GEMINI.md | ✅ 已合 |
| `eb160ee` | 监控系统桌面截图 | ✅ 已合 |
| `736d862` | 哨兵/向量可配置 + 钱包 + MODELS 更新 | ✅ 钱包已合；sentinel 用我们自己的方式实现；DEFAULT_MODEL 未跟（保持 gemini-3-flash） |
| `5bc5dc3` | 小米智能家居 MCP | ✅ 已合 |
| `b884eb2` | 群聊接入智能家居 | ✅ 已合 |
| `886874b` | 阅读批注 bug 修复 | ⏭ 跳过（上游自己引入的问题，我们不受影响） |

---

*最后更新: 2026-05-15*
