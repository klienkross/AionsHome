# 聊天代码复用重构 + 链式哈希 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 抽取 chat.js 和 chatroom.js 的共用逻辑到 chat-core.js，并为两个消息表添加链式哈希以检测多端同步时的消息丢失。

**Architecture:** 创建 `chat-core.js` 作为共享模块，用全局对象 `AionChat` 暴露公用函数。chat.js 和 chatroom.js 通过 `<script>` 标签先加载 chat-core.js，再加载各自的业务代码。链式哈希在后端消息写入时计算（CRC32），前端同步时比对。

**Tech Stack:** 纯 JavaScript（无构建工具）、Python/aiosqlite（后端）、CRC32（哈希算法，轻量够用）

---

### Task 1: 创建 chat-core.js — 音效模块

**Files:**
- Create: `aion-chat/static/chat-core.js`
- Modify: `aion-chat/static/chat.js:31-52` (删除音效代码)
- Modify: `aion-chat/static/chatroom.js:19-23` (删除音效代码)

- [ ] **Step 1: 创建 chat-core.js 骨架 + 音效**

```javascript
/* ── Aion Chat Core — 共享模块 ── */
const AionChat = (() => {
  // ── 音效 ──
  const sndSend = new Audio('/public/发送消息.mp3');
  const sndRecv = new Audio('/public/收到消息.mp3');
  sndSend.preload = 'auto';
  sndRecv.preload = 'auto';

  let _audioUnlocked = false;
  function _unlockAudio() {
    if (_audioUnlocked) return;
    _audioUnlocked = true;
    sndSend.load();
    sndRecv.load();
    sndSend.volume = 0; sndSend.play().then(() => { sndSend.pause(); sndSend.currentTime = 0; sndSend.volume = 1; }).catch(() => { sndSend.volume = 1; });
    sndRecv.volume = 0; sndRecv.play().then(() => { sndRecv.pause(); sndRecv.currentTime = 0; sndRecv.volume = 1; }).catch(() => { sndRecv.volume = 1; });
    document.removeEventListener('click', _unlockAudio);
    document.removeEventListener('touchstart', _unlockAudio);
  }
  document.addEventListener('click', _unlockAudio);
  document.addEventListener('touchstart', _unlockAudio);

  function playSend() { sndSend.currentTime = 0; sndSend.play().catch(() => {}); }
  function playRecv() { sndRecv.currentTime = 0; sndRecv.play().catch(() => {}); }

  return { playSend, playRecv };
})();

// 向后兼容：直接暴露为全局函数
const playSend = AionChat.playSend;
const playRecv = AionChat.playRecv;
```

- [ ] **Step 2: 从 chat.js 删除音效代码**

删除 chat.js 第 31-52 行（从 `// ── 收发消息音效 ──` 到 `function playRecv()`），因为 chat-core.js 已经提供了。

- [ ] **Step 3: 从 chatroom.js 删除音效代码**

删除 chatroom.js 第 19-23 行：
```javascript
const sndSend = new Audio('/public/发送消息.mp3');
const sndRecv = new Audio('/public/收到消息.mp3');
function playSend() { sndSend.currentTime = 0; sndSend.play().catch(() => {}); }
function playRecv() { sndRecv.currentTime = 0; sndRecv.play().catch(() => {}); }
```

- [ ] **Step 4: 在 HTML 中引入 chat-core.js**

在 `chat.html` 的 `</body>` 前，现有 `<script>` 标签之前添加：
```html
<script src="/static/chat-core.js"></script>
```

在 `chatroom.html` 的 `</body>` 前，现有 `<script>` 标签之前添加：
```html
<script src="/static/chat-core.js"></script>
```

- [ ] **Step 5: 手动测试**

打开私聊页面发消息 → 听到发送音效；收到 AI 回复 → 听到接收音效。
打开群聊页面同样测试。

- [ ] **Step 6: Commit**

