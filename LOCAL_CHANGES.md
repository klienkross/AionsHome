# 本地改动摘要

记录本 fork 相对于 upstream (`death34018-hue/AionsHome`) 的主要差异，**供合并上游时快速判断冲突归属和处理策略**。

上游同步进度：截止 `7cae860`（2026-05-18），表内上游提交已评估/合并/跳过；`c24f53c`（2026-05-19）以 merge 方式落地了下表新增提交。
> 下次合并前 fetch upstream 复核增量即可。

---

## 合并速查：上游改了某文件时怎么办

| 上游改动的文件 | 处理策略 |
|---|---|
| `memory.py` | 我们重写过（哨兵/向量调用迁到 `sentinel.py`；Memory V2 召回：并集候选 + BGE-reranker 精排）。逐段判断上游改动是否涉及我们已迁移/重写的部分 |
| `sentinel.py` | **我们独有**，上游没有此文件。上游的哨兵/向量改动（如 736d862）在我们这里对应 sentinel.py |
| `camera.py` | 我们已改为 `from sentinel import call_sentinel`。上游若改哨兵调用方式 → 跳过；改监控逻辑 → 正常合 |
| `config.py` | 我们多了 `dashscope_key`、`mimo_key`、`custom_keys`、`obsidian_vault_path`、`sentinel_*`/`embedding_*`。上游加 `get_sentinel_config()` / `get_embedding_config()` → 跳过（我们用 sentinel.py） |
| `database.py` | 我们多了 `chain_hash` 字段迁移 + Memory V2 表（`memory_cards` 等）。上游改表结构 → 逐段判断，勿覆盖我们的迁移 |
| `ai_providers.py` | 我们加了重试、日志、CLI provider。上游改动需逐段 review |
| `routes/chat.py` | 双方都频繁改。我们多了 Memory V2、THINK/THINK_SCHEDULE、Obsidian 指令、情绪标注。按"插槽"模式合（新指令加在管线对应位置） |
| `chat.html` / `chat.js` / `chat.css` | **分歧极大**：我们已全站迁移 Web Components（`components.js`）+ 抽取 `chat-core.js`（滚动/上传预览/格式化/图片查看器/主题/音效）。上游 UI 改动基本需手动重写到新结构，不能直接合 |
| 其它 `static/*.html` | 多数页面已 Web Components 化（fund/gift/location/memory/schedule/settings/worldbook/monitor-logs/activity-logs/chatroom 等）。上游改这些页面 → 对照组件手动 resolve |
| `static/sw.js` | 我们改为 stale-while-revalidate + 静态资源缓存。上游改 SW → 跳过或手动融合 |
| `routes/settings.py` | 我们多了 sentinel/embedding 配置字段、MiMo TTS、DEFAULT_MODEL、钱包/Obsidian 等。上游加新配置字段 → 正常加 |
| `context_builder.py` | 我们独有，上游同名文件结构不同。上游改能力声明 → 在我们的 context_builder.py 里也加一份 |
| `mcp_client.py` / `routes/chatroom.py` | 小米智能家居 MCP 已合（5bc5dc3 / b884eb2）。上游再改 MCP → 逐段 review |

---

## 核心架构差异

### 哨兵 & 向量：sentinel.py（我们独有）
- 上游：哨兵/向量调用散落在 memory.py、camera.py 等，硬编码 Gemini → 后改为可配置
- 我们：统一抽成 `sentinel.py` 模块，走 DashScope OpenAI 兼容端点，前端 settings 可配置 base_url/api_key/model
- **合并规则**：上游对哨兵/向量的改动直接跳过，我们在 sentinel.py 里独立演进

### 记忆系统：Memory V2（含 Memory Evolution）
- 上游：memory.py 原版（简单 embedding + recall）
- 我们：原子卡片（`memory_cards.py`）+ V2 Digest 引擎（`digest_v2.py`，卡片拆分/情绪/对话强度）+ 主动检索整理（`active_recall.py`）+ Ebbinghaus 遗忘曲线后台归档（`decay_engine.py`）+ numpy 向量矩阵缓存（`embedding_cache.py`）+ BGE-reranker 精排 + 关键词∪向量并集召回 + 事实核查
- **合并规则**：逐段对比，区分"哨兵/向量调用改动"（已在 sentinel.py 实现）和"记忆逻辑改动"（需 user 检查后手动融合）；衰减/缓存/reranker 为我们独有，上游同类改动跳过

### WS 可靠性：链式哈希（我们独有）
- 上游：无
- 我们：消息写入计算链式哈希（CRC32，`chain_hash.py`），DB 字段迁移，心跳响应 + 每 30s 链式哈希校验 API，前端重连时校验，历史回填脚本（`scripts/backfill_hashes.py`）
- **合并规则**：上游无对应物，整体跳过冲突，我们独立演进

