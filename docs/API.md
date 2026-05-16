## API 一览

### 对话/消息
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/conversations` | GET | 对话列表 |
| `/api/conversations` | POST | 创建对话 |
| `/api/conversations/{conv_id}` | PUT | 更新对话（标题/模型） |
| `/api/conversations/{conv_id}` | DELETE | 删除对话 |
| `/api/conversations/{conv_id}/messages` | GET | 消息列表（支持 `?limit=50&before=时间戳` 分页） |
| `/api/conversations/{conv_id}/send` | POST | 发送消息（SSE 流式） |
| `/api/conversations/{conv_id}/regenerate` | POST | 重新生成 AI 回复（SSE 流式） |
| `/api/messages/{msg_id}` | PUT | 编辑消息 |
| `/api/messages/{msg_id}` | DELETE | 删除消息 |
| `/api/cam-check-trigger` | POST | Core 主动查看监控触发（前端延迟 5 秒后调用） |

### 摄像头
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/cam/status` | GET | 摄像头和监控状态 |
| `/api/cam/cameras` | GET | 可用摄像头列表 |
| `/api/cam/open` | POST | 打开摄像头 |
| `/api/cam/close` | POST | 关闭摄像头 |
| `/api/cam/monitor/start` | POST | 开始定时监控 |
| `/api/cam/monitor/stop` | POST | 停止定时监控 |
| `/api/cam/config` | GET/POST | 读取/保存摄像头配置 |
| `/api/cam/frame` | GET | 获取当前帧（JPEG） |
| `/api/cam/screenshot` | POST | 手动截图 |
| `/api/cam/logs` | GET | 日志日期列表 |
| `/api/cam/logs/{date}` | GET | 指定日期的日志条目 |

### 设置/世界书/状态
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/settings` | GET/POST | 读取/保存设置（API Key 等） |
| `/api/worldbook` | GET/POST | 读取/保存世界书 |
| `/api/chat_status` | GET | 获取当前聊天状态摘要 |
| `/api/models` | GET | 可用模型列表 |

### 记忆库
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/memories` | GET | 获取所有记忆（按时间倒序） |
| `/api/memories` | POST | 手动添加记忆（自动向量化） |
| `/api/memories/{id}` | PUT | 编辑记忆（重新向量化，支持 unresolved 字段） |
| `/api/memories/{id}` | DELETE | 删除记忆 |
| `/api/memories/{id}/unresolved` | PATCH | 切换记忆的 unresolved 状态 |

### TTS 语音合成
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/tts` | POST | TTS 合成代理，接收 `{text, voice}`，返回 mp3 音频流 |
| `/api/tts/voices` | GET | 获取硅基流动账号下的可用音色列表 |
| `/api/tts/audio/{name}` | GET/HEAD | 获取 TTS 缓存音频分片（`{msg_id}_s{seq}.mp3`），HEAD 用于前端探测分片是否存在 |

### 文件管理
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/files` | GET | 导出文件列表 |
| `/api/files/{filename}` | DELETE | 删除导出文件 |
| `/api/upload` | POST | 上传图片/视频 |

### 音乐
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/music/search` | GET | 搜索歌曲（`?keyword=xxx&limit=5`） |
| `/api/music/detail/{song_id}` | GET | 获取歌曲详情 |
| `/api/music/play` | POST | 获取播放信息（`{song_id}` → 返回歌曲信息 + audio_url + web_url） |
| `/api/music/stream/{song_id}` | GET | 服务端代理推流（后端实时获取 CDN URL 并转发音频流，解决防盗链） |

### 语音唤醒/通话
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/voice/status` | GET | 语音状态（开关/通话中/AI说话中） |
| `/api/voice/toggle` | POST | 开关语音监听（`{enabled, wake_word}`） |
| `/api/voice/ai-speaking` | POST | 前端通知 TTS 播放状态（`{speaking}`） |
| `/api/voice/cam-check-start` | POST | 通知语音模块 CAM_CHECK 开始，保持 AI 说话状态 |
| `/api/voice/remote-asr` | POST | 手机端远程 ASR：接收 WAV 音频文件，转发硅基流动 SenseVoiceSmall 识别 |