```
git add aion-chat/static/chat-core.js aion-chat/static/chat.js aion-chat/static/chatroom.js aion-chat/static/chat.html aion-chat/static/chatroom.html
git commit -m "refactor: 抽取音效逻辑到 chat-core.js"
```

---

### Task 2: chat-core.js — 主题切换

**Files:**
- Modify: `aion-chat/static/chat-core.js`
- Modify: `aion-chat/static/chat.js:54-71` (删除主题代码)

- [ ] **Step 1: 在 chat-core.js 中添加主题模块**

在 `return` 语句之前添加：
```javascript
  // ── 主题 ──
  function applyTheme(theme) {
    const next = theme === 'light' ? 'light' : 'dark';
    document.body.dataset.theme = next;
    localStorage.setItem('aion_chat_theme', next);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', next === 'dark' ? '#050923' : '#eef3ff');
    if (window.AionStatusBar) window.AionStatusBar.setBarStyle(next);
  }

  function toggleTheme() {
    applyTheme(document.body.dataset.theme === 'light' ? 'dark' : 'light');
  }

  applyTheme(localStorage.getItem('aion_chat_theme') || 'dark');
  window.addEventListener('storage', e => {
    if (e.key === 'aion_chat_theme') applyTheme(e.newValue || 'dark');
  });
```

更新 return：`return { playSend, playRecv, applyTheme, toggleTheme };`

添加全局兼容：
```javascript
const applyAionTheme = AionChat.applyTheme;
const toggleAionTheme = AionChat.toggleTheme;
```

- [ ] **Step 2: 从 chat.js 删除主题代码**

删除 chat.js 第 54-71 行（`function applyAionTheme` 到 `storage` listener 结尾）。

- [ ] **Step 3: 测试 + Commit**

在聊天页面切换主题，确认切换正常、刷新后保持。

```
git add aion-chat/static/chat-core.js aion-chat/static/chat.js
git commit -m "refactor: 抽取主题切换到 chat-core.js"
```

---

### Task 3: chat-core.js — 消息格式化 + 图片查看器

**Files:**
- Modify: `aion-chat/static/chat-core.js`
- Modify: `aion-chat/static/chat.js:107-132, 2518-2550` (删除 formatMsg, openImageViewer)
- Modify: `aion-chat/static/chatroom.js:1083-1110` (删除 esc, escWithImages)

- [ ] **Step 1: 在 chat-core.js 中添加格式化 + 图片查看器**

