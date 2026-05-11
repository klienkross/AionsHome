# 本地改动摘要

记录本 fork 相对于 upstream (death34018-hue/AionsHome) 的主要改动，便于合并上游时快速定位冲突归属。

---

## 核心模块

### ai_providers.py
- 自定义 OpenAI 兼容端点 (custom provider) 支持
- 图片转文本开关 + 无限上下文开关
- DEFAULT_MODEL 可配置化
- DashScope 哨兵调用路径

### config.py
- `get_key()` 新增 mimo / custom provider 分支
- MODELS 字典新增 deepseek 自定义端点
- skip-worktree 保护本地密钥不提交

### main.py
- sensor webhook channel 注册 + event loop 初始化
- ntfy.sh 桥接启动
- HTTP 缓存头优化
- 启动时打印本机 IP
- reading (朗读) 模块注册

### routes/chat.py
- Memory V2 系统集成 (recall、主动检索、前端 API)
- 提示词拼接顺序重构 (时间+记忆末尾，最近对话置底)
- Obsidian 日记工具
- 背景思考 [THINK:] tag 解析 + 异步执行
- 背景思考结果注入上下文
- THINK_SCHEDULE 指令
- 重连后补偿检查错过的通知

### ws.py
- 朗读 TTS WebSocket 流 (ReadingSession)
- 心跳机制 (_last_pong, _heartbeat_task)

### database.py
- Memory V2 原子卡片表 (memory_cards, card_links, card_aggregates)
- background_thoughts 表
- schedules.repeat 字段
- 情绪标注字段 (valence, arousal)

---

## 记忆系统 (Memory V2)

### memory.py
- 时间戳聚合切片
- 语义判官 (flash-lite) 长段切分
- 时间衰减 + 近期补充按重要度排序
- DashScope 向量模型切换
- 情绪标注 (Russell 环形模型)
- 云端同步 (sync_to_cloud)

### memory_cards.py — 全新文件
- 原子卡片 CRUD

### digest_v2.py — 全新文件
- V2 Digest 引擎，分层关键词提示词，并发化

### active_recall.py — 全新文件
- 主动记忆检索

---

## 传感器 & 位置

### sensor.py — 全新文件
- 事件接收与缓冲区
- 窗口到期分析 + Sentinel 集成
- Core 唤醒 + 地理围栏位置更新
- 事件去抖 (60s)
- activity_log 写入

### location.py
- 围栏 state 标签支持
- 关闭状态仍输出围栏数据
- GPS 心跳不再覆盖围栏接管的位置状态
- DashScope 切换

### camera.py
- DashScope 哨兵切换 (替换原 siliconflow)

---

## TTS & 朗读

### tts.py
- MiMo-V2.5 TTS 引擎 (替换硅基流动)
- 【】语气提示提取作为风格指令

### reading.py — 全新文件
- ReadingSession + SSE 朗读 API

### book.py
- PDF 导入支持

---

## Webhook & 通知

### routes/webhooks.py — 全新文件
- MacroDroid webhook 接收端点
- sensor webhook channel
- ntfy.sh 桥接路由

### webhook_ai.py — 全新文件
- AI 消息生成管道 (夜间手机使用提醒等)

### ntfy_bridge.py — 全新文件
- ntfy.sh 公网中转桥接

---

## 定时调度

### schedule.py
- Monitor 无摄像头时降级到传感器上下文
- 定时思考调度
- repeat 重复日程支持

---

## 哨兵 & 向量

### sentinel.py — 全新文件
- 统一的哨兵调用封装 (DashScope)
- 图片转文本支持

---

## 其他

### obsidian.py — 全新文件: 日记读取/搜索/摘要
### ghost_forest.py: DashScope 切换
### gift.py: 送礼流程改为后台异步
### sync_to_cloud.py — 全新文件: 记忆/聊天记录云端同步
### routes/settings.py: MiMo TTS 设置项, DEFAULT_MODEL 配置
### routes/memories.py: V2 卡片 API
### routes/book.py: 读书笔记记忆召回

---

## 前端

### static/chat.html: 朗读入口 UI
### static/reading.html: 朗读播放页面
### static/memory.html: V2 卡片管理界面
### static/settings.html: DEFAULT_MODEL 配置 UI
### static/theater.html: 朗读相关调整
### static/common.js: 朗读相关工具函数

---

## Android App

### AionPushService.java: 重连后补偿检查通知
### LauncherActivity.java: 启动页 IP 可编辑
### WebViewActivity.java: IP 地址传递调整

---

*最后更新: 2026-05-11*
