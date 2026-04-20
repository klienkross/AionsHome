# Obsidian 日记查看功能设计

**日期：** 2026-04-20  
**状态：** 已审批

---

## 概述

让 bot 能主动读取本地 Obsidian 日记，通过文本命令模式触发，与现有 `[POI_SEARCH:]`、`[MEMORY:]` 等命令风格一致。

---

## 命令规格

| 命令 | 格式 | 说明 |
|------|------|------|
| 读指定日期 | `[OBSIDIAN_READ:2026-04-20]` | 读取对应日期的 `.md` 文件全文 |
| 读最近N天 | `[OBSIDIAN_RECENT:7]` | 读最近 N 篇日记，每篇调用 flash-lite 提取实质内容摘要 |
| 关键词搜索 | `[OBSIDIAN_SEARCH:关键词]` | 全库搜索含该关键词的日记，返回文件名+命中行 |

N 上限：14 天，搜索结果上限：10 篇。

---

## 文件改动

### 新建 `aion-chat/obsidian.py`

三个异步函数：

- `read_diary(date_str: str) -> str` — 读取 `{vault}/{date_str}.md`，文件不存在返回提示
- `read_recent(n: int) -> str` — 列出最近 N 个日记文件，每篇调用 `summarize_diary()` 生成摘要后拼接
- `summarize_diary(date_str: str, content: str) -> str` — 调用 Gemini flash-lite，提取日记实质内容（跳过模板头部），返回100字内摘要
- `search_diary(keyword: str) -> str` — 遍历 vault 内所有 `.md` 文件，返回含关键词的文件名和命中行

vault 路径从 `config.SETTINGS` 读取 `obsidian_vault_path` 字段。

### 修改 `aion-chat/routes/chat.py`

在现有 pattern 区块新增3条正则（约5行）：

```python
OBSIDIAN_READ_PATTERN   = re.compile(r'\[OBSIDIAN_READ:(\d{4}-\d{2}-\d{2})\]')
OBSIDIAN_RECENT_PATTERN = re.compile(r'\[OBSIDIAN_RECENT:(\d+)\]')
OBSIDIAN_SEARCH_PATTERN = re.compile(r'\[OBSIDIAN_SEARCH:([^\]]+)\]')
```

在 streaming 结束后的命令处理区块，仿照 `POI_SEARCH` 的处理方式：
1. 检测命令
2. 调用 `obsidian.py` 对应函数
3. 将结果作为 `system` 消息插入数据库并广播
4. 二次调用 AI 让 bot 结合日记内容回复

### 修改 `aion-chat/data/settings.json`

新增一个字段：

```json
"obsidian_vault_path": "D:/Obsidian/Daily Notes"
```

### System Prompt

在世界书（worldbook）或 system prompt 中告知 bot 三个命令的使用场景：

```
你可以使用以下命令查阅用户的 Obsidian 日记：
- [OBSIDIAN_READ:YYYY-MM-DD] 查看指定日期日记
- [OBSIDIAN_RECENT:N] 查看最近N天日记（最多14天）
- [OBSIDIAN_SEARCH:关键词] 搜索日记中含某关键词的内容
当用户提到日记、某天发生的事、想回顾过去时，主动使用这些命令。
```

---

## 执行流程

```
用户："帮我看看上周的日记"
  → bot 输出 [OBSIDIAN_RECENT:7]
  → chat.py 检测命令
  → obsidian.py 读取最近7篇，每篇调用 flash-lite 摘要（跳过模板）
  → 插入 system 消息："📖 已读取最近7篇日记：\n..."
  → 二次调用 AI，bot 结合内容回复用户
```

---

## 错误处理

| 情况 | 处理 |
|------|------|
| vault 路径未配置 | 返回 "Obsidian 日记路径未配置" |
| 指定日期文件不存在 | 返回 "YYYY-MM-DD 暂无日记" |
| vault 目录不存在 | 返回 "日记目录不存在，请检查路径配置" |
| 关键词无匹配 | 返回 "未找到含'关键词'的日记" |

---

## 约束

- N 最大 14，超出截断为 14
- 搜索结果最多返回 10 篇
- RECENT 模式：每篇调用 flash-lite 摘要，跳过模板头，提取实质内容（100字内）
- OBSIDIAN_READ 单篇：返回全文，由 bot 自行处理（无需摘要）
- flash-lite 摘要失败时降级为截取正文第一个非空标题段落后的内容
- 不写入、不修改任何日记文件，只读