```javascript
  // ── HTML 转义 ──
  function escHtml(s) {
    if (!s) return '';
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  // ── 解析 [[image:...]] 标记 ──
  function escWithImages(str, opts = {}) {
    if (!str) return '';
    const imgRe = /\[\[image:(\S+?)\]\]/g;
    let result = '', lastIdx = 0, match;
    while ((match = imgRe.exec(str)) !== null) {
      const before = str.slice(lastIdx, match.index);
      if (before) result += escHtml(before);
      let imgUrl = match[1];
      if (opts.rewriteUploads && imgUrl.startsWith('/uploads/'))
        imgUrl = opts.rewriteUploads + imgUrl.slice('/uploads/'.length);
      const safeUrl = escHtml(imgUrl);
      result += `<img class="cr-inline-img" src="${safeUrl}" onclick="openImageViewer(this.src)" loading="lazy" style="max-width:100%;border-radius:8px;cursor:pointer;margin:4px 0">`;
      lastIdx = imgRe.lastIndex;
    }
    const tail = str.slice(lastIdx);
    if (tail) result += escHtml(tail);
    return result;
  }

  // ── 消息格式化（转账卡片 + 图片） ──
  function formatMsg(s) {
    const escaped = escHtml(s);
    const transferRe = /\[转账[：:]\s*(-?\d+(?:\.\d+)?)\s*元\]/g;
    let processed = escaped.replace(transferRe, (match, amount) => {
      const val = parseFloat(amount);
      const isNeg = val < 0;
      const absVal = Math.abs(val);
      if (isNeg) {
        return `<div class="transfer-card deduct"><div class="transfer-card-body"><div class="transfer-card-amount">¥${absVal}</div><div class="transfer-card-desc">钱包扣除</div></div><div class="transfer-card-footer">扣除</div></div>`;
      } else {
        return `<div class="transfer-card"><div class="transfer-card-body"><div class="transfer-card-amount">¥${absVal}</div><div class="transfer-card-desc">发起了一笔转账</div></div><div class="transfer-card-footer">转账</div></div>`;
      }
    });
    const imgRe = /\[\[image:(\S+?)\]\]/g;
    let result = '', lastIdx = 0, m;
    while ((m = imgRe.exec(processed)) !== null) {
      result += processed.slice(lastIdx, m.index).replace(/\n/g, '<br>');
      const safeUrl = m[1];
      result += `<img class="cr-inline-img" src="${safeUrl}" onclick="openImageViewer && openImageViewer(this.src)" loading="lazy" style="max-width:100%;border-radius:8px;cursor:pointer;margin:4px 0">`;
      lastIdx = imgRe.lastIndex;
    }
    result += processed.slice(lastIdx).replace(/\n/g, '<br>');
    return result;
  }

  // ── 图片查看器 ──
  function openImageViewer(url) {
    const overlay = document.createElement('div');
    overlay.className = 'image-viewer-overlay';
    overlay.innerHTML = `
      <button class="image-viewer-close" onclick="this.parentElement.remove()">&times;</button>
      <img src="${url}" alt="图片">
      <div class="image-viewer-actions">
        <button onclick="saveImage('${url}')">💾 保存图片</button>
        <button onclick="this.closest('.image-viewer-overlay').remove()">关闭</button>
      </div>
    `;
    overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
    document.body.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add('active'));
  }

  function closeImageViewer() {
    const viewer = document.querySelector('.image-viewer-overlay');
    if (viewer) viewer.remove();
  }
```

更新 return 和全局暴露。

- [ ] **Step 2: 从 chat.js 删除 `escHtml`, `formatMsg`, `openImageViewer`, `saveImage`**

注意：`saveImage` 保留在 chat.js 中（包含 Android 原生桥接逻辑），chat-core.js 的 `openImageViewer` 调用它时它需要存在。

- [ ] **Step 3: 从 chatroom.js 删除 `esc`, `escWithImages`, `openImageViewer`, `closeImageViewer`**

chatroom.js 的 `escWithImages` 调用改为使用 chat-core 版本并传 `{ rewriteUploads: '/cr-uploads/' }`。

在 chatroom.js 的 `msgHTML` 函数中：
```javascript
// 原: const fmt = isUser ? esc : escWithImages;
const fmt = isUser ? escHtml : (s => escWithImages(s, { rewriteUploads: '/cr-uploads/' }));
```

- [ ] **Step 4: 测试 + Commit**

测试：发送带 `[[image:...]]` 的消息、转账消息、点击图片查看。

```
git add aion-chat/static/chat-core.js aion-chat/static/chat.js aion-chat/static/chatroom.js
git commit -m "refactor: 抽取消息格式化和图片查看器到 chat-core.js"
```

---

### Task 4: chat-core.js — 附件上传 + 预览

**Files:**
- Modify: `aion-chat/static/chat-core.js`
- Modify: `aion-chat/static/chat.js:2725-2774`
- Modify: `aion-chat/static/chatroom.js:995-1077`

- [ ] **Step 1: 在 chat-core.js 添加附件模块**

