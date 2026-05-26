# 记忆召回实验方法手册

## 背景

### 时间线

| 日期 | 事件 |
|--|--|
| 5/16 | `bd9f1cc` — reranker 接入生产 recall_memories（embedding top-25 + kw top-20 并集 → reranker 精排） |
| 5/18 | `112a6d0` — recall_eval.py 创建，粗筛实验：embedding@5=98%, 并集@5=99.5% |
| 5/26 | 正式评估集基线（60 对），reranker 保留在生产 recall_memories 中 |

### 粗筛实验 vs 正式评估（为什么 98% 不能跟 60% 对比）

5/18 的粗筛实验方法：LLM 用自己的卡片内容生成查询 → 测该卡片是否在 top-K 结果里。ground truth 存在**同源偏置**——LLM 生成查询时自然会把卡片内容写进查询，embedding 轻松找回。这测的是"卡片→自身的向量一致性"，不是真实检索难度。

正式评估集（`eval_dataset.json`）消除了这个偏置：每条 query 独立标注多个 expected_card_ids，不是简单的"找自己"。

### 5/26 基线数据

2026-05-26 评估集基线（60 对，1844 张卡片）：

|  | 纯关键词（离线） | 关键词+向量 |
|--|--|--|
| Hit Rate @5 | 60.0% | 51.7% |
| Recall @5 | 0.206 | 0.178 |
| MRR | 0.476 | 0.319 |

向量分量加入后效果下降——embedding 区分度不足，加的是噪声不是信号。

按 type 拆分后发现差异显著：

| type | Hit Rate @5（离线） | 说明 |
|--|--|--|
| plan | 100% | 关键词就够 |
| event | 100% | 关键词就够 |
| emotion | 72.7% | 中等 |
| fact | 66.7% | 中等 |
| aggregate | 50.0% | 关键词太泛 |
| preference | 33.3% | 关键词脆弱 |
| promise | 33.3% | 关键词脆弱 |

preference/promise 类型的关键词碰撞率极低，是召回短板。reranker 能读懂语义补上关键词漏掉的相关内容（见小红书文章中的交叉实验）。

## 实验 1：reranker 对不同 type 的召回增益

**目标**：验证 reranker 是否能拯救 preference/promise 的召回，同时不拖累 plan/event。

**方法**：
1. 用现有评估集，跑 `recall_eval.py` 获得纯关键词基线（已有）
2. 在 `recall_eval.py` 中加入 reranker 精排路径：
   - 关键词+importance 粗筛 top-20
   - reranker 精排取 top-5
3. 对比两种管道在每种 type 上的 precision/recall/hit_rate

**预期**：
- plan/event：变化不大（关键词已经够用）
- preference/promise：显著提升（reranker 补上同义表达）
- emotion：可能提升也可能不变（spec 提到 reranker 对情绪链条不敏感）

**执行**：
```powershell
# 基线（已有）
python aion-chat/tools/recall_eval.py --verbose

# 加 reranker（需要先改 recall_eval.py 加 --reranker 选项）
python aion-chat/tools/recall_eval.py --reranker --verbose
```

**改动点**：在 `recall_eval.py` 的 `score_cards` 之后加一步：取 top-20 候选，调 `sentinel.fetch_rerank(query, docs)` 重排，输出 top-5。用 `--reranker` flag 控制开关。

**注意**：
- `fetch_rerank` 是 async 函数，recall_eval.py 当前是同步的。参照已有的 `_embed_queries_async` 模式写 `_rerank_async` wrapper。
- 基线公式澄清：离线模式下 query_vec=None 导致 vec_sim=0，实际基线是 `kw×0.5 + imp×0.2`（不是"纯关键词"）。
- 候选数 top-20 的理由：与生产代码一致（kw top-20），60 条样本 × 20 候选 = 1200 次 reranker 调用，SiliconFlow 计费可接受。可额外测 top-30 看粗筛池大小的影响。
- 同时测 @5 和 @10，reranker 在更大 K 下增益可能更明显。

### 实验 1 结果（2026-05-26）

整体对比（粗筛 top-20）：

|  | 基线 | +reranker | 变化 |
|--|--|--|--|
| Hit Rate @5 | 60.0% | 63.3% | +3.3% |
| Recall @5 | 0.206 | 0.267 | +29.6% |
| Precision @5 | 0.123 | 0.160 | +30.1% |
| MRR | 0.476 | 0.599 | +25.8% |
| Median Rank | 3 | 1 | 大幅改善 |
| 延迟 | 17ms/sample | 1084ms/sample | +62x |

按 type 拆分：

| type | 基线 Hit Rate @5 | +reranker | 判断 |
|--|--|--|--|
| fact | 66.7% | **100%** | 显著增益 |
| promise | 33.3% | **66.7%** | 显著增益 |
| event | 100% | 100% | 不变 |
| plan | 100% | 100% | 不变 |
| emotion | 72.7% | 72.7% | 不变 |
| preference | 33.3% | 33.3% | **无效**——rank 35-89，粗筛 top-20 进不了池子 |
| aggregate | 50.0% | 37.5% | **有害**——reranker 把不相关内容排上来挤掉关键词命中的 |

**结论**：
1. reranker 应保留在生产 recall_memories 中，对 fact/promise 有真实增益
2. preference 需要别的方案——问题在粗筛阶段就漏了，reranker 无从精排
3. aggregate 可能需要跳过 reranker 或调整策略
4. 之前"召回侧无用"的结论被推翻，原因是旧实验有同源偏置

