# Aion Chat 本地 fork 说明

对照 [README.md](README.md)，**只记录本 fork 相对 upstream (`death34018-hue/AionsHome`) 的功能增量**。
合并冲突的归属与处理策略见 [LOCAL_CHANGES.md](LOCAL_CHANGES.md)，实现细节见 [docs/](docs/)。

## 项目定位（增量）
在原版基础上新增：传感器/地理围栏环境感知、Obsidian 日记联动、Webhook 远程触发 AI、记忆库 V2（原子卡片 + 遗忘曲线）、云端记忆同步。

## 新手必看：fork 新增了哪些可接入 / 可配置项

> 这些是原版没有、本 fork 新加的「开关」。配上对应 key 就解锁能力，不配则该功能静默关闭。
> 配置位置有两处：`aion-chat/data/settings.json`（文本编辑）或 **前端设置页 `/settings`**（网页填）。

| 想解锁的能力 | 配置项 | 在哪配 | 说明 / 默认值 |
|---|---|---|---|
| **AI 哨兵 + 记忆向量**（核心，建议必配）| `dashscope_key` | `data/settings.json` | 阿里云百炼 API key。哨兵分析、记忆 embedding 全走它。默认端点 DashScope，模型 `qwen-flash` / 视觉 `qwen3-vl-flash` / 向量 `text-embedding-v4`（1024维）|
| 换一家哨兵/向量端点 | `sentinel_base_url` / `sentinel_api_key` / `sentinel_model` / `sentinel_vl_model` / `embedding_base_url` / `embedding_api_key` / `embedding_model` | 前端 `/settings` | 留空就用上面 DashScope 默认；想换别家 OpenAI 兼容服务在这填 |
| **语音合成 TTS** | `mimo_key` | `data/settings.json` | 小米 MiMo TTS（替代原版硅基流动 CosyVoice2），不配则无 TTS。**当前限时免费，强烈推荐配上** |
| **接任意第三方大模型**（Claude 中转站等）| `custom_keys` + `MODELS` 加一条 | `data/settings.json` + `config.py` | `"custom_keys": {"<名>": "sk-xxx"}`，再在 `config.py` 的 `MODELS` 里加 `{"provider":"custom","model":"上游模型名","base_url":"https://xxx/v1","key_name":"<名>"}` |
| **AI 读你的 Obsidian 日记** | `obsidian_vault_path` | `data/settings.json` | 指向本地 Obsidian vault 目录。配了之后 AI 才会获得 `[OBSIDIAN_READ/RECENT/SEARCH]` 日记指令 |
| **手机传感器/地理围栏喂给 AI** | `ntfy_enabled` + `ntfy_topic` | `data/settings.json` | 启用后订阅 ntfy.sh topic，手机（MacroDroid）事件经 `/api/webhooks` 进哨兵管道 |
| **智能家居 (Home Assistant)** | `ha_token` | `data/settings.json` | HA 长期访问令牌。MCP Server 读取优先级：环境变量 `HA_TOKEN` > `settings.json` > `home_assistant_mcp.json`。URL 等非敏感项仍在 `data/home_assistant_mcp.json` |

> 🎁 **MiMo TTS 当前限时免费**——目前性价比最高的语音方案，萌新建议第一个就配上它。

### AI 生图：两套独立方案

本 fork 有两个生图入口，使用**不同的模型和提供商**，注意区分：

| 功能 | 触发方式 | 模型 | 提供商 | 需要的 key | 需梯子 |
|---|---|---|---|---|---|
| **SELFIE / DRAW**（用户主动要求画图） | AI 回复 `[SELFIE:提示词]` 或 `[DRAW:提示词]` | 优先 `qwen-image-2.0-pro`，fallback `gemini-3.1-flash-image-preview` | 阿里云百炼 / Google Gemini | `dashscope_key`（优先）或 `gemini_key` | 百炼否 / Gemini 是 |
| **礼物生图**（AI 自动送礼时） | 记忆总结后 AI 判断送礼 | `Kwai-Kolors/Kolors` | 硅基流动 | `siliconflow_key` | 否 |

- SELFIE 模式支持参考图（`public/生图锚点.jpg`），Qwen-Image 和 Gemini 均支持，保证人物一致性；Kolors 不支持参考图
- **有 `dashscope_key` 就优先走 Qwen-Image（国内直连、无需梯子）**，Qwen 失败才 fallback Gemini；都没有 key 则 SELFIE/DRAW 不可用
- 前端设置页的「AI 生图」开关（`image_gen_enabled`）只控制 SELFIE/DRAW，礼物生图始终跟随记忆总结自动触发

## 不用梯子也能跑

本 fork 相对原版最实用的改动之一：**核心链路全部改走国内可直连服务，整套程序无需梯子即可运行**。

- 原版强依赖 Google Gemini（哨兵分析、记忆 embedding、Gemini CLI、AI 生图）——这些在国内必须挂梯子。
- 本 fork 把主链路换成国内服务：
  - **哨兵 + 记忆向量** → 阿里云百炼 DashScope（`sentinel.py`）
  - **主聊天模型** → 默认硅基流动（`config.py` 的 `MODELS` 第一项，非 Gemini）
  - **语音 ASR** → 硅基流动 SenseVoiceSmall
  - **语音合成 TTS** → 小米 MiMo
  - 音乐 / 基金 / 高德定位本就是国内服务