```javascript
  // ── 附件上传 ──
  function createAttachmentManager(uploadUrl, previewAreaId) {
    let pending = [];

    async function handleFiles(input) {
      for (const file of input.files) {
        const fd = new FormData();
        fd.append('file', file);
        try {
          const res = await fetch(uploadUrl, { method: 'POST', body: fd });
          const data = await res.json();
          if (data.error) { showToast(data.error); continue; }
          pending.push(data);
        } catch (err) {
          showToast('上传失败: ' + err.message);
        }
      }
      input.value = '';
      render();
    }

    async function handlePaste(e) {
      const items = e.clipboardData && e.clipboardData.items;
      if (!items) return;
      for (const item of items) {
        if (!item.type.startsWith('image/')) continue;
        e.preventDefault();
        const file = item.getAsFile();
        if (!file) continue;
        const fd = new FormData();
        fd.append('file', file);
        try {
          const res = await fetch(uploadUrl, { method: 'POST', body: fd });
          const data = await res.json();
          if (data.error) { showToast(data.error); continue; }
          pending.push(data);
          render();
        } catch (err) {
          showToast('粘贴上传失败: ' + err.message);
        }
      }
    }

    function remove(i) { pending.splice(i, 1); render(); }

    function render() {
      const area = document.getElementById(previewAreaId);
      if (!area) return;
      if (!pending.length) { area.className = 'preview-area'; area.innerHTML = ''; return; }
      area.className = 'preview-area has-files';
      area.innerHTML = pending.map((a, i) => {
        const isVid = a.type && a.type.startsWith('video/');
        const media = isVid ? `<video src="${a.url}" muted></video>` : `<img src="${a.url}">`;
        return `<div class="preview-item">${media}<button class="preview-remove" onclick="attachments.remove(${i})">✕</button></div>`;
      }).join('');
    }

    function flush() { const urls = pending.map(a => a.url); pending = []; render(); return urls; }
    function hasPending() { return pending.length > 0; }

    return { handleFiles, handlePaste, remove, render, flush, hasPending, get pending() { return pending; } };
  }
```

- [ ] **Step 2: 重构 chat.js 使用 attachmentManager**

在 chat.js 开头（删除旧 `pendingAttachments` 及相关函数后）：
```javascript
const attachments = AionChat.createAttachmentManager('/api/upload', 'previewArea');
```

将原有的 `handleFileSelect` 调用替换为 `attachments.handleFiles(input)`，
`renderPreview()` → `attachments.render()`，
`pendingAttachments.length` → `attachments.hasPending()`，
发送时 `pendingAttachments.map(a => a.url); pendingAttachments = [];` → `attachments.flush()`。

- [ ] **Step 3: 重构 chatroom.js 使用 attachmentManager**

```javascript
const attachments = AionChat.createAttachmentManager('/api/chatroom/upload', 'previewArea');
```

同样替换所有引用。

- [ ] **Step 4: 测试 + Commit**

测试：选择文件上传、粘贴图片、删除预览项、发送后预览区清空。

```
git add aion-chat/static/chat-core.js aion-chat/static/chat.js aion-chat/static/chatroom.js
git commit -m "refactor: 抽取附件上传预览到 chat-core.js"
```

---

### Task 5: chat-core.js — 滚动工具函数

**Files:**
- Modify: `aion-chat/static/chat-core.js`
- Modify: `aion-chat/static/chatroom.js:166-179`

- [ ] **Step 1: 在 chat-core.js 添加滚动工具**

```javascript
  // ── 滚动工具 ──
  function createScrollHelper(containerEl) {
    function isNearBottom() {
      return containerEl.scrollHeight - containerEl.scrollTop - containerEl.clientHeight < 100;
    }
    function scrollToBottom(force = false) {
      if (force || isNearBottom()) containerEl.scrollTop = containerEl.scrollHeight;
    }
    return { isNearBottom, scrollToBottom };
  }

  function setupAutoResize(textarea, maxHeight = 120) {
    textarea.addEventListener('input', () => {
      textarea.style.height = 'auto';
      textarea.style.height = Math.min(textarea.scrollHeight, maxHeight) + 'px';
    });
  }
```