### 活动日志
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/activity/report` | POST | 设备活动上报（`{device, app, title?, timestamp?}`），自动名称解析+过滤+JSONL存储+WS广播 |
| `/api/activity/status` | GET | PC 采集线程状态诊断（是否运行、线程状态、上次窗口标题等） |
| `/api/activity/dates` | GET | 返回所有有日志的日期列表 |
| `/api/activity/logs/{date}` | GET | 返回指定日期的活动日志（自动名称解析） |
| `/api/activity/recent` | GET | 返回最近 N 小时的活动日志（默认 8 小时，`?hours=N`） |
| `/api/activity/clear` | POST | 清除所有活动日志文件 |

### 日程/闹铃
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/schedules` | GET | 日程列表（可选 `?status=active`） |
| `/api/schedules` | POST | 手动添加日程（`{type, trigger_at, content}`） |
| `/api/schedules/{id}` | DELETE | 删除日程 |

### 奥罗斯幽林 TRPG
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/ghost-forest/personas` | GET | 列出所有 DM/玩家人设 |
| `/api/ghost-forest/personas` | POST | 创建/更新人设 |
| `/api/ghost-forest/personas/{pid}` | DELETE | 删除人设 |
| `/api/ghost-forest/sessions` | GET | 列出所有游戏会话（摘要） |
| `/api/ghost-forest/sessions` | POST | 创建新游戏会话 |
| `/api/ghost-forest/sessions/{sid}` | GET | 获取完整会话数据 |
| `/api/ghost-forest/sessions/{sid}` | PATCH | 更新会话模型 |
| `/api/ghost-forest/sessions/{sid}` | DELETE | 删除会话 |
| `/api/ghost-forest/sessions/{sid}/generate-outline` | POST | AI 生成剧情大纲（SSE） |
| `/api/ghost-forest/sessions/{sid}/start` | POST | 提交属性分配，开始游戏 |
| `/api/ghost-forest/sessions/{sid}/narrate` | POST | AI 生成当前回合叙述（SSE） |
| `/api/ghost-forest/sessions/{sid}/choose` | POST | 提交选择 + 骰子结果（SSE） |
| `/api/ghost-forest/sessions/{sid}/pause` | POST | 暂停游戏 |
| `/api/ghost-forest/sessions/{sid}/resume` | POST | 恢复游戏 |
| `/api/ghost-forest/sessions/{sid}/finale` | POST | 生成大结局（SSE） |
| `/api/ghost-forest/sessions/{sid}/summary` | POST | 生成冒险总结 |

### 定位/高德地图
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/location/heartbeat` | POST | GPS 心跳上报（`{lng, lat, accuracy}`，可选 `force=true` 强制刷新 API） |
| `/api/location/status` | GET | 当前定位状态（坐标、地址、天气、状态、距离） |
| `/api/location/poi-search` | POST | 手动触发 POI 搜索（`{categories}` 逗号分隔类型） |
| `/api/location/pois` | GET | 获取缓存的 POI 列表 |
| `/api/location/config` | GET | 读取定位配置（含 `active` 字段供 Android 判断是否采集） |
| `/api/location/config` | POST | 保存定位配置（高德Key/开关/安静时段/阈值等） |
| `/api/location/set-home` | POST | 设置家位置（`{lng, lat}` GCJ-02 坐标） |

### SSE 事件类型（send / regenerate）
| type | 说明 |
|------|------|
| `start` | 流开始，含 AI 消息 id |
| `chunk` | 流式文本块 |
| `cam_check` | Core 触发 [CAM_CHECK]，前端播放提示音+延迟触发 |
| `cam_offline` | 摄像头未开启，前端显示提示 |
| `music` | 音乐卡片数据：主推荐歌曲 + 候选列表 |
| `poi_search` | POI 搜索触发：含 msg_id + categories，前端显示蓝色搜索指示器 |
| `toy_command` | 玩具控制指令：含 commands 数组 + msg_id |
| `image_gen_start` | AI 生图开始：含 msg_id + prompt + is_selfie，前端显示橙色生图指示器 |
| `debug` | Debug 数据：模型名、token 用量、召回记忆、完整 prompt |
| `done` | 流结束 |

