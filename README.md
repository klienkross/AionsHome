# Aion Chat 项目档案

## 项目定位
局域网 + 外网（Tailscale 组网）多端同步 AI 聊天程序 + 摄像头智能监控系统。PC/手机浏览器同时使用，支持 PWA 安装为独立 App（全屏无地址栏），数据全部存在本地电脑上。

## 技术栈
- **后端**：Python FastAPI + SQLite (aiosqlite) + WebSocket
- **前端**：多页面架构（原生 JS，无框架），暖光主题，手机/PC 自适应。chat.html/css/js 为主聊天页（结构/样式/逻辑分离），独立功能页通过 common.css/common.js 共享样式和工具函数
- **摄像头**：OpenCV (`cv2`) DirectShow 后端后台线程采集 + ESP32-CAM HTTP 远程抓帧（双摄切换 + App 桥接模式）
- **语音**：WebRTC VAD 语音检测 + 硅基流动 ASR (SenseVoiceSmall) + TTS (CosyVoice2) + 语音消息（按住录制）
- **AI 接口**：硅基流动（OpenAI 兼容）、Google Gemini（REST API）、AiPro 中转站（OpenAI 兼容）、Gemini CLI（本地子进程调用，免费 OAuth 认证）、Codex CLI（本地子进程调用，Connor 专用）
- **AI 生图**：Gemini `gemini-3.1-flash-image-preview`（REST API generateContent，responseModalities=["IMAGE"]）
- **Embedding**：Gemini `gemini-embedding-001`（3072维），余弦相似度检索
- **Android App**：Java，WebView + 前台推送服务（OkHttp 4.12.0 WebSocket）+ 原生录音桥 + 原生摄像头桥 + 原生视频录制桥（MediaCodec + MediaMuxer），compileSdk 34 / minSdk 24
- **音乐**：pyncm（网易云音乐 API，搜索/歌曲详情/音频URL，支持 MUSIC_U Cookie VIP 登录 + 服务端代理推流）
- **EPUB 解析**：ebooklib（EPUB 读取）+ BeautifulSoup4 / lxml（HTML 解析）
- **基金监控**：akshare（A股/基金数据拉取）+ chinese-calendar（中国节假日/交易日判断）
- **MCP 娱乐室**：mcp（Python MCP SDK，支持 Streamable HTTP / stdio 传输，接入外部服务如 AI 小镇）
- **聊天室**：三人群聊（用户 + Aion + Connor-Codex），Connor 代理通过 HTTP 轮询接入 Codex CLI 服务，随机回复顺序，统一时间线上下文（私聊+群聊合并排序，场景切换标记），统一记忆总结（Aion/Connor 各自合并私聊+群聊消息总结，独立锚点，1小时无新消息自动触发），图片收发（用户发图→CLI 管线通过本地绝对路径传递、API 管线通过 base64 内嵌，Codex 回复 `[[image:...]]` 标记→前端渲染，图片存储于 `Connor-Codex/uploads/YYYY-MM-DD/`），TTS 语音合成（Aion/Connor 独立音色配置，硅基流动 CosyVoice2 服务端流式切分+并行合成，通过 SSE 推送音频分段顺序播放，配置持久化 localStorage）
- **依赖库**：fastapi, uvicorn, httpx, aiosqlite, opencv-python, Pillow, sounddevice, numpy, webrtcvad-wheels, pyncm, pywin32, psutil, ebooklib, beautifulsoup4, lxml, akshare, chinese-calendar, mcp

## 文档
详细内容已拆分到独立文件：

- [docs/架构.md](docs/架构.md) — 模块化文件结构、路由、支持的模型
- [docs/功能.md](docs/功能.md) — 已实现功能详解 + 各功能工作流程（含动态壁纸）
- [docs/API.md](docs/API.md) — API 一览、SSE/WebSocket 事件、Prompt 注入顺序、关键实现细节
- [docs/踩坑记录.md](docs/踩坑记录.md) — Android 推送服务踩坑 + 最终技术方案
- [CHANGELOG.md](CHANGELOG.md) — 更新日志

## 启动方式
```bash
# 方式一：双击启动脚本
双击 一键启动.bat

# 方式二：命令行
cd aion-chat
python main.py
```
服务监听 `0.0.0.0:8080`

## 访问地址
- PC：`http://localhost:8080`
- 手机：`http://192.xxx.x.xx:8080`（同一 WiFi 下，用 `ipconfig` 查看 WLAN IP）

## 注意事项
- 搬迁目录后需修改 `一键启动.bat` 中的路径（第11行 `cd /d` 后面的绝对路径）
- 所有数据路径都是相对路径，搬迁不影响
- VPN (singbox) 可能干扰局域网访问，必要时关闭或加直连规则
- 防火墙已添加 8080 端口入站规则（规则名 "Aion Chat 8080"）
- 备份只需复制 `data/` 文件夹