- [ ] **Step 2: chatroom.js 使用 createScrollHelper**

```javascript
const scroll = AionChat.createScrollHelper(messagesEl);
// 原 scrollToBottom(force) → scroll.scrollToBottom(force)
// 原 isNearBottom() → scroll.isNearBottom()
```

保留 chat.js 的 `scrollBottom()` 不动（它有更复杂的抑制逻辑 `_suppressScrollBottom`）。

- [ ] **Step 3: 测试 + Commit**

```
git add aion-chat/static/chat-core.js aion-chat/static/chatroom.js
git commit -m "refactor: 抽取滚动工具到 chat-core.js"
```

---

### Task 6: 链式哈希 — 后端数据库 + 计算逻辑

**Files:**
- Modify: `aion-chat/database.py` (两个表添加 chain_hash 字段)
- Create: `aion-chat/chain_hash.py` (哈希计算模块)

- [ ] **Step 1: 创建 chain_hash.py**

```python
"""链式哈希 — 用于检测多端同步时的消息丢失"""
import struct

_CRC32_TABLE = None

def _ensure_table():
    global _CRC32_TABLE
    if _CRC32_TABLE is not None:
        return
    _CRC32_TABLE = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0xEDB88320 if crc & 1 else crc >> 1
        _CRC32_TABLE.append(crc & 0xFFFFFFFF)

def crc32(data: bytes) -> int:
    _ensure_table()
    crc = 0xFFFFFFFF
    for b in data:
        crc = _CRC32_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF

def compute_chain_hash(prev_hash: str, msg_id: str, content: str, created_at: float) -> str:
    """计算链式哈希：hash(prev_hash + msg_id + content + timestamp)"""
    payload = f"{prev_hash}|{msg_id}|{content}|{created_at:.6f}".encode('utf-8')
    return format(crc32(payload), '08x')
```

- [ ] **Step 2: 数据库添加 chain_hash 字段**

在 `database.py` 的 `init_db` 函数中，两个表各加 migration：

```python
# messages 表
try:
    await db.execute("ALTER TABLE messages ADD COLUMN chain_hash TEXT DEFAULT ''")
except:
    pass

# chatroom_messages 表
try:
    await db.execute("ALTER TABLE chatroom_messages ADD COLUMN chain_hash TEXT DEFAULT ''")
except:
    pass
```

- [ ] **Step 3: Commit**

```
git add aion-chat/chain_hash.py aion-chat/database.py
git commit -m "feat: 链式哈希模块 + 数据库字段迁移"
```

---

### Task 7: 链式哈希 — 写入时计算

**Files:**
- Modify: `aion-chat/routes/chat.py` (私聊消息写入时计算 chain_hash)
- Modify: `aion-chat/routes/chatroom.py` (群聊消息写入时计算 chain_hash)

- [ ] **Step 1: 找到私聊消息插入点**

在 `routes/chat.py` 中找到 INSERT INTO messages 的位置。插入前：
1. 查询该会话最新一条消息的 chain_hash
2. 用 `compute_chain_hash(prev_hash, msg_id, content, created_at)` 计算新哈希
3. 将 chain_hash 写入 INSERT 语句

```python
from chain_hash import compute_chain_hash

# 获取前一条消息的 chain_hash
row = await db.execute_fetchone(
    "SELECT chain_hash FROM messages WHERE conv_id = ? ORDER BY created_at DESC LIMIT 1",
    (conv_id,)
)
prev_hash = row[0] if row and row[0] else '00000000'
new_hash = compute_chain_hash(prev_hash, msg_id, content, created_at)
```

- [ ] **Step 2: 同样处理 chatroom 消息插入**

在 `routes/chatroom.py` 中找到 INSERT INTO chatroom_messages 的位置，同样添加链式哈希计算。

