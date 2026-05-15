# Memory Evolution：记忆系统改造设计

> 基于 Ombre-Brain 的遗忘曲线理念，在现有 SQLite + memory_cards 体系上做渐进增强。

## 背景

现有记忆系统（digest_v2 + memory.py）已有情感坐标、向量搜索、卡片生命周期、链式聚合等功能，但存在以下问题：

1. **硬规则生命周期**：按 type + age 强制 close 卡片，不符合人类记忆规律
2. **召回性能瓶颈**：2000 条卡片纯 Python 余弦计算已需 ~500ms，半年后将不可接受
3. **情感评分全部为零**：Agent B 解析逻辑与 sentinel 返回格式不匹配，2033 条卡片 100% 双零
4. **AI 幻觉固化**：Ari 编造/歪曲的历史事件被 digest 写入卡片，形成幻觉正反馈循环
5. **回忆重复生卡**：Ari 在对话中提起旧记忆时，digest 将其当作新事件提取
6. **去重范围过窄**：仅在同一 conversation 内去重，跨天内容无法去重
7. **卡片量过大**：日均 80~160 条，大量低重要度琐碎卡片

## 方案选择

**方案 A（渐进增强）**：在现有代码上逐模块改造，借鉴 Ombre-Brain 的公式和理念，不引入其代码依赖。

理由：现有系统在链式聚合、memory_links 方面比 Ombre-Brain 更成熟；Ombre-Brain 的存储模型（Markdown bucket）与 SQLite 不兼容；真正需要引入的核心只有遗忘曲线公式。

## 改动清单

### 1. Ebbinghaus 遗忘曲线

**替代**：`digest_v2.py` 末尾的 auto-close 硬规则（按 type + importance + age 强制关闭卡片）。

**衰减公式**：

```
vitality = importance × (activation_count ^ 0.3) × e^(-λ × days) × emotion_weight
```

- `λ = 0.05`（衰减速率，可在 settings.json 的 `decay.lambda` 调节）
- `activation_count`：被召回次数，每次命中 +1
- `emotion_weight = 1.0 + |valence| × 0.3 + arousal × 0.2`
- `days = (now - last_activated) / 86400`（距上次激活的天数，而非创建时间）
- 新卡片 `last_activated` 初始值为 `created_at`（创建即视为一次激活）

**归档规则**：
- `vitality < 0.1` → `status = 'archived'`（新状态，召回时跳过，数据不删）
- `pinned` / `unresolved` 的卡片不参与衰减
- aggregate 卡片跟随其链中最新卡片的 vitality

**后台任务**：每 6 小时扫描一次 `status='open'` 的卡片，计算 vitality 并归档低于阈值的。替代现有 auto-close 逻辑。

**数据库改动**：

```sql
ALTER TABLE memory_cards ADD COLUMN activation_count INTEGER DEFAULT 0;
ALTER TABLE memory_cards ADD COLUMN last_activated REAL;
-- status 新增 'archived' 值（无需 DDL，TEXT 字段）
```

**涉及文件**：
- 新建 `decay_engine.py`：衰减计算 + 后台归档任务
- `main.py`：lifespan 中启动衰减任务
- `digest_v2.py`：移除 auto-close 硬规则（约 520~540 行）
- `database.py`：新增字段迁移

---

### 2. numpy 向量化加速召回

**替代**：`memory.py` 中逐条 `cosine_similarity` 的纯 Python 循环。

**实现**：
- 启动时（或首次召回时）将所有 embedding 加载为 `np.ndarray` 矩阵，缓存在内存中
- 召回时 `query_vec @ matrix.T` 一次算出全部相似度
- 新卡片创建时追加到矩阵（`np.vstack`）
- 卡片删除/归档时标记为 invalid，定期 compact
- 纯内存缓存，启动时从 DB 加载，无需额外持久化

**预期性能**：
- 2000 条：~500ms → ~5ms
- 20000 条：~4.7s → ~50ms

**涉及文件**：
- `memory.py`：`recall_memories`、`build_surfacing_memories` 改用矩阵计算
- 新建 `embedding_cache.py`：管理 numpy 矩阵的加载/追加/compact

---

### 3. 去重范围扩大

**替代**：`_dedup_against_realtime` 的 `WHERE source_conv=?`。

**改为**：

```sql
WHERE embedding IS NOT NULL
  AND created_at > ?   -- now - 7天
  AND status IN ('open', 'closed')
```

复用 numpy 矩阵计算相似度，阈值不变（0.85）。

**涉及文件**：
- `digest_v2.py`：`_dedup_against_realtime` 函数

---

### 4. Prompt 改造

**替代**：现有 separate 模式（Agent A + Agent B 分离调用）和 unified prompt。

**改为单一 unified prompt**，主要改进：

#### 数量与质量控制
- 每组对话限制 2~8 张卡片
- 单卡 content 不少于 30 字，过短的合并
- 同一主题的零散信息合并为一个条目
- importance < 0.2 的内容不生成

#### 过滤规则
- 去除口水话、打招呼、重复信息
- AI 复述/回忆过去事件不提取为新卡片，除非用户补充了新信息

#### 来源标注
- 新增 `source` 字段：`"user"` / `"ai"` / `"both"`
- AI 单方面声称的历史事件标记 `"ai"`，后续进入事实核查