### WebSocket 事件类型
| type | 说明 |
|------|------|
| `conv_created/updated/deleted` | 对话变动同步 |
| `msg_created/updated/deleted` | 消息变动同步 |
| `monitor_log` | 新监控日志推送 |
| `chat_status` | 聊天状态摘要更新 |
| `memory_added` | 新记忆添加 |
| `voice_state` | 语音状态广播（开关/唤醒/聊天中/AI思考/挂断等） |
| `cam_check` | [CAM_CHECK] 触发通知（SSE + WS 双通道） |
| `music` | 音乐卡片数据广播（SSE + WS 双通道） |
| `debug` | Debug 数据广播（SSE + WS 双通道，语音发送时也能收到） |
| `monitor_alert` | 定时监控触发，前端播放提示音，手机端弹高优先级通知 |
| `schedule_alarm` | 闹铃到期触发，前端弹出确认弹窗，手机端弹高优先级通知 |
| `schedule_changed` | 日程列表变动，前端刷新面板 |
| `toy_command` | 玩具控制指令广播（含 commands 数组，SSE + WS 双通道） |
| `location_update` | 定位状态更新广播（地址、天气、状态变更等） |
| `poi_search` | POI 搜索触发广播（SSE + WS 双通道，前端显示搜索指示器） |
| `activity_log` | 新设备活动日志推送（含 device/app/title/time，前端实时追加） |
| `tts_chunk` | TTS 音频分片推送（含 msg_id/seq/url），前端收到即加入播放队列 |
| `tts_done` | TTS 合成完毕通知（含 msg_id），前端标记该消息队列已结束，播完最后一片后清理 |
| `tts_state` | 客户端→服务端：TTS 开关/音色同步（`{enabled, voice}`），服务端据此判断是否需要合成 |
| `image_gen_start` | AI 生图开始广播（SSE + WS 双通道） |
| `image_gen_done` | AI 生图完成广播（含 conv_id），前端移除指示器 |
| `image_gen_failed` | AI 生图失败广播（含 conv_id），前端移除指示器 |

### 消息角色说明
| 角色 | 说明 | 是否显示在聊天 |
|------|------|---------------|
| `user` | 用户消息 | ✅ |
| `assistant` | AI 回复（含 Core 唤醒/主动查看监控的回复） | ✅ |
| `cam_user` | Sentinel 截图查询（内部） | ❌ 隐藏 |
| `cam_log` | Sentinel 分析结果（内部） | ❌ 隐藏 |
| `cam_trigger` | Core 唤醒时的系统提示（内部） | ❌ 隐藏 |

## Prompt 注入顺序
```
1. [系统设定 - AI人设] + assistant 确认                                        ← 缓存命中
2. [系统设定 - 用户信息] + assistant 确认                                      ← 缓存命中
3. [系统能力] 合并能力提示 + 日程列表 + assistant 确认（不含时间）    ← 缓存命中
   - [MUSIC:歌曲名 歌手名]  — 点歌（始终可用）
   - [CAM_CHECK]            — 主动查看监控（仅摄像头开启时）
   - [POI_SEARCH:类型名]    — 搜索附近 POI（仅外出状态 + 定位开启时）
   - [ALARM:datetime|内容]  — 设置闹铃（始终可用）
   - [REMINDER:date|内容]  — 设置日程提醒（始终可用）
   - [SCHEDULE_DEL:id]      — 删除日程（始终可用）
   - [TOY:1]~[TOY:9]        — 控制玩具预设档位（仅密语模式开启时）
   - [TOY:STOP]             — 停止玩具（仅密语模式开启时）
   - [SELFIE:prompt]        — AI 自拍生图（附带参考图，仅 AI 生图开关开启时）
   - [DRAW:prompt]          — AI 自由画图（仅 AI 生图开关开启时）
   - 【当前日程列表】         — 活跃日程/闹铃一览
   - 【位置信息】             — 当前地址 + 实时天气 + 离家距离 + 状态（仅有有效坐标时注入）
4. 当前准确时间                                                    ← ⚡缓存分界点
   + [背景记忆] unresolved📌 + 话题相关 + 近期补充（最多8条）      ← 动态
5. [相关记忆] 向量召回的记忆（与背景记忆去重） + assistant 确认   ← 动态
6. 聊天历史（受上下文长度滑块限制）                                ← 动态
```

