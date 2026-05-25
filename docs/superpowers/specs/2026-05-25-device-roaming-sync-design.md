# 多设备漫游同步设计

## 概述

基于现有 `sync_to_cloud.py` 扩展，实现多设备间聊天上下文、记忆库、日程、活动日志的增量同步。核心目标：任何设备坐下来就能接上之前的对话上下文。

## 架构

```
[GitHub: Aions_memory 仓库] ← 单一 source of truth
       │
  ┌────┼────────────────┐
  │    │                 │
[PC/家] [operit/外出]  [新电脑]
  │                      │
  └── WiFi webhook ──────┘
       触发 push/pull
```

## 同步范围

| 数据 | 格式 | 说明 |
|------|------|------|
| 聊天记录 | JSON 增量 | 自上次同步后的新消息，按对话 ID 分文件 |
| 记忆库 | JSON | memories 表条目 + embedding (base64) |
| 日程 | JSON | 活跃的 schedules 条目 |
| 活动日志 | Markdown 摘要 | 最近活动摘要（供 op 参考上下文） |

## 云端仓库结构（Aions_memory）

```
Aions_memory/
├── device_state.json          # 当前活跃设备 + 最后活跃时间戳
├── sync_anchor.json           # 各设备上次同步到的位置（msg created_at）
├── chats/
│   ├── conversations.json     # 对话列表元数据（id, title, model, updated_at）
│   └── {conv_id}.json         # 单个对话的增量消息列表
├── memories/
│   └── memories.json          # 记忆条目数组（含 embedding base64 编码）
├── schedules.json             # 活跃日程快照
└── activity_summary.md        # 最近活动摘要（人类可读，供 op 做上下文参考）
```

## 核心流程

### sync-out（推送到云端）

触发时机：
- WiFi 断开家庭网络 → 自动化 webhook 调用
- 手动 CLI：`python sync_to_cloud.py --push`
- 手动 API：`POST /api/sync/push`

步骤：
1. 读取 `sync_anchor.json` 获取本设备上次同步位置
2. 从 chat.db 导出上次同步后的新消息（按 `created_at > anchor` 过滤）
3. 从 memories 表导出新增记忆
4. 导出当前活跃日程
5. 生成活动摘要（最近几小时的设备使用概况）
6. 更新 `device_state.json`（标记本设备为 idle，记录时间戳）
7. 通过 GitHub REST API 提交所有变更

### sync-back（从云端拉取）

触发时机：
- WiFi 连上家庭网络 → 自动化 webhook 调用
- 手动 CLI：`python sync_to_cloud.py --pull`
- 手动 API：`POST /api/sync/pull`
- 新电脑首次 bootstrap

步骤：
1. 通过 GitHub API 拉取所有文件
2. 读取 `sync_anchor.json`，确定本设备需要导入哪些增量
3. 将新消息导入 chat.db（跳过已存在的 msg id）
4. 将新记忆导入 memories 表（跳过已存在的 id，embedding 从 base64 解码为 blob）
5. 合并日程（新增的导入，已删除的标记）
6. 更新本地 `sync_anchor.json`
7. 更新 `device_state.json`（标记本设备为 active）

### 设备注册（自动）

新设备首次执行 sync 时自动注册，无需手动编辑设备列表：

1. 检查本地 `settings.json` 是否有 `device_id`
2. 若无 → 自动生成（`{hostname}-{随机4位}`，如 `DESKTOP-A3F2`）
3. 写入本地 `settings.json`
4. 推送到云端 `device_state.json` 的 `devices` 中注册
5. `device_name` 可选手动修改（不改则用自动生成的 ID）

唯一需要手动配置的是 `github_sync_token`（安全考虑必须手动填写）。

### 设备状态管理

`device_state.json` 结构：
```json
{
  "active_device": "pc-home",
  "last_active_at": "2026-05-25T14:30:00+08:00",
  "devices": {
    "pc-home": {
      "name": "家里电脑",
      "last_seen": "2026-05-25T14:30:00+08:00",
      "status": "active"
    },
    "phone-operit": {
      "name": "手机/operit",
      "last_seen": "2026-05-25T14:25:00+08:00",
      "status": "idle"
    }
  }
}
```

### 增量锚点

`sync_anchor.json` 结构：
```json
{
  "pc-home": {
    "last_msg_at": 1748150000.0,
    "last_memory_at": 1748149000.0,
    "last_sync_at": "2026-05-25T14:30:00+08:00"
  },
  "phone-operit": {
    "last_msg_at": 1748148000.0,
    "last_memory_at": 1748147000.0,
    "last_sync_at": "2026-05-25T12:00:00+08:00"
  }
}
```

## 触发方式

| 触发 | 场景 | 实现 |
|------|------|------|
| WiFi webhook（自动） | 日常出门/回家 | 现有自动化链路发 HTTP 请求到 `/api/sync/push` 或 `/api/sync/pull` |
| CLI（手动） | 新电脑 bootstrap、调试 | `python sync_to_cloud.py --push / --pull` |
| API 端点（手动） | 从 chat 页面触发 | `POST /api/sync/push`、`POST /api/sync/pull` |
| Operit 指令（手动） | 在外面确认拿到最新上下文 | 告诉 op "同步一下"，op 调 GitHub API 拉取 |

## 冲突处理

- **设计假设**：同一时间只有一台设备活跃（人只在一个地方）
- **策略**：last-write-wins，按 `created_at` 时间戳排序
- **消息去重**：用 msg id 做幂等，相同 id 不重复插入
- **记忆去重**：用 memory id 做幂等
- **极端情况**：两台设备同时产生消息 → 按时间戳合并到同一时间线，不丢数据

## GitHub API 使用

认证：Personal Access Token（fine-grained，仅限 Aions_memory 仓库读写权限）

关键操作：
- 读文件：`GET /repos/{owner}/{repo}/contents/{path}`
- 写文件：`PUT /repos/{owner}/{repo}/contents/{path}`（含 sha 做乐观锁）
- 批量提交：使用 Git Trees API 一次性提交多个文件变更

Token 存储：`data/settings.json` 中新增 `github_sync_token` 字段

## 新电脑开箱即用流程

1. 克隆 AionsHome 项目
2. 配置 `data/settings.json`（填入 GitHub token + 设备名）
3. 执行 `python sync_to_cloud.py --pull`
4. 自动拉取全部最新状态：对话、记忆、日程
5. 启动 aion-chat，上下文完整可用

## 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `aion-chat/sync_to_cloud.py` | 重写 | 从全量 shutil 改为 GitHub API 增量同步 |
| `aion-chat/config.py` | 修改 | 新增 sync 相关配置读取（token、设备名、仓库信息） |
| `aion-chat/routes/sync.py` | 新建 | `/api/sync/push` 和 `/api/sync/pull` 端点 |
| `aion-chat/main.py` | 修改 | 注册 sync 路由 |
| `aion-chat/data/settings.json` | 修改 | 新增 `github_sync_token`、`device_id`、`sync_repo` 字段 |

## 不在此次范围内

- Operit 端的实现细节（op 怎么读 GitHub 由 op 自己的能力决定）
- 端到端加密（后续可加，当前仓库设为 private 即可）
- 实时同步（不需要，切换时同步一次就够）
- chat.db 二进制同步（用 JSON 导出替代）