**后续**：
- aggregate 需要单独分析为什么 reranker 反而有害
- preference 需要从关键词质量入手（实验 3）

### 实验 4 结果（2026-05-26）

| 配置 | Hit Rate @5 | Recall @5 | Precision @5 | MRR |
|--|--|--|--|--|
| 基线（无 reranker） | 60.0% | 0.206 | 0.123 | 0.476 |
| +reranker top-20 | **63.3%** | **0.267** | **0.160** | **0.599** |
| +reranker top-30 | 63.3% | 0.261 | 0.157 | 0.593 |
| no-vec + reranker top-20 | 63.3% | 0.267 | 0.160 | 0.599 |

**结论**：
1. 放大粗筛池到 top-30 没有帮助——preference 的 rank 是 35-89，top-30 还是捞不到
2. 去掉向量跟保留向量结果完全一样——向量在此场景下是纯噪声，可以从公式中去掉
3. **reranker top-20 是当前最优配置**，生产公式可简化为 `kw×0.7 + imp×0.3 → reranker 精排`
4. preference 的瓶颈不在粗筛池大小，在关键词根本碰不上——需要从关键词质量（实验 3）或 query 侧改进入手

## 实验 2：关键词质量对召回的影响

**目标**：验证 spec 中"关键词太具体导致跨卡片不碰撞"的诊断。

**方法**：
1. 从评估集中取所有 MISS 样本
2. 人工检查：query_keywords 和目标卡片的 keywords 之间有没有重叠
3. 统计 MISS 样本中"语义相关但关键词不碰撞"的比例

**预期**：大部分 MISS 是关键词碰不上，不是卡片不存在。

**执行**：
```powershell
# 输出详细报告，人工检查 MISS 样本
python aion-chat/tools/recall_eval.py --verbose --output recall_report.json
# 然后在 recall_report.json 中筛 hit=false 的样本看关键词
```

**半自动化**：在 recall_eval.py 的 per-sample 报告中，对 MISS 样本额外输出 query_keywords 与目标卡片 keywords 的交集，一眼看出哪些是"语义相关但关键词不碰撞"。

## 实验 3：分层关键词 prompt 改进

**目标**：测试改进后的关键词（领域词+实体词两层结构）对召回的影响。

**方法**：
1. 选 20 张 preference/promise 卡片
2. 用改进后的 prompt 重新生成关键词（领域词在前、实体词在后、泛指词黑名单）
3. 手动替换到评估集中，重跑 recall_eval
4. 对比改进前后的 hit rate

**注意**：这个实验改的是卡片侧的关键词，不是 query 侧。需要重跑 digest 或手动更新 DB。影响面大，不要直接改 DB。

**更轻量的做法**：在 `recall_eval.py` 加 `--keyword-override keywords_override.json` 参数，加载一个 JSON 文件覆盖特定卡片的关键词，评估脚本内做 A/B 对比，不动 DB。

**遗漏**：query 侧关键词质量同样由 LLM 生成，如果 query 侧也差，只改卡片侧效果有限。建议补对称实验。

## 实验 4：混合管道（关键词 + reranker，去掉向量）

**目标**：既然向量是负贡献，测试完全去掉向量分量的效果。

**方法**：
1. 评分公式从 `kw×0.5 + vec×0.3 + imp×0.2` 改为 `kw×0.7 + imp×0.3`（权重待标定，先用这个起步）
2. 粗筛后 reranker 精排
3. 全量跑评估集
4. 测试多个粗筛策略：top-20 / top-30 / top-50，看粗筛池大小对最终结果的影响

**风险**：如果关键词碰不上（preference/promise 的核心问题），粗筛 top-20 里可能根本不包含正确答案，reranker 无从精排。放大粗筛池（top-30+）可缓解，但成本线性增长。

**执行**：
```powershell
# 在 recall_eval.py 加 --no-vec 选项
python aion-chat/tools/recall_eval.py --no-vec --reranker --verbose
```

## 实验优先级

1. **实验 1**（reranker 按 type 增益）— 最快出结论，只需改 recall_eval.py
2. **实验 2**（关键词碰撞诊断）— 手动检查，帮助理解根因
3. **实验 4**（去向量混合管道）— 如果实验 1 验证 reranker 有用，这个是自然的下一步
4. **实验 3**（关键词质量）— 改动最大，最后做

## 延迟与成本

参考数据（来自小红书文章实测）：
- embedding 单次调用：~500-600ms（HTTP 到阿里云）
- reranker 精排 20 条：~500-730ms
- 合计单次召回延迟：~1.0-1.2s（含 reranker），~680ms（不含）

每个实验如果涉及 reranker，需记录实际延迟。最终方案上线前要确认延迟在可接受范围内（用户感知 1-2s 可接受）。

## 工具清单

| 文件 | 用途 |
|--|--|
| `aion-chat/tools/recall_eval.py` | 评估脚本，需扩展 --reranker / --no-vec |
| `aion-chat/tools/eval_dataset.json` | 评估集（60 对） |
| `aion-chat/tools/gen_eval_dataset.py` | 评估集生成器 |
| `aion-chat/sentinel.py:fetch_rerank` | reranker 接口（已保留） |
| `aion-chat/memory.py` | 召回主逻辑（生产代码，实验验证后再改） |