## 关键实现细节
- **模块化架构**：main.py 仅约 70 行，业务逻辑拆分到 config/database/ws/ai_providers/memory/camera + routes/ 下 5 个路由模块
- **多模态构建**：`build_multimodal_messages()`（硅基流动 base64 URL）和 `build_gemini_contents()`（Gemini inline_data）
- **Token 用量捕获**：stream_ai 通过 meta dict 在流式过程中捕获 Gemini usageMetadata / 硅基流动 usage
- **Gemini 轮次交替**：Gemini API 要求 user/model 严格交替，所有系统注入都以 user+assistant 对形式插入
- **[CAM_CHECK] 流程**：后端在 SSE 中发 `cam_check` 事件 + WebSocket 广播 → 前端播放音频+5秒 setTimeout → POST trigger API → 后端 asyncio.create_task 异步截图+AI分析
- **cam_check 加载指示器**：前端用 `camCheckMsgId` 全局变量跟踪，`renderMessages()` 重建 DOM 后自动恢复指示器
- **语音唤醒架构**：voice.py 运行在独立线程，通过 `asyncio.run_coroutine_threadsafe` 桥接主事件循环；WebRTC VAD (mode=2) 做帧级人声检测（30ms/帧），不需要噪底校准
- **半双工协调**：`ai_speaking` 标志由服务端 `tts_done` WebSocket 事件驱动（前端收到后调用 `notifyVoiceAiSpeaking(false)`），暂停录音期间持续 `stream.read()` 丢弃数据防止缓冲区溢出；voice.py 的 `_async_send` 在 HTTP POST body 中携带 `tts_enabled`/`tts_voice` 参数
- **消息分页**：后端 `?limit=50&before=时间戳` 参数，前端 `loadOlderMessages()` 滚动到顶部自动加载，保持滚动位置
- **SSE + WS 双通道**：cam_check 和 debug 事件同时写入 SSE 流和 WebSocket 广播，确保语音发送的消息（无 SSE 流读取端）也能被前端接收
- **文件导出**：消息变动自动同步到 `chats/{conv_id}.md`，含 YAML front matter，导出跳过 cam_* 角色
- **监控定时器**：基于时间戳比较（`_next_capture_at`），非 sleep 阻塞，间隔修改即时生效
- **摄像头 DirectShow + 验证机制**：所有 `cv2.VideoCapture` 使用 `CAP_DSHOW` 后端（Windows MSMF 后端对 USB 摄像头不稳定）；`_verify_camera()` 最多等 8 秒读到非垃圾帧（`frame.mean() > 5` 排除绿屏/黑屏）才算成功；`_capture_loop` 运行时也检测绿屏帧，连续 100 帧无效触发重连；重连逐个尝试 index 0-4 并验证，失败后 30 秒重试
- **Sentinel 日志压缩**：哨兵每次分析时输出历史概况摘要（summary），避免 Core 唤醒时全量日志导致 token 过高
- **TTS 流式推送架构**：`tts.py` 的 `TTSStreamer` 在 AI 流式输出过程中实时接收文本（`feed()`），按标点（句号/问号/感叹号/换行等）切分为 100-200 字的片段（`_try_split()` + `_find_cut_position()`），每段 `asyncio.create_task` 异步调用硅基流动 CosyVoice2-0.5B 合成 mp3（`_synthesize()`），合成完成后通过 `_dispatch()` 将音频保存到 `data/tts_cache/{msg_id}_s{seq}.mp3` 并 WebSocket 广播 `tts_chunk` 事件；`flush()` 在 AI 输出结束后处理剩余文本并等待所有合成任务完成，最后广播 `tts_done` 事件
- **TTS 多端状态同步**：前端开启 TTS 后通过 WebSocket 发送 `tts_state` 消息，`ConnectionManager.tts_clients` 字典跟踪各连接的 TTS 状态；HTTP POST（send_message/regenerate）body 中的 `tts_enabled`/`tts_voice` 通过 `set_tts_fallback()` 存入 `_tts_fallback` 作为回落，确保 cam_check/闹铃/定时监控等服务端发起的消息也能正确获取 TTS 状态（`any_tts_enabled()` + `get_tts_voice()` 同时检查两处）
- **PC 活动采集**：`PCActivityTracker` 守护线程通过 `win32gui.GetForegroundWindow()` + `psutil.Process.name()` 每 15 秒检测前台窗口变化，通过 `asyncio.run_coroutine_threadsafe()` 桥接主事件循环上报；`pywin32` 和 `psutil` 必须安装在项目 `.venv` 中（系统 Python 中的无效）
- **App 名称解析**：服务端 `KNOWN_APPS` 字典映射 80+ 常见包名/进程名→中文名，`resolve_app_name()` 返回 `None` 表示需过滤的系统应用（桌面、SystemUI 等），读取历史日志时 `_resolve_entries()` 对旧条目重新解析确保名称一致
- **活动日志清理**：`cleanup_old_activity_logs()` 读取→过滤→重写 JSONL 文件，仅保留 `KEEP_HOURS=8` 小时内的条目，每次上报时顺带执行
- **TTS 前端播放流程**：前端 `ttsQueue`（Map，key=msg_id）维护各消息的播放队列，`playNextTTSChunk()` 按 seq 顺序取出分片 URL 播放；收到 `tts_done` WebSocket 事件后标记 `q.finished = true`，当最后一片播放完毕且队列标记结束时，调用 `finishTTSForMsg()` 清理并通知语音模块（`notifyVoiceAiSpeaking(false)`）恢复录音
- **消息编辑 attachments 修复**：后端 `update_message` 广播前 `json.loads` 解析 attachments，避免前端收到字符串导致渲染崩溃
- **PWA 架构**：`sw.js` 和 `manifest.json` 物理存放在 `static/` 目录，但通过 `main.py` 的独立路由从根路径 `/sw.js`、`/manifest.json` 提供，确保 Service Worker 作用域覆盖全站
- **外网访问**：通过 Tailscale 组建虚拟局域网，WireGuard 端到端加密，无需暴露公网端口；代码层面零改动，仅需两端安装 Tailscale 并登录同一账号
- **BLE 玩具集成**：Web Bluetooth API 连接 SOSEXY BLE 设备（服务 0xEE01，写入 0xEE03），sendData2 封包协议（前缀 00 + 18字节分包 + 随机包头 + 终止包）；`whisper_mode` 参数按需注入 `[TOY:x]` 能力到 prompt，后端 `TOY_CMD_PATTERN` 正则检测+strip+广播+`_toy_sys_msg` 系统消息
- **背景记忆浮现**：`build_surfacing_memories(topic, keywords)` 三层策略构建最多 8 条背景记忆：① unresolved 优先（最多 2 条）→ ② 用即时哨兵的 topic 做 embedding 匹配（Top 3，阈值 0.50）→ ③ 最近 3 天的记忆补充。注入时 unresolved 带 📌 前缀，与后续 RAG 召回自动去重
- **记忆阈值**：cosine ≥ 0.75 才召回，top_k=3，去重阈值 0.85
- **静音保活**：`startSilentKeepAlive()` 创建 AudioContext + OscillatorNode（gain=0.001），30 秒循环，防止手机浏览器后台杀 JS 线程导致 WebSocket 断连和闹铃失效
- **Web Notification**：`sendSystemNotification()` 封装 Notification API，闹铃弹窗和监控提醒时同时发送系统推送，需用户授权 `Notification.requestPermission()`
- **AudioBridge 架构**：`AudioBridge.java` 使用 `AudioRecord(VOICE_RECOGNITION, 16000, MONO, PCM_16BIT)`，录音线程每 40ms 读取 1280 字节（640 samples），base64 编码后通过 `evaluateJavascript` 注入 JS；JS 端 `remoteVoice._onNativeChunk()` 解码 → 存入环形 buffer → 能量 VAD 判断语音段 → 静音截断 → 拼接 WAV 头 → POST 到 `/api/voice/remote-asr`
- **远程 ASR 端点**：`routes/voice.py` 的 `/api/voice/remote-asr` 接收 multipart WAV 文件，用 httpx 转发到硅基流动 `https://api.siliconflow.cn/v1/audio/transcriptions`（model=FunAudioLLM/SenseVoiceSmall），返回 `{text}` JSON
- **手机端语音协调**：`remoteVoice` 对象维护 `aiSpeaking` 状态，通过 `notifyVoiceAiSpeaking()` 和 `notifyVoiceCamCheckStart()` 统一分发给 PC 端 `/api/voice/ai-speaking` 或手机端 `remoteVoice._onAiSpeaking()`，TTS 播放完毕（`tts_done` 事件触发）后自动恢复录音
- **音乐点歌架构**：`music.py` 封装 pyncm（`_ensure_session` 线程安全匿名登录），`routes/music.py` 提供 REST API 并导出 `MUSIC_CMD_PATTERN` 正则；`routes/chat.py` 在 send_message 和 regenerate 流结束后检测 `[MUSIC:xxx]`，搜索并通过 SSE `music` 事件 + WebSocket 广播发送卡片数据
- **能力提示合并**：[MUSIC:xxx] 和 [CAM_CHECK] 合并为单个 `[系统能力]` user+assistant 对注入，减少 token 消耗（从 4 条消息降为 2 条）
- **音乐前端渲染**：`msgMusicCards` 字典按消息 ID 存储卡片数据，`renderMusicCards()` / `buildMusicCardHtml()` 生成卡片 DOM，`playMusicOnline()` 创建固定底部播放器，`closeMusicPlayer()` 停止并移除
- **日程/闹铃架构**：`schedule.py` 的 `ScheduleManager` 在独立线程运行（30 秒间隔），通过 `run_coroutine_threadsafe` 桥接主事件循环执行 DB 操作和 WebSocket 广播；`_fire_alarm` 复用 camera.py 相同的 Core 唤醒模式（世界书前缀+记忆+历史+触发提示）；`_parse_dt` 支持 6 种日期时间格式，仅日期时默认 09:00
- **日程系统消息**：`_sys_msg()` 辅助函数在日程创建/删除时插入 system 角色消息到当前对话，风格与哨兵唤醒消息（📷）一致，使用 📅/🗑️ 图标前缀
- **AionPushService 架构**：前台服务使用 OkHttp 4.12.0 维持独立 WebSocket 连接，与 WebView 内的 JS WebSocket 并行但互不干扰。通知通过 `NotificationManager` 发送，渠道 ID 区分优先级。心跳线程是纯 Java `Thread`（非 HandlerThread），`Thread.sleep()` 不依赖 Android Looper 消息队列，锁屏后仍能正常唤醒
- **推送与前端 WebSocket 的关系**：Service 的 WebSocket 仅用于接收消息并弹通知，不做任何 UI 操作。WebView 内的 JS WebSocket 负责完整的 UI 交互。两条连接同时连到服务端 `/ws`，`ConnectionManager.active` 列表中会有两个客户端
- **高德定位架构**：`location.py` 独立模块，`process_heartbeat(lng, lat, accuracy, is_gcj02, skip_sentinel)` 为核心入口。`skip_sentinel` 参数用于测试脚本避免触发哨兵通知。所有高德 API 调用使用 httpx 异步请求，Key 从 `data/location_config.json` 读取
- **WGS84→GCJ-02 坐标转换**：`wgs84_to_gcj02()` 实现完整的国测局加密偏移算法（含 Krasovsky 椭球参数），中国境内坐标最大偏移约 500-700 米。Android 端不做转换，统一由服务端处理
- **三级心跳研判**：`process_heartbeat` 维护 `last_api_lng/lat` 跟踪上次 API 调用的坐标，通过 Haversine 距离判断是否显著移动（≥`movement_threshold` 500m）。轻量级处理零 API 消耗，刷新级消耗 2 次 API（逆地理+天气），完整级额外消耗 1 次 AI 调用（哨兵通知）
- **状态机防误触**：家坐标为 (0,0) 或未设置时保持 `unknown` 状态不做研判；每次心跳先算距离再判状态，状态切换必须经过完整级处理
- **哨兵通知**：`_notify_sentinel()` 调用 `gemini-3.1-flash-lite-preview`，注入世界书人设 + `chat_status.json` 聊天状态 + 记忆召回 + 详细位置上下文（距离/地址/天气），生成自然语言通知消息
- **POI 按需搜索**：`perform_poi_check()` 模式同 `perform_cam_check()`：异步执行，使用最新缓存坐标重新逆地理编码 + POI 搜索 → 构建 system 消息 → 调用 Core 生成跟进回复 → 插入对话 + WebSocket 广播
- **Android 定位线程**：`AionPushService` 中 `startLocationThread()` 启动独立 Java Thread（非 HandlerThread），`Thread.sleep(10min)` 循环，每次先 GET `/api/location/config` 检查 `active` 字段，`active = enabled && !is_location_quiet_hours()`，false 时完全跳过 GPS 采集
- **定位 UI**：chat.html 设置面板中「📍 定位追踪」为可折叠区块（默认收起），监控日志弹窗底部增加「📍 缓存定位」调试行（显示坐标/状态/地址/精度/更新时间）
- **POI 搜索指示器**：前端 `poiSearchMsgId` + `poiSearchCategories` 全局变量跟踪，`handlePoiSearch()` 创建蓝色弹跳动画指示器（样式同 cam-check 绿色），45 秒安全超时自动消失，新 assistant 消息到达时自动移除
- **前台服务类型扩展**：`AndroidManifest.xml` 中 `foregroundServiceType="dataSync|location"`，`startForeground()` 传入 `FOREGROUND_SERVICE_TYPE_DATA_SYNC | FOREGROUND_SERVICE_TYPE_LOCATION`，同时声明 `ACCESS_FINE_LOCATION` + `ACCESS_COARSE_LOCATION` + `ACCESS_BACKGROUND_LOCATION` 权限
- **服务端广播兼容**：`ws.py` 的 `broadcast()` 使用 `try/except` 逐连接发送，单个连接异常不影响其他连接。新增 `except Exception` 兜底确保 RST/EOF 等异常也能清理死连接

