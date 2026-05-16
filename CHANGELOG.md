## 更新日志

### 2026-05-10 — CLI 图片管线修复 + Connor 跨窗口上下文 + 多场景群聊集成

**背景**：
1. Connor 私聊窗口看不到群聊消息（`build_connor_1v1_prompt` 传了 1v1 的 room_id 给 `fetch_merged_timeline` 的 `room_id` 参数，但该参数是用于指定群聊房间的，导致 0 条群聊消息被合并）
2. 闹铃/监控/哨兵/cam_check 等触发场景只能看到私聊历史，无法感知群聊上下文
3. 通过 Gemini CLI 和 Codex CLI 发送图片全部报错（Gemini CLI: `Separator is not found, and chunk exceed the limit`；Codex CLI: `Input exceeds the maximum length of 1048576 characters`）

**改动内容**：

1. **`ai_providers.py` — `_build_cli_prompt` 图片/音频本地路径传递**
   - 旧方式：完全忽略 messages 中的 `attachments` 字段
   - 中间尝试：将图片转 base64 内嵌到 prompt 文本 → 失败（base64 编码后轻松超过 CLI stdin 的长度限制）
   - 最终方案：解析附件为本地绝对路径，直接写入 prompt 文本，由 CLI 自行读取文件
   - 支持 image/* 和 audio/* 两类附件，结构化附件（voice/video dict）跳过（已有 transcript 文本兜底）

2. **`chatroom.py` — `stream_connor_cli` 支持 messages 列表**
   - 新增 `messages` 参数，可直接传入完整消息列表（保留附件），不再强制转为纯文本
   - 传入 messages 时自动注入 Connor persona 作为 system 消息

3. **`chatroom.py` — `build_connor_1v1_prompt` → `build_connor_1v1_context`**
   - 从返回纯文本 prompt 改为返回 messages 列表，timeline 消息保留 attachments 字段
   - 修复 `room_id` 参数误传问题（不再传 1v1 room_id 给 `fetch_merged_timeline`）

4. **`routes/chatroom.py` — Connor 群聊/私聊管线改造**
   - `_reply_connor`（群聊）：不再手动将 history 转为纯文本（丢失附件），直接传 `connor_history` 给 `stream_connor_cli(messages=...)`
   - `_generate_connor_reply`（私聊）：改用 `build_connor_1v1_context` + `stream_connor_cli(messages=...)`

5. **`schedule.py` — 闹铃/监控触发集成群聊上下文**
   - `_fire_alarm`、`_fire_monitor`：使用 `fetch_merged_timeline` 替代原来只查私聊的逻辑，懒导入避免循环依赖

6. **`camera.py` — 哨兵/cam_check 集成群聊上下文**
   - `_call_core`（哨兵 Core 唤醒）、`perform_cam_check`（主动查看监控）：同样使用 `fetch_merged_timeline`，懒导入避免循环依赖

**不影响的线路**：硅基流动（`build_multimodal_messages`，base64 内嵌 API JSON）和 Gemini 原生 API（`build_gemini_contents`，base64 内嵌 `inline_data`）的图片处理方式不变

**踩坑记录**：见下方坑 10、坑 11、坑 12

### 2026-05-09 — 统一时间线上下文 + 统一记忆总结

**背景**：之前 Aion 私聊只能看到私聊历史，群聊只能看到群聊历史，两个 AI 的记忆总结也各自独立（Connor 私聊和群聊分别总结，群聊记忆还要同步一份到 Aion 主库）。改为统一时间线，让每个 AI 都能同时看到私聊和群聊内容，记忆总结也合并处理。

**改动内容**：

1. **新增 `context_builder.py`** — 统一上下文构建模块：
   - `fetch_merged_timeline(who, limit, *, conv_id, room_id)`：同时查询 `messages` 和 `chatroom_messages` 两张表，按 `created_at` 合并排序，返回统一时间线
   - `render_merged_timeline(merged, who)`：将合并时间线转为 AI 历史格式，私聊/群聊混合时自动插入场景切换标记 `[以下为群聊记录]` / `[以下为私聊记录]`，消息前缀带 `[群聊]` / `[私聊]` 标签
   - `build_ability_block()`、`build_memory_blocks()`、`strip_tool_commands()` 等工具函数从各处抽取统一

2. **`routes/chat.py`** — Aion 私聊上下文统一：
   - `send_message`、`edit_resend`、`regenerate` 三个函数的历史构建改为 `fetch_merged_timeline("aion")` + `render_merged_timeline()`，Aion 在私聊中也能看到群聊内容

3. **`chatroom.py`** — 群聊上下文 + Connor 记忆统一：
   - `build_aion_group_context()` / `build_connor_group_context()`：改用统一时间线，移除旧的跨窗口上下文注入
   - `digest_chatroom()`：合并 Connor 1v1 + 群聊消息统一总结，使用 `connor_unified` 锚点，scope 固定为 `"connor"`，删除"群聊记忆同步写入 Aion 主库"逻辑（两个 AI 各管各的记忆）
   - `_connor_1v1_auto_digest_loop()`：不再查找特定房间，直接调用 `digest_chatroom()` 统一总结
   - `connor_1v1_on_message()`：群聊消息也触发计时器重置

4. **`memory.py`** — Aion 记忆总结统一：
   - `_do_digest()`：在私聊消息基础上追加查询群聊 `chatroom_messages`，标记 `_source`（private/group），混合来源时消息格式带 `[群聊]` / `[私聊]` 标签

5. **`main.py`** — 自动总结空闲检测增强：
   - `_auto_digest_loop()`：同时检查 `messages` 和 `chatroom_messages` 两张表的最后用户消息时间，避免群聊活跃时误触发私聊自动总结

6. **`routes/chatroom.py`** — 触发器扩展：
   - `_save_msg()`：群聊消息也触发 `connor_1v1_on_message()` 重置自动总结计时器

7. **Bug 修复**：
   - 流式输出气泡残留原始指令：`aion_done` / `connor_done` 事件用服务端清洗后的内容替换 streamingText
   - 闹铃/日程创建时缺少系统消息：在 `process_schedule_commands` 之前预检测指令并插入系统提示
   - 音乐点歌后不自动播放：添加 `autoplay: True` 参数

### 2026-05-08 — Gemini CLI 本地调用接入

**背景**：Gemini CLI（`@google/gemini-cli`）支持通过 Google OAuth 免费调用 Gemini 模型，无需 API Key。将其作为第四种 AI 调用方式集成到项目中。

**改动内容**：
1. **`ai_providers.py`**：
   - 新增 `_find_gemini_script()`：自动定位全局安装的 gemini CLI 脚本（npm root -g 方式 + gemini.cmd 位置推导）
   - 新增 `_build_cli_prompt(messages)`：将 messages 列表拼成 `[System Instruction] / [User] / [Assistant]` 格式的完整 prompt
   - 新增 `call_gemini_cli()` 异步生成器：通过 `asyncio.create_subprocess_exec` 启动 CLI 子进程，stdin 传入 prompt（绕过 Windows 命令行 8K 长度限制），流式读取 stdout 并 yield
   - `stream_ai()` 新增 `gemini_cli` provider 路由分支
2. **`config.py`**：`MODELS` 字典新增 `CLI-2.5pro`、`CLI-3.1pro`、`CLI-2.5flash` 三个模型
3. **新增 `cli线部署教程.md`**：面向朋友的 CLI 线路部署指南

**使用方式**：聊天界面右上角切换模型到 `CLI-xxx`，其余（人设、记忆、指令解析、TTS）全部照常工作。不需要额外启动任何服务。

**部署前置**：`npm install -g @google/gemini-cli` + 首次运行 `gemini` 完成 OAuth 认证

### 2026-04-08 — UI 多页面拆分重构

**背景**：原 chat.html 单文件近 4000 行，所有功能（设置/世界书/记忆库/日程/摄像头/监控日志/定位）以模态弹窗形式耦合在聊天页内，维护和扩展困难。

**改动内容**：
1. **新建 7 个独立功能页面**：settings.html、worldbook.html、memory.html、schedule.html、camera.html、monitor-logs.html、location.html，每个页面独立完整（HTML+CSS+JS）
2. **新建共享层**：common.css（CSS 变量/子页面布局/组件样式/闹铃弹窗/toast）+ common.js（api() 封装/WebSocket 连接/闹铃弹窗/系统通知）
3. **chat.html 瘦身**：删除了 7 个模态弹窗的 HTML + 对应 JS 函数（摄像头控制/监控日志/WebSocket override/记忆库管理/日程管理/设置/世界书/定位），保留与聊天深度耦合的功能（语音唤醒/TTS/BLE密语/音乐/系统日志/[CAM_CHECK]）
4. **侧边栏简化**：移除 6 个功能导航按钮，仅保留「系统日志」「密语时刻」「返回主页」
5. **main.py 新增路由**：/settings、/worldbook、/memory、/schedule、/camera、/monitor-logs、/location
6. **home.html 更新**：APPS 注册表新增 camera/logs/location 入口，memory/worldbook/alarm/settings 绑定对应 URL
7. **文件管理器优化**：标题栏加关闭按钮，文件列表区域可滚动

**保留在 chat.html 的功能**：语音唤醒通话、TTS 语音合成、密语时刻(BLE)、音乐点歌、[CAM_CHECK] 主动查看监控、系统日志（session 级）、文件管理器

**子页面共享机制**：每个子页面通过 `<link href="/static/common.css">` + `<script src="/static/common.js">` 引入共享层，调用 `connectCommonWS()` 建立独立 WebSocket 连接（用于接收闹铃弹窗），各页面自行管理 API 调用和渲染逻辑

### 2026-04-08 — 后台消息保障 + 子页面 iframe 浮层（防切页丢消息/TTS 中断）

**背景**：多页面拆分后，从 chat.html 导航到设置/主页/监控日志等页面会销毁聊天页，导致：① 正在等待的 AI 回复丢失（SSE 流中断，后端 generate() 生成器被关闭，DB 保存和 WS 广播永远不执行）；② TTS 语音播放立即停止（Audio 元素和队列被销毁）。手机上尤其明显，发消息后切到其他页面查看就会丢回复。

**改动内容**：

1. **后端：AI 生成解耦为后台任务**（`routes/chat.py` — `send_message` + `regenerate_message`）
   - 原架构：`generate()` 异步生成器内 AI 流式输出 → 后处理（指令检测、音乐搜索、日程解析）→ 存 DB → WS 广播，全在 `yield` 链路中，客户端断开则全部丢失
   - 新架构：拆为 `_bg_generate()` 后台任务 + `generate()` SSE 转发层
     - `_bg_generate()`：`asyncio.create_task()` 启动，AI 流式输出 + 全部后处理 + 存 DB + WS 广播，通过 `asyncio.Queue` 向 SSE 层推送事件，`try/finally` 确保始终运行到结束
     - `generate()`：仅从 Queue 读取并 `yield`，纯薄层转发。客户端断开时生成器正常关闭，后台任务不受影响
   - **效果**：即使客户端断开连接（切页/关闭/网络中断），AI 回复依然会完成生成、存入数据库、通过 WebSocket 广播到所有在线客户端

2. **前端：子页面 iframe 浮层**（`static/chat.html`）
   - 新增全屏 `#subPageOverlay`：包含顶部关闭栏 + `<iframe>` 容器
   - 侧栏「⚙ 设置」「🏠 返回主页」「⬅ 返回」全部改为 `openSubPage(url)` → 在浮层中打开目标页，chat.html 始终存活
   - `closeSubPage()`：关闭浮层 + 重新加载消息列表（补上浮层期间后台生成的新消息）
   - 浏览器返回键 (`popstate`) 自动关闭浮层
   - **效果**：SSE 流式接收、TTS 播放、WS 连接在浮层打开期间全部不中断

3. **home.html iframe 适配**
   - 当 home.html 在 iframe 中加载时，点击「聊天」→ `window.parent.closeSubPage()` 关闭浮层回到 chat.html
   - 点击「密语时刻」→ 关闭浮层 + 调用 `window.parent.openWhisper()`

**涉及文件**：`routes/chat.py`（后端核心）、`static/chat.html`（前端浮层 + 导航改造）、`static/home.html`（iframe 适配）