```python
row = await db.execute_fetchone(
    "SELECT chain_hash FROM chatroom_messages WHERE room_id = ? ORDER BY created_at DESC LIMIT 1",
    (room_id,)
)
prev_hash = row[0] if row and row[0] else '00000000'
new_hash = compute_chain_hash(prev_hash, msg_id, content, created_at)
```

- [ ] **Step 3: API 返回消息时包含 chain_hash**

确保 GET 消息列表的查询 SELECT 中包含 chain_hash 字段，序列化时带给前端。

- [ ] **Step 4: Commit**

```
git add aion-chat/routes/chat.py aion-chat/routes/chatroom.py
git commit -m "feat: 消息写入时计算链式哈希"
```

---

### Task 8: 链式哈希 — 同步校验 API

**Files:**
- Modify: `aion-chat/routes/chat.py` (添加校验 endpoint)
- Modify: `aion-chat/routes/chatroom.py` (添加校验 endpoint)

- [ ] **Step 1: 添加私聊会话哈希校验 API**

```python
@router.get("/api/conversations/{conv_id}/hash")
async def get_conv_hash(conv_id: str):
    """返回该会话最新消息的 chain_hash 和消息数量"""
    async with aiosqlite.connect(DB_PATH) as db:
        row = await db.execute_fetchone(
            "SELECT chain_hash, COUNT(*) FROM messages WHERE conv_id = ? ORDER BY created_at DESC LIMIT 1",
            (conv_id,)
        )
        # 更准确的查询
        hash_row = await db.execute_fetchone(
            "SELECT chain_hash FROM messages WHERE conv_id = ? ORDER BY created_at DESC LIMIT 1",
            (conv_id,)
        )
        count_row = await db.execute_fetchone(
            "SELECT COUNT(*) FROM messages WHERE conv_id = ?",
            (conv_id,)
        )
        return {
            "chain_hash": hash_row[0] if hash_row else "00000000",
            "count": count_row[0] if count_row else 0
        }
```

- [ ] **Step 2: 添加群聊房间哈希校验 API**

```python
@router.get("/api/chatroom/rooms/{room_id}/hash")
async def get_room_hash(room_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        hash_row = await db.execute_fetchone(
            "SELECT chain_hash FROM chatroom_messages WHERE room_id = ? ORDER BY created_at DESC LIMIT 1",
            (room_id,)
        )
        count_row = await db.execute_fetchone(
            "SELECT COUNT(*) FROM chatroom_messages WHERE room_id = ?",
            (room_id,)
        )
        return {
            "chain_hash": hash_row[0] if hash_row else "00000000",
            "count": count_row[0] if count_row else 0
        }
```

- [ ] **Step 3: 添加二分定位 API（找到第一个 hash 不一致的位置）**

```python
@router.post("/api/conversations/{conv_id}/hash/verify")
async def verify_conv_hashes(conv_id: str, request: Request):
    """接收客户端的 {hashes: [{id, chain_hash}]} 列表，返回第一个不一致的 msg_id"""
    body = await request.json()
    client_hashes = body.get("hashes", [])
    async with aiosqlite.connect(DB_PATH) as db:
        for item in client_hashes:
            row = await db.execute_fetchone(
                "SELECT chain_hash FROM messages WHERE id = ?",
                (item["id"],)
            )
            server_hash = row[0] if row else None
            if server_hash != item["chain_hash"]:
                return {"match": False, "diverge_at": item["id"]}
        return {"match": True}
```

- [ ] **Step 4: Commit**

```
git add aion-chat/routes/chat.py aion-chat/routes/chatroom.py
git commit -m "feat: 链式哈希校验 API"
```

---

### Task 9: 链式哈希 — 前端同步检查

**Files:**
- Modify: `aion-chat/static/chat.js` (WS 重连时校验)
- Modify: `aion-chat/static/chatroom.js` (WS 重连时校验)

- [ ] **Step 1: chat.js — WS 重连时比对哈希**