#### 情感评分修复
- valence / arousal 在 unified prompt 中直接输出
- temperature 设为 0.0

**输出格式**：

```json
{
  "content": "...",
  "type": "event|preference|emotion|promise|plan|fact",
  "keywords": ["领域词", "实体词1", "实体词2"],
  "importance": 0.3,
  "unresolved": false,
  "valence": 0.4,
  "arousal": -0.2,
  "source": "user"
}
```

**涉及文件**：
- `digest_v2.py`：`_build_unified_prompt`、`_build_agent_a_prompt`（废弃）、`_build_agent_b_prompt`（废弃）、`_parse_emotion_output`（废弃）

---

### 5. 事实核查流程

在 digest Phase 3（建卡）后新增 **Phase 3.5：事实核查**。

**仅对 `source="ai"` 的卡片执行**（约 5% 的卡片量）：

```
1. 提取卡片中的关键词
2. 在 messages 表中搜索（往前 30 天范围，匹配 role='user' 的消息）
3. 找到匹配 → verified = 1，保留
4. 找不到 → 调 sentinel 二次判断：
   - 传入卡片内容 + 对话上下文片段
   - 问："这条记忆描述的事件在对话记录中有直接证据吗？"
   - 判定无依据 → 丢弃卡片，打印日志 "[digest_v2] 丢弃未核实卡片: ..."
```

**数据库改动**：

```sql
ALTER TABLE memory_cards ADD COLUMN source TEXT DEFAULT 'both';
ALTER TABLE memory_cards ADD COLUMN verified INTEGER DEFAULT 1;
```

**涉及文件**：
- `digest_v2.py`：新增 `_verify_ai_claims` 函数
- `database.py`：新增字段迁移

---

### 6. 情感评分接入公式

现有召回公式不使用 valence/arousal。改造后：

**召回公式**：

```
base_score  = kw_score × 0.5 + vec_sim × 0.3 + importance × 0.2
final_score = base_score × vitality
```

vitality 已包含 emotion_weight，所以情感自然参与了召回排序：高情感记忆衰减慢 → vitality 高 → 排名靠前。

**涉及文件**：
- `memory.py`：`recall_memories`、`build_surfacing_memories`

---

### 7. surfaced_ids 持久化

**目的**：让 digest 知道哪些记忆在对话中被召回过，用于去重和聚合。

**存储**：messages 表新增 `surfaced_memory_ids TEXT`（JSON 数组），每条 assistant 消息记录本轮注入的记忆 ID。

**数据库改动**：

```sql
ALTER TABLE messages ADD COLUMN surfaced_memory_ids TEXT;
```

**digest 使用**：
- 读取当前 group 时间范围内所有 assistant 消息的 surfaced_memory_ids
- 合并为 `surfaced_set`
- 新卡片与 surfaced_set 中的卡片做向量相似度比较：
  - 相似度 ≥ 0.85 → 跳过（旧记忆的复述）
  - 相似度 0.65~0.85 → 创建 `follow_up` link 到旧卡片（用户补充了新信息）
  - 相似度 < 0.65 → 正常建卡
- 召回命中的卡片 `activation_count += 1`（衰减续命）

**涉及文件**：
- `routes/chat.py`：保存 surfaced_ids 到 messages 表
- `digest_v2.py`：读取 surfaced_ids，新增对比逻辑
- `database.py`：新增字段迁移

---

## 数据库改动汇总

```sql
-- memory_cards 新增字段
ALTER TABLE memory_cards ADD COLUMN activation_count INTEGER DEFAULT 0;
ALTER TABLE memory_cards ADD COLUMN last_activated REAL;
ALTER TABLE memory_cards ADD COLUMN source TEXT DEFAULT 'both';
ALTER TABLE memory_cards ADD COLUMN verified INTEGER DEFAULT 1;

-- messages 新增字段
ALTER TABLE messages ADD COLUMN surfaced_memory_ids TEXT;
```

## 新增文件

| 文件 | 职责 |
|------|------|
| `decay_engine.py` | Ebbinghaus 衰减计算 + 后台归档任务 |
| `embedding_cache.py` | numpy 矩阵管理（加载/追加/compact/相似度计算） |

## 改动文件

| 文件 | 改动范围 |
|------|----------|
| `digest_v2.py` | prompt 重写、废弃 Agent B、事实核查、surfaced 去重、移除 auto-close |
| `memory.py` | 召回公式改造、activation_count 更新、使用 embedding_cache |
| `memory_cards.py` | create_card 适配新字段 |
| `routes/chat.py` | 持久化 surfaced_ids |
| `database.py` | 字段迁移 |
| `main.py` | 启动衰减后台任务 |

## 不改动的部分

- 存储格式保持 SQLite（不换 Markdown）
- memory_links / 链式聚合逻辑保持不变
- API 路由（routes/memories.py）保持兼容
- 前端页面不需要改动（memory.html 等）
- 关键词匹配保持现有子串方式（测试证明 rapidfuzz 在当前数据上收益不大）

## 参考来源

- Ombre-Brain `decay_engine.py`：衰减公式 `importance × activation^0.3 × e^(-λ×days) × emotion_weight`
- Ombre-Brain `dehydrator.py`：DIGEST_PROMPT 的数量控制和质量过滤规则
- 本地仓库参考副本：`_ombre-brain-ref/`（可在实现完成后删除）