- **结论**：聊天、记忆、语音、监控哨兵这条主链路，配好上表的国内 key 即可，全程不用梯子。
- 仍需梯子的只剩**可选项**：Gemini 系模型、Gemini CLI、ntfy.sh 公网中转——不碰这些完全不影响主程序。SELFIE/DRAW 生图现已支持 Qwen-Image（百炼），配了 `dashscope_key` 即可国内直连；礼物生图走硅基流动 Kolors，同样不需要梯子。

## 技术栈（增量）
- **哨兵/向量改用阿里云百炼 DashScope**（OpenAI 兼容端点）：哨兵 `qwen-flash` / 视觉 `qwen3-vl-flash`，Embedding `text-embedding-v4`（1024维）——替代原版散落的 Gemini 哨兵/embedding 调用，前端可配置 base_url/key/model
- **TTS 改用小米 MiMo TTS** —— 替代原版硅基流动 CosyVoice2
- **记忆检索**：numpy 向量矩阵缓存（启动加载、批量余弦）+ BGE-reranker 精排 + 关键词∪向量并集候选
- **远程接入**：新增 ntfy.sh JSON 流桥接、MacroDroid/Tasker webhook（aiohttp）
- **配置**：新增一批可接入开关（DashScope 哨兵/向量、MiMo TTS、自定义 OpenAI 兼容端点、Obsidian、ntfy webhook）——详见上方〈新手必看〉表

## 功能（增量）
- **记忆系统 V2**：原子卡片 CRUD（event/preference/emotion/promise/plan/fact/aggregate + 关系链）；V2 Digest 引擎（卡片拆分 + 情绪评价 + 对话强度）；Ebbinghaus 遗忘曲线引擎后台定期归档低活跃卡片；主动检索与整理（`organize_memories` / `execute_mem_edit`）
- **Obsidian 日记联动**：AI 指令 `[OBSIDIAN_READ:日期]` / `[OBSIDIAN_RECENT:N]` / `[OBSIDIAN_SEARCH:关键词]`，自动读取本地 vault 并回注上下文
- **后台思考**：`[THINK:想法]` 静默思考（结果不发用户，可后续引用）、`[THINK_SCHEDULE:HH:MM|daily|内容]` 每日定时思考
- **传感器环境感知**：MacroDroid webhook → 15 分钟事件窗口累积 → 哨兵分析；地理围栏事件（60s debounce、高优先级即时处理）
- **Webhook 远程触发 AI**：`/api/webhooks` 通用 handler 管道（channel→handler 注册式扩展），内置夜间手机活动检测
- **ntfy.sh 桥接**：订阅公网 topic，消息转发进传感器管道
- **钱包系统**：余额查询 / 转账记录 / 入账 API
- **BLE 广播玩具控制**：`toy_adv` 桥接外部 ToyController 构建 BLE 广播 payload
- **云端记忆同步**：`sync_to_cloud` 推送聊天记录与记忆库到独立 `Aions_memory` 仓库
- **WS 可靠性**：心跳响应 + 每 30s 链式哈希（CRC32）校验消息序列
- **鬼林 TRPG**：改用 DashScope 驱动

## 架构（增量）

### 全新功能（原版完全没有的能力）
- `sensor.py` — 传感器事件驱动环境感知（手机传感器/地理围栏 → 哨兵）
- `obsidian.py` — 读取用户外部 Obsidian vault 日记 / 搜索 / 摘要
- `ntfy_bridge.py` — ntfy.sh 公网中转桥接（绕过 MacroDroid 直连防火墙）
- `webhook_ai.py` + `routes/webhooks.py` — Webhook 触发 AI 消息（MacroDroid 夜间手机提醒等）
- `sync_to_cloud.py` — 记忆/聊天总结后自动同步到云端独立仓库

### 对原版功能的抽象 / 重构 / 补强（原版已有，被抽出或改写）
- `sentinel.py` — 原版哨兵/向量散在 `memory.py`/`camera.py`（硬编码 Gemini），**抽象**为独立模块 + 改走 DashScope
- `memory.py`（重写）— 底层哨兵/向量改走 sentinel
- **Memory V2**（重构原版「向量记忆库」）：`memory_cards.py` 原子卡片 / `digest_v2.py` V2 Digest / `active_recall.py` 主动检索 / `decay_engine.py` Ebbinghaus 衰减 / `embedding_cache.py` numpy 缓存
- `chain_hash.py` — 在原版 WS 上**补充**消息链式哈希完整性校验
- `location.py`（大幅重写）— 原版高德定位 + 新增地理围栏状态机 / 哨兵联动
- `tts.py`（重写）— 原版 TTS 重写为小米 MiMo 流式
- `reading.py` — **重写**原版「AI 陪伴阅读」的逐句 TTS 朗读（prefetch）
- `routes/wallet.py` — 上游钱包功能从 `chat` **抽出**独立 route + 面板重构
- `routes/toy_adv.py` + `toy_adv.py` — 在原版「密语时刻」BLE 玩具上**集成** ToyController 广播模式
- `mcp_servers/home_assistant_server.py` — 上游小米智能家居 MCP（`afc1dc3` 合入）

---

*与 README 一一对照；合并上游前必读 [LOCAL_CHANGES.md](LOCAL_CHANGES.md)。*