在 `connectWS` 的 `ws.onopen` 回调中，加入同步校验：

```javascript
// WS 重连时，检查当前会话的消息完整性
async function checkSyncIntegrity() {
  if (!currentConvId || !currentMessages.length) return;
  const lastMsg = currentMessages[currentMessages.length - 1];
  if (!lastMsg.chain_hash) return;
  try {
    const resp = await api("GET", `/api/conversations/${currentConvId}/hash`);
    if (resp.chain_hash !== lastMsg.chain_hash) {
      console.warn('[Sync] 哈希不一致，重新加载消息');
      const msgs = await api("GET", `/api/conversations/${currentConvId}/messages?limit=${MSG_PAGE_SIZE}`);
      currentMessages = msgs;
      renderMessages();
    }
  } catch {}
}
```

在 `ws.onopen` 中调用 `checkSyncIntegrity()`。

- [ ] **Step 2: chatroom.js — 同样处理**

```javascript
async function checkSyncIntegrity() {
  if (!currentRoom) return;
  try {
    const resp = await api(`/rooms/${currentRoom.id}/hash`);
    const lastEl = messagesEl.querySelector('.message-row:last-child');
    // 简单策略：如果消息数量不一致就刷新
    const localCount = messagesEl.querySelectorAll('.message-row').length;
    if (resp.count !== localCount) {
      await loadMessages();
    }
  } catch {}
}
```

- [ ] **Step 3: 测试 + Commit**

模拟场景：在一端发消息，另一端断开 WS 后重连，观察是否自动补齐。

```
git add aion-chat/static/chat.js aion-chat/static/chatroom.js
git commit -m "feat: 前端 WS 重连时链式哈希校验"
```

---

### Task 10: 历史消息回填哈希

**Files:**
- Create: `aion-chat/scripts/backfill_hashes.py`

- [ ] **Step 1: 写回填脚本**

```python
"""一次性回填已有消息的 chain_hash"""
import asyncio
import aiosqlite
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + '/..')
from chain_hash import compute_chain_hash

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'chat.db')

async def backfill():
    async with aiosqlite.connect(DB_PATH) as db:
        # 私聊
        convs = await db.execute_fetchall("SELECT DISTINCT conv_id FROM messages")
        for (conv_id,) in convs:
            msgs = await db.execute_fetchall(
                "SELECT id, content, created_at FROM messages WHERE conv_id = ? ORDER BY created_at ASC",
                (conv_id,)
            )
            prev_hash = '00000000'
            for msg_id, content, created_at in msgs:
                new_hash = compute_chain_hash(prev_hash, msg_id, content or '', created_at)
                await db.execute("UPDATE messages SET chain_hash = ? WHERE id = ?", (new_hash, msg_id))
                prev_hash = new_hash

        # 群聊
        rooms = await db.execute_fetchall("SELECT DISTINCT room_id FROM chatroom_messages")
        for (room_id,) in rooms:
            msgs = await db.execute_fetchall(
                "SELECT id, content, created_at FROM chatroom_messages WHERE room_id = ? ORDER BY created_at ASC",
                (room_id,)
            )
            prev_hash = '00000000'
            for msg_id, content, created_at in msgs:
                new_hash = compute_chain_hash(prev_hash, msg_id, content or '', created_at)
                await db.execute("UPDATE chatroom_messages SET chain_hash = ? WHERE id = ?", (new_hash, msg_id))
                prev_hash = new_hash

        await db.commit()
    print("Done: all messages backfilled with chain_hash")

if __name__ == '__main__':
    asyncio.run(backfill())
```

- [ ] **Step 2: 运行回填**

```
cd aion-chat/scripts && python backfill_hashes.py
```

- [ ] **Step 3: Commit**

```
git add aion-chat/scripts/backfill_hashes.py
git commit -m "chore: 历史消息链式哈希回填脚本"
```