### 前端：Web Components 化（大幅重写）
- 上游：原生多页面，逻辑内联在各 html
- 我们：全站迁移 Web Components（`static/components.js`）+ 主聊天页逻辑抽取到 `static/chat-core.js`
- **合并规则**：上游前端改动不能直接合，需对照组件结构手动重写

### TTS：MiMo 引擎
- 上游：硅基流动 TTS
- 我们：小米 MiMo TTS + 朗读系统（`reading.py`，WS 驱动逐句朗读）
- **合并规则**：上游 TTS 相关改动跳过

---

## 我们独有的模块（上游没有）

后端：
- `sentinel.py` — 哨兵/向量统一调用（DashScope）
- `memory_cards.py` — Memory V2 原子卡片 CRUD
- `digest_v2.py` — V2 Digest 引擎
- `active_recall.py` — 主动记忆检索/整理
- `decay_engine.py` — Ebbinghaus 衰减引擎 + 后台归档循环
- `embedding_cache.py` — numpy 向量矩阵缓存
- `chain_hash.py` — WS 消息链式哈希（CRC32）
- `sensor.py` — 传感器事件驱动环境感知
- `location.py` — 地理围栏（大幅重写）
- `reading.py` — WS 驱动朗读引擎
- `obsidian.py` — Obsidian 日记读取/搜索
- `ghost_forest.py` — 鬼林（改用 DashScope）
- `tts.py` — 小米 MiMo TTS
- `ntfy_bridge.py` — ntfy.sh 中转
- `webhook_ai.py` — Webhook 触发的 AI 消息生成管道
- `sync_to_cloud.py` — 云端记忆同步
- `toy_adv.py` — BLE 广播玩具桥接
- `routes/webhooks.py` — Webhook 端点
- `routes/wallet.py` — 钱包 API
- `routes/toy_adv.py` — BLE 玩具控制 API
- `mcp_servers/home_assistant_server.py` — 小米智能家居 MCP Server

前端：
- `static/components.js` — Web Components 基建
- `static/chat-core.js` — 主聊天页逻辑模块（滚动/上传/格式化/查看器/主题/音效）

---

## 上游已合并的功能改动

下表提交均通过 `afc1dc3`（2026-05-15，squash `feat/upstream-merge-easy`）落地：

| 上游 commit | 内容 | 合并状态 |
|---|---|---|
| `2ecccaa` | Gemini CLI 工具调用开关 + GEMINI.md | ✅ 已合 |
| `eb160ee` | 监控系统桌面截图 | ✅ 已合 |
| `736d862` | 哨兵/向量可配置 + 钱包 + MODELS 更新 | ✅ 钱包已合（并重构面板）；sentinel 用我们自己的方式实现；未跟上游的 DEFAULT_MODEL 改动（我们 `DEFAULT_MODEL = next(iter(MODELS))` = 硅基GLM-5.1，走国内、免梯子） |
| `5bc5dc3` | 小米智能家居 MCP | ✅ 已合 |
| `b884eb2` | 群聊接入智能家居 | ✅ 已合 |
| `886874b` | 阅读批注 bug 修复 | ⏭ 跳过（上游自己引入的问题，我们不受影响） |

下表提交通过 `c24f53c`（2026-05-19，merge `feat/upstream-merge-2026-05-18`）落地：

| 上游 commit | 内容 | 合并状态 |
|---|---|---|
| `e47c12e` | 朋友圈/斗地主/步数/Connor钱包/双AI批注/日程智能分发 | ✅ 朋友圈、步数、Connor钱包、双AI批注、语音附件已合；前端 Web Components 保留；⏭ 跳过斗地主 |
| `7cae860` | 聊天室硬编码名称修复 + 鬣狗家族名称 | ✅ 已合（含动态名称重构） |

---

## fork 自身新增（2026-05-15 起，非上游）

| 范围 | 关键提交 | 说明 |
|---|---|---|
| Memory Evolution | `cb62111` | Ebbinghaus 衰减引擎 + numpy 向量缓存 + 事实核查 |
| 记忆召回 | `bd9f1cc` | BGE-reranker 精排，关键词+向量并集候选混合管道 |
| WS 链式哈希 | `29da487`→`d73f6ed` | 链式哈希模块 + DB 迁移 + 写入计算 + 校验 API + 前端重连校验 + 回填脚本 + 心跳响应 |
| 前端重构 | `18c75fe` / `caaf442`→`9eba7fe` | 全站 Web Components 迁移 + chat-core.js 抽取 |
| PWA | `072d13a` / `d73f6ed` | SW 静态资源缓存 + stale-while-revalidate + 清除缓存按钮 |
| 测试 | `ac2a05e` | 衰减引擎/Digest/链式哈希/向量缓存 58 用例 |

---

*最后更新: 2026-05-19*
