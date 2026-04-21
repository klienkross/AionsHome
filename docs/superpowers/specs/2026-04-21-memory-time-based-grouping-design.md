# 基于时间戳的记忆切片

**日期：** 2026-04-21
**目标文件：** [aion-chat/memory.py](../../../aion-chat/memory.py)
**替换函数：** `_split_into_groups`（当前位于 line 332）

## 背景与动机

现有 `_split_into_groups` 按固定每 20 条切分新消息，完全忽略 `messages.created_at` 时间戳。结果：

- 同一晚连续对话可能被拦腰切成两段，破坏摘要语义；
- 跨多天的零散消息可能被硬塞进同一组，让模型在不连贯的内容上做总结；
- `manual_digest` 中既然已经为每组打印 `[对话时间范围: ...]`，说明"按时间聚合"才是设计本意。

本次改动只替换切分函数，不改 `manual_digest` 主流程、不改 prompt、不改入库逻辑。

## 切分策略

**核心思想：** 先按时间间隙做"自然 session"切分，再对极端长度做归一化。

### 新签名

```python
def _split_into_groups(
    msgs: list,
    gap_seconds: int = 3600,   # 1 小时：相邻消息间隔超过此值即视为新 session
    target_min: int = 10,      # 段长度下限（短段尝试合并）
    target_max: int = 20,      # 段长度上限（长段需细分）
) -> list[list]
```

调用点 [memory.py:406](../../../aion-chat/memory.py#L406) 不变，参数走默认值。

### 算法三步

**Step 1：粗切（时间间隙）**
按 `created_at` 升序遍历 `msgs`（`manual_digest` 查询时已 ORDER BY ASC，假定有序）。维护当前段，遇到 `msgs[i].created_at - msgs[i-1].created_at > gap_seconds` 就把当前段封口，开始新段。

**Step 2：短段合并（A2，单次扫描）**
对所有 `len(seg) < target_min` 的段，决定并入哪一侧邻居：

- 计算 `gap_left = seg[0].created_at - prev_seg[-1].created_at`
- 计算 `gap_right = next_seg[0].created_at - seg[-1].created_at`
- 选择 gap 更小的一侧合并；相等时优先并入前一段。
- 边界段（首段无 prev、末段无 next）只能并入唯一邻居。
- **单次贪心**：从前往后扫一遍，合并完成即接受结果。即使合并后段仍 <10 也不再继续合并，避免连锁合并把整批消息塞回一段。

**Step 3：长段细分（B2，递归 / 兜底 B1）**
对所有 `len(seg) > target_max` 的段：

1. 在段内计算所有相邻消息的时间差，找出最大的 gap；
2. 如果该最大 gap 显著（启发式：> 段内 gap 中位数 × 3，且 > 60 秒），在该位置切一刀，对两侧子段分别递归；
3. 否则段内时间分布过于均匀，**降级为按 `target_max` 硬切**（沿用旧 B1 逻辑：连续切 20 条一组，余数 <5 并入末组）。
4. 递归终止条件：所有子段 ≤ `target_max`。

### 退化与边界

| 输入 | 行为 |
|---|---|
| `msgs == []` | 返回 `[]` |
| `len(msgs) == 1` | 返回 `[msgs]` |
| `len(msgs) <= target_max` | 跳过所有步骤，直接返回 `[msgs]`（快路径） |
| 所有消息时间戳相同 | Step 1 单段 → Step 3 走 B1 兜底硬切 |
| Step 2 后某段仍 <10 | 接受现状，不再合并 |

## 测试

新增 `aion-chat/test_memory_grouping.py`，覆盖以下用例（pytest 风格，纯函数测试，无需数据库）：

1. **空 / 单条 / 短输入**：分别返回 `[]`、`[msgs]`、`[msgs]`。
2. **单 session 长聊**：50 条消息全部 30 秒间隔 → Step 1 单段，Step 3 切成 ≥3 个 ≤20 的子段。
3. **多日稀疏对话**：3 天，每天 12 条，日间隔 >1 小时 → 期望保留为 3 段（每段 12，>=target_min，Step 2 不触发）。
   - 配套用例：3 天每天 5 条，单次贪心合并后会塌成 1~2 段，断言段数 ∈ {1, 2} 且总条数=15（验证不丢消息，不强求段结构）。
4. **零散短段需合并（A2）**：构造 [12 条 / 间隔 / 3 条 / 间隔 / 15 条]，且让中间 3 条距离左侧 gap < 距离右侧 gap → 期望并入左侧，得到 [15, 15]。
5. **长段细分用次级 gap（B2）**：30 条消息，前 15 条密集、后 15 条密集，中间一个 50 分钟 gap（< 1h 阈值，所以 Step 1 不切）→ Step 3 在该 gap 切成 [15, 15]。
6. **长段无明显 gap，B1 兜底**：50 条均匀间隔 1 分钟 → Step 3 找不到显著 gap，按 20 硬切。
7. **边界短段**：首段只有 4 条 → 只能并入下一段。

## 不在范围内

- 不改 `manual_digest` 的 prompt、importance/keywords 提取逻辑、embedding 入库；
- 不改 digest anchor、UI 展示、broadcast 行为；
- 不引入新的依赖，不调外部模型；
- 不改 worldbook、不改其他切分相关函数（如有）。

## 验收

- `_split_into_groups` 行为符合上述测试用例；
- `manual_digest` 端到端跑一次（用现有数据库）能成功产出记忆，无异常；
- 现有功能行为对长度 ≤20 的输入与旧版完全一致（快路径保护）。
