/* ── Aion 聊天室前端逻辑 ── */

const API = '/api/chatroom';
let currentRoom = null;
let rooms = [];
let isSending = false;
let isAiChatting = false;
let chatroomModel = '';
let pendingAttachments = [];  // [{url, type, name}]

const AVATARS = {
  user: '/public/UserIcon.png',
  aion: '/public/gropicon1.png',
  connor: '/public/codexicon.png',
};

// ── 音效 ──
const sndSend = new Audio('/public/发送消息.mp3');
const sndRecv = new Audio('/public/收到消息.mp3');
function playSend() { sndSend.currentTime = 0; sndSend.play().catch(() => {}); }
function playRecv() { sndRecv.currentTime = 0; sndRecv.play().catch(() => {}); }

// ── TTS 语音合成 ──
let crTtsEnabled = localStorage.getItem('chatroom_tts_enabled') === 'true';
let crTtsAionVoice = localStorage.getItem('chatroom_tts_aion_voice') || '';
let crTtsConnorVoice = localStorage.getItem('chatroom_tts_connor_voice') || '';
let crTtsAudio = new Audio();
let crTtsPlaying = false;
let crTtsChunkQueues = {}; // { msgId: { nextPlay: 0, chunks: {seq: url}, finished: bool } }
let crTtsPlayOrder = [];   // msgId 按到达顺序排列

function crEnqueueTTSChunk(msgId, seq, url) {
  if (!crTtsEnabled) return;
  if (!crTtsChunkQueues[msgId]) {
    crTtsChunkQueues[msgId] = { nextPlay: 0, chunks: {}, finished: false };
    crTtsPlayOrder.push(msgId);
  }
  crTtsChunkQueues[msgId].chunks[seq] = url;
  if (!crTtsPlaying) crPlayNextTTSChunk();
}

async function crPlayNextTTSChunk() {
  if (!crTtsEnabled) { crTtsPlaying = false; return; }
  while (crTtsPlayOrder.length > 0) {
    const msgId = crTtsPlayOrder[0];
    const q = crTtsChunkQueues[msgId];
    if (!q) { crTtsPlayOrder.shift(); continue; }
    const nextSeq = q.nextPlay;
    const url = q.chunks[nextSeq];
    if (url === undefined) {
      if (q.finished) {
        const maxSeq = Object.keys(q.chunks).length > 0 ? Math.max(...Object.keys(q.chunks).map(Number)) : -1;
        if (nextSeq > maxSeq) {
          crTtsPlayOrder.shift();
          delete crTtsChunkQueues[msgId];
          continue;
        }
      }
      crTtsPlaying = false;
      return;
    }
    crTtsPlaying = true;
    try {
      crTtsAudio.src = url;
      crTtsAudio.onended = () => { crTtsPlaying = false; q.nextPlay++; crPlayNextTTSChunk(); };
      crTtsAudio.onerror = () => { crTtsPlaying = false; q.nextPlay++; crPlayNextTTSChunk(); };
      await crTtsAudio.play().catch(() => { crTtsPlaying = false; q.nextPlay++; crPlayNextTTSChunk(); });
      return;
    } catch(e) {
      crTtsPlaying = false;
      q.nextPlay++;
    }
  }
  crTtsPlaying = false;
}

function crFinishTTSForMsg(msgId) {
  const q = crTtsChunkQueues[msgId];
  if (!q) return;
  q.finished = true;
  // 尝试清理已播完的
  while (crTtsPlayOrder.length > 0) {
    const id = crTtsPlayOrder[0];
    const qq = crTtsChunkQueues[id];
    if (!qq || !qq.finished) break;
    const maxSeq = Object.keys(qq.chunks).length > 0 ? Math.max(...Object.keys(qq.chunks).map(Number)) : -1;
    if (qq.nextPlay > maxSeq) {
      crTtsPlayOrder.shift();
      delete crTtsChunkQueues[id];
    } else break;
  }
  if (!crTtsPlaying) crPlayNextTTSChunk();
}

function crStopTTS() {
  crTtsAudio.pause();
  crTtsAudio.src = '';
  crTtsChunkQueues = {};
  crTtsPlayOrder = [];
  crTtsPlaying = false;
}

function onTtsToggleChange() {
  crTtsEnabled = document.getElementById('setTtsEnabled').checked;
  localStorage.setItem('chatroom_tts_enabled', crTtsEnabled);
  if (!crTtsEnabled) crStopTTS();
}

async function crLoadTTSVoices() {
  try {
    const resp = await fetch('/api/tts/voices');
    const data = await resp.json();
    const aionSel = document.getElementById('setTtsAionVoice');
    const connorSel = document.getElementById('setTtsConnorVoice');
    if (data.voices && data.voices.length > 0) {
      const opts = data.voices.map(v => {
        const name = v.customName || v.uri || 'Unknown';
        return { uri: v.uri, name };
      });
      aionSel.innerHTML = opts.map(o =>
        `<option value="${o.uri}" ${o.uri === crTtsAionVoice ? 'selected' : ''}>${o.name}</option>`
      ).join('');
      connorSel.innerHTML = opts.map(o =>
        `<option value="${o.uri}" ${o.uri === crTtsConnorVoice ? 'selected' : ''}>${o.name}</option>`
      ).join('');
    } else {
      aionSel.innerHTML = '<option value="">无可用音色</option>';
      connorSel.innerHTML = '<option value="">无可用音色</option>';
    }
  } catch(e) {
    console.error('加载TTS音色失败:', e);
  }
}

// ── DOM ──
const roomListEl = document.getElementById('roomList');
const messagesEl = document.getElementById('messages');
const composer = document.getElementById('composer');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('sendBtn');
const roomTitleEl = document.getElementById('roomTitle');
const menuBtn = document.getElementById('menuBtn');
const sidebar = document.getElementById('sidebar');
const backdrop = document.getElementById('sidebarBackdrop');
const connorDot = document.getElementById('connorDot');
const connorStatusEl = document.getElementById('connorStatus');
const aiChatBtn = document.getElementById('aiChatBtn');
const toastEl = document.getElementById('toast');

// ══════════════════════════════════════════════════
//  工具函数
// ══════════════════════════════════════════════════

function toast(msg, ms = 2000) {
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  setTimeout(() => toastEl.classList.remove('show'), ms);
}

function timeStr(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function isNearBottom() {
  return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 100;
}

function scrollToBottom(force = false) {
  if (force || isNearBottom()) {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }
}

function resizeInput() {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + 'px';
}

// ══════════════════════════════════════════════════
//  API 调用
// ══════════════════════════════════════════════════

async function api(path, opts = {}) {
  const resp = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  return resp.json();
}

async function fetchCurrentModel() {
  try {
    const convs = await (await fetch('/api/conversations')).json();
    if (Array.isArray(convs) && convs.length > 0 && convs[0].model) {
      chatroomModel = convs[0].model;
    }
  } catch {}
}

// ══════════════════════════════════════════════════
//  房间列表
// ══════════════════════════════════════════════════

async function loadRooms() {
  rooms = await api('/rooms');
  renderRoomList();
}

function renderRoomList() {
  roomListEl.innerHTML = rooms.map(r => {
    const active = currentRoom && currentRoom.id === r.id ? 'active' : '';
    const typeBadge = r.type === 'connor_1v1'
      ? '<span class="type-badge connor">私聊</span>'
      : '<span class="type-badge group">群聊</span>';
    return `
      <div class="room-item ${active}" onclick="selectRoom('${r.id}')">
        ${typeBadge}
        <span class="title">${esc(r.title)}</span>
        <span class="msg-count">${r.message_count || 0}</span>
        <button class="del-btn" onclick="event.stopPropagation(); deleteRoom('${r.id}')" title="删除">✕</button>
      </div>`;
  }).join('');
}

async function createRoom(type) {
  const title = type === 'connor_1v1' ? '私聊 ' + (rooms.filter(r => r.type === 'connor_1v1').length + 1) : '群聊 ' + (rooms.filter(r => r.type === 'group').length + 1);
  const result = await api('/rooms', {
    method: 'POST',
    body: JSON.stringify({ title, type }),
  });
  if (result.error) {
    // connor_1v1 已存在，直接切过去
    if (result.existing_id) {
      selectRoom(result.existing_id);
      closeSidebar();
    } else {
      toast(result.error);
    }
    return;
  }
  await loadRooms();
  selectRoom(result.id);
  closeSidebar();
}

async function deleteRoom(roomId) {
  if (!confirm('确定删除此聊天室？消息和记忆将一并删除。')) return;
  await api(`/rooms/${roomId}`, { method: 'DELETE' });
  if (currentRoom && currentRoom.id === roomId) {
    currentRoom = null;
    renderEmptyChat();
  }
  await loadRooms();
}

async function selectRoom(roomId) {
  const room = rooms.find(r => r.id === roomId);
  if (!room) return;
  currentRoom = room;
  renderRoomList();
  roomTitleEl.textContent = room.title;
  composer.style.display = 'flex';
  aiChatBtn.style.display = room.type === 'group' ? '' : 'none';
  await loadMessages();
  closeSidebar();
}

// ══════════════════════════════════════════════════
//  消息
// ══════════════════════════════════════════════════

async function loadMessages() {
  if (!currentRoom) return;
  const msgs = await api(`/rooms/${currentRoom.id}/messages?limit=100`);
  renderMessages(msgs);
  scrollToBottom(true);
}

function renderMessages(msgs) {
  if (!msgs || !msgs.length) {
    messagesEl.innerHTML = `
      <div class="empty-state">
        <div class="icon">${currentRoom.type === 'connor_1v1' ? '🤖' : '👥'}</div>
        <div>${currentRoom.type === 'connor_1v1' ? '和 Connor 开始私聊吧' : '三人群聊，开始吧'}</div>
      </div>`;
    return;
  }
  messagesEl.innerHTML = msgs.map(m => msgHTML(m)).join('');
}

function msgHTML(m) {
  const sender = m.sender || 'user';

  // 系统事件消息（点歌、闹钟等）
  if (sender === 'system') {
    return `<div class="system-event-msg" data-msg-id="${m.id || ''}">${esc(m.content || '')}</div>`;
  }

  const senderNames = { user: '我', aion: 'Aion', connor: 'Connor' };
  const name = senderNames[sender] || sender;
  const avatar = AVATARS[sender] || AVATARS.user;
  const time = timeStr(m.created_at);

  // 用户消息按单换行拆，AI消息按双换行拆
  const isUser = sender === 'user';
  const raw = m.content || '';
  // AI 消息使用 escWithImages 解析 [[image:...]]，用户消息纯转义
  const fmt = isUser ? esc : escWithImages;
  const parts = raw.split(isUser ? /\n+/ : /\n{2,}/).filter(p => p.trim());
  let bubblesHtml;
  if (parts.length > 1) {
    bubblesHtml = '<div class="bubbles">' + parts.map(p => `<div class="bubble">${fmt(p)}</div>`).join('') + '</div>';
  } else {
    bubblesHtml = `<div class="bubble">${fmt(raw)}</div>`;
  }

  // 渲染附件图片
  const attHtml = renderAttachments(m.attachments);

  const msgId = m.id || '';
  const menuHtml = msgId ? `
    <div class="msg-menu-wrap">
      <button class="msg-menu-btn" onclick="toggleMsgMenu(event)">⋯</button>
      <div class="msg-menu-dropdown">
        <button onclick="deleteMsg('${msgId}', this)">删除</button>
      </div>
    </div>` : '';

  const senderLine = sender !== 'user'
    ? `<div class="sender-line"><span class="sender-label ${sender}">${esc(name)}</span>${menuHtml}</div>`
    : (menuHtml ? `<div class="sender-line user-line">${menuHtml}</div>` : '');

  return `
    <div class="message-row ${sender}" data-msg-id="${msgId}">
      <div class="msg-body">
        <img class="avatar" src="${avatar}" alt="${name}">
        <div class="msg-content">
          ${senderLine}
          ${bubblesHtml}
          ${attHtml}
        </div>
      </div>
      <div class="message-meta">${time}</div>
    </div>`;
}

/* ── 消息菜单 ── */
function toggleMsgMenu(e) {
  e.stopPropagation();
  const dropdown = e.currentTarget.nextElementSibling;
  // 关闭所有其他下拉
  document.querySelectorAll('.msg-menu-dropdown.show').forEach(d => { if (d !== dropdown) d.classList.remove('show'); });
  dropdown.classList.toggle('show');
}

async function deleteMsg(msgId, btnEl) {
  try {
    await fetch(`${API}/messages/${msgId}`, { method: 'DELETE' });
    const row = document.querySelector(`[data-msg-id="${msgId}"]`);
    if (row) row.remove();
  } catch (e) { console.error('删除失败', e); }
}

// 点击空白处关闭下拉菜单
document.addEventListener('click', () => {
  document.querySelectorAll('.msg-menu-dropdown.show').forEach(d => d.classList.remove('show'));
});

function appendMessage(m) {
  // 移除空状态
  const empty = messagesEl.querySelector('.empty-state');
  if (empty) empty.remove();
  // 移除 typing 指示器
  const typing = messagesEl.querySelector('.typing-indicator');
  if (typing) typing.remove();

  const div = document.createElement('div');
  div.innerHTML = msgHTML(m);
  messagesEl.appendChild(div.firstElementChild);
  scrollToBottom();
}

function appendTyping(who) {
  const existing = messagesEl.querySelector('.typing-indicator');
  if (existing) existing.remove();
  const div = document.createElement('div');
  div.className = 'typing-indicator';
  div.textContent = `${who} 回复中...`;
  messagesEl.appendChild(div);
  scrollToBottom();
}

function updateTypingStatus(who, statusText) {
  const indicator = messagesEl.querySelector('.typing-indicator');
  if (indicator) {
    indicator.textContent = `${who} ${statusText}`;
  } else {
    appendTyping(who);
    const el = messagesEl.querySelector('.typing-indicator');
    if (el) el.textContent = `${who} ${statusText}`;
  }
}

function appendAiChatStatus(text) {
  const existing = messagesEl.querySelector('.ai-chat-status');
  if (existing) existing.remove();
  const div = document.createElement('div');
  div.className = 'ai-chat-status';
  div.textContent = text;
  messagesEl.appendChild(div);
  scrollToBottom();
}

function removeAiChatStatus() {
  const existing = messagesEl.querySelector('.ai-chat-status');
  if (existing) existing.remove();
}

// ── 流式消息累积 ──
let streamingBubble = null;
let streamingText = '';
let pendingStreamSender = null;
let pendingStreamId = null;

function startStreamingBubble(sender, id) {
  streamingText = '';
  const senderNames = { aion: 'Aion', connor: 'Connor' };
  const name = senderNames[sender] || sender;
  const avatar = AVATARS[sender] || AVATARS.user;

  // 移除 typing
  const typing = messagesEl.querySelector('.typing-indicator');
  if (typing) typing.remove();

  const row = document.createElement('div');
  row.className = `message-row ${sender}`;
  row.id = `streaming-${id}`;
  row.innerHTML = `
    <div class="msg-body">
      <img class="avatar" src="${avatar}" alt="${name}">
      <div class="msg-content">
        <div class="sender-label ${sender}">${esc(name)}</div>
        <div class="bubble"></div>
      </div>
    </div>
    <div class="message-meta">${timeStr(Date.now() / 1000)}</div>`;
  messagesEl.appendChild(row);
  streamingBubble = row.querySelector('.bubble');
  scrollToBottom();
}

function feedStreamingChunk(text) {
  if (!streamingBubble) return;
  streamingText += text;
  streamingBubble.textContent = streamingText;
  scrollToBottom();
}

function endStreamingBubble(attachments) {
  // 流结束后，按双换行拆分成多个气泡，并解析 [[image:...]]
  if (streamingBubble && streamingText) {
    const parts = streamingText.split(/\n{2,}/).filter(p => p.trim());
    if (parts.length > 1) {
      const parent = streamingBubble.parentElement;
      const container = document.createElement('div');
      container.className = 'bubbles';
      parts.forEach(p => {
        const b = document.createElement('div');
        b.className = 'bubble';
        b.innerHTML = escWithImages(p);
        container.appendChild(b);
      });
      parent.replaceChild(container, streamingBubble);
      // 附件图片追加到多气泡容器后面
      const attHtml = renderAttachments(attachments);
      if (attHtml) container.insertAdjacentHTML('afterend', attHtml);
    } else {
      // 单气泡也解析 [[image:...]]
      streamingBubble.innerHTML = escWithImages(streamingText);
      // 附件图片追加到气泡后面
      const attHtml = renderAttachments(attachments);
      if (attHtml) streamingBubble.insertAdjacentHTML('afterend', attHtml);
    }
  }
  streamingBubble = null;
  streamingText = '';
}

// ══════════════════════════════════════════════════
//  发送消息
// ══════════════════════════════════════════════════

composer.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = inputEl.value.trim();
  if ((!text && !pendingAttachments.length) || !currentRoom || isSending) return;

  isSending = true;
  sendBtn.disabled = true;
  inputEl.value = '';
  resizeInput();

  const attachments = pendingAttachments.map(a => a.url);
  pendingAttachments = [];
  renderPreview();

  // 立即显示用户消息
  playSend();
  appendMessage({ sender: 'user', content: text, created_at: Date.now() / 1000, attachments });

  try {
    const resp = await fetch(`${API}/rooms/${currentRoom.id}/send`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: text, model: chatroomModel, attachments, tts_enabled: crTtsEnabled, tts_aion_voice: crTtsAionVoice, tts_connor_voice: crTtsConnorVoice }),
    });

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const data = JSON.parse(line.slice(6));
          handleSSE(data);
        } catch {}
      }
    }
  } catch (err) {
    toast('发送失败: ' + err.message);
  } finally {
    isSending = false;
    sendBtn.disabled = false;
    endStreamingBubble();
    inputEl.focus();
  }
});

function handleSSE(data) {
  switch (data.type) {
    case 'aion_start':
      appendTyping('Aion');
      // 延迟创建流式气泡，等第一个 chunk 到达时再创建
      pendingStreamSender = 'aion';
      pendingStreamId = data.id;
      break;
    case 'aion_status':
      updateTypingStatus('Aion', data.text);
      break;
    case 'aion_chunk':
      if (pendingStreamSender && !streamingBubble) {
        startStreamingBubble(pendingStreamSender, pendingStreamId);
        pendingStreamSender = null;
        pendingStreamId = null;
      }
      feedStreamingChunk(data.content);
      break;
    case 'aion_done':
      pendingStreamSender = null;
      pendingStreamId = null;
      // 用服务端清理后的干净文本替换流式累积的原始文本（包含工具指令）
      if (data.message && data.message.content != null && streamingBubble) {
        streamingText = data.message.content;
      }
      endStreamingBubble(data.message && data.message.attachments);
      playRecv();
      break;
    case 'connor_start':
      appendTyping('Connor');
      pendingStreamSender = 'connor';
      pendingStreamId = data.id;
      break;
    case 'connor_status':
      updateTypingStatus('Connor', data.text);
      break;
    case 'connor_chunk':
      if (pendingStreamSender && !streamingBubble) {
        startStreamingBubble(pendingStreamSender, pendingStreamId);
        pendingStreamSender = null;
        pendingStreamId = null;
      }
      feedStreamingChunk(data.content);
      break;
    case 'connor_done':
      pendingStreamSender = null;
      pendingStreamId = null;
      // 用服务端清理后的干净文本替换流式累积的原始文本
      if (data.message && data.message.content != null && streamingBubble) {
        streamingText = data.message.content;
      }
      endStreamingBubble(data.message && data.message.attachments);
      // 如果 connor_done 带了 message 且没有流式气泡（兼容旧路径），追加消息
      if (data.message
          && !document.getElementById(`streaming-${data.message.id}`)
          && !document.querySelector(`[data-msg-id="${data.message.id}"]`)) {
        appendMessage(data.message);
      }
      playRecv();
      break;
    case 'round_start':
      appendAiChatStatus(`AI 互聊 第 ${data.round}/${data.total} 轮`);
      break;
    case 'tts_chunk':
      crEnqueueTTSChunk(data.data.msg_id, data.data.seq, data.data.url);
      break;
    case 'tts_done':
      crFinishTTSForMsg(data.data.msg_id);
      break;
    case 'error':
      toast('错误: ' + data.content);
      break;
    case 'system_msg':
      if (data.message) { appendMessage(data.message); }
      break;
    case 'music':
      // 音乐播放由 WS broadcast 触发主聊天页面的播放器，这里无需处理
      break;
    case 'heart_whisper':
      // 群聊心语：复用主聊天的心语通知
      if (window.parent && window.parent.postMessage) {
        window.parent.postMessage({type: 'chatroom_heart_whisper', data: data}, '*');
      }
      break;
  }
}

inputEl.addEventListener('input', resizeInput);
inputEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.isComposing) {
    // Shift+Enter 或 Ctrl+Enter 发送，Enter 换行
    if (e.shiftKey || e.ctrlKey) {
      e.preventDefault();
      composer.requestSubmit();
    }
  }
});

// ══════════════════════════════════════════════════
//  AI 互聊
// ══════════════════════════════════════════════════

async function triggerAiChat() {
  if (!currentRoom || currentRoom.type !== 'group' || isAiChatting) return;
  isAiChatting = true;
  aiChatBtn.disabled = true;
  aiChatBtn.textContent = '⏳ 互聊中...';

  try {
    const resp = await fetch(`${API}/rooms/${currentRoom.id}/ai-chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: chatroomModel, tts_enabled: crTtsEnabled, tts_aion_voice: crTtsAionVoice, tts_connor_voice: crTtsConnorVoice }),
    });

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const data = JSON.parse(line.slice(6));
          handleSSE(data);
        } catch {}
      }
    }
  } catch (err) {
    toast('AI 互聊失败: ' + err.message);
  } finally {
    isAiChatting = false;
    aiChatBtn.disabled = false;
    aiChatBtn.textContent = '💬 让他们聊';
    endStreamingBubble();
    removeAiChatStatus();
  }
}

// ══════════════════════════════════════════════════
//  设置
// ══════════════════════════════════════════════════

async function openSettings() {
  if (!currentRoom) { toast('请先选择一个房间'); return; }

  // 先立即打开面板，再异步填充数据（提升感知速度）
  document.getElementById('setTtsEnabled').checked = crTtsEnabled;
  document.getElementById('settingsOverlay').classList.add('active');

  // 三个请求并行发起，避免串行等待外部服务超时
  const [room, cfg] = await Promise.all([
    api(`/rooms/${currentRoom.id}`),
    api('/config'),
    crLoadTTSVoices(),
  ]);

  document.getElementById('setTitle').value = room.title || '';
  document.getElementById('setAionPersona').value = room.aion_persona || '';
  document.getElementById('setConnorPersona').value = room.connor_persona || '';
  document.getElementById('setContextMin').value = room.context_minutes || 30;
  document.getElementById('setAiRounds').value = room.ai_chat_rounds || 3;
  document.getElementById('setConnorUrl').value = cfg.connor_url || 'http://127.0.0.1:8787';

  // connor_1v1 隐藏 Aion 人设和互聊回合
  document.getElementById('fieldAionPersona').style.display = room.type === 'connor_1v1' ? 'none' : '';
  document.getElementById('fieldAiRounds').style.display = room.type === 'connor_1v1' ? 'none' : '';
}

function closeSettings() {
  document.getElementById('settingsOverlay').classList.remove('active');
}

async function saveSettings() {
  if (!currentRoom) return;

  // 保存房间设置
  await api(`/rooms/${currentRoom.id}`, {
    method: 'PUT',
    body: JSON.stringify({
      title: document.getElementById('setTitle').value,
      aion_persona: document.getElementById('setAionPersona').value,
      connor_persona: document.getElementById('setConnorPersona').value,
      context_minutes: parseInt(document.getElementById('setContextMin').value) || 30,
      ai_chat_rounds: parseInt(document.getElementById('setAiRounds').value) || 3,
    }),
  });

  // 保存 Connor 配置
  await api('/config', {
    method: 'PUT',
    body: JSON.stringify({
      connor_url: document.getElementById('setConnorUrl').value,
    }),
  });

  // 保存 TTS 音色配置到 localStorage（开关已由 toggle 实时保存）
  crTtsAionVoice = document.getElementById('setTtsAionVoice').value;
  crTtsConnorVoice = document.getElementById('setTtsConnorVoice').value;
  localStorage.setItem('chatroom_tts_aion_voice', crTtsAionVoice);
  localStorage.setItem('chatroom_tts_connor_voice', crTtsConnorVoice);

  // 刷新
  currentRoom.title = document.getElementById('setTitle').value;
  roomTitleEl.textContent = currentRoom.title;
  await loadRooms();
  closeSettings();
  toast('已保存');
}

async function triggerDigest() {
  if (!currentRoom) return;
  toast('正在总结记忆...');
  const result = await api(`/rooms/${currentRoom.id}/digest`, { method: 'POST' });
  toast(result.message || '总结完成');
  loadMemories();
}

// ══════════════════════════════════════════════════
//  记忆库
// ══════════════════════════════════════════════════

function openMemory() {
  if (!currentRoom) { toast('请先选择一个房间'); return; }
  document.getElementById('memoryOverlay').classList.add('active');
  hideMemForm();
  loadMemories();
  closeSidebar();
}

function closeMemory() {
  document.getElementById('memoryOverlay').classList.remove('active');
}

// 点击遮罩关闭记忆库
document.getElementById('memoryOverlay').addEventListener('click', (e) => {
  if (e.target.id === 'memoryOverlay') closeMemory();
});

async function loadMemories() {
  if (!currentRoom) return;
  const memListEl = document.getElementById('memList');
  try {
    const mems = await api(`/rooms/${currentRoom.id}/memories`);
    if (!Array.isArray(mems) || !mems.length) {
      memListEl.innerHTML = '<div class="mem-empty">暂无记忆，可手动添加或总结生成</div>';
      return;
    }
    memListEl.innerHTML = mems.map(m => {
      const date = new Date(m.created_at * 1000).toLocaleDateString();
      const kw = m.keywords ? `关键词: ${esc(m.keywords)}` : '';
      return `
        <div class="mem-item" data-id="${m.id}">
          <div class="mem-content">${esc(m.content)}</div>
          <div class="mem-meta">
            <span>${date}</span>
            <span>重要度: ${m.importance}</span>
            ${kw ? `<span>${kw}</span>` : ''}
            <div class="mem-actions">
              <button onclick="editMemory('${m.id}')" title="编辑">✏️</button>
              <button class="del" onclick="deleteMemory('${m.id}')" title="删除">✕</button>
            </div>
          </div>
        </div>`;
    }).join('');
  } catch (err) {
    memListEl.innerHTML = `<div class="mem-empty">加载失败: ${err.message}</div>`;
  }
}

function showAddMemory() {
  document.getElementById('memEditId').value = '';
  document.getElementById('memContent').value = '';
  document.getElementById('memKeywords').value = '';
  document.getElementById('memImportance').value = '0.5';
  document.getElementById('memForm').style.display = 'block';
  document.getElementById('memContent').focus();
}

function hideMemForm() {
  document.getElementById('memForm').style.display = 'none';
}

async function editMemory(memId) {
  const mems = await api(`/rooms/${currentRoom.id}/memories`);
  const mem = (Array.isArray(mems) ? mems : []).find(m => m.id === memId);
  if (!mem) { toast('找不到该记忆'); return; }

  document.getElementById('memEditId').value = memId;
  document.getElementById('memContent').value = mem.content || '';
  document.getElementById('memKeywords').value = mem.keywords || '';
  document.getElementById('memImportance').value = mem.importance ?? 0.5;
  document.getElementById('memForm').style.display = 'block';
  document.getElementById('memContent').focus();
}

async function saveMemory() {
  if (!currentRoom) return;
  const editId = document.getElementById('memEditId').value;
  const content = document.getElementById('memContent').value.trim();
  if (!content) { toast('内容不能为空'); return; }

  const body = {
    content,
    keywords: document.getElementById('memKeywords').value.trim(),
    importance: parseFloat(document.getElementById('memImportance').value) || 0.5,
  };

  try {
    let result;
    if (editId) {
      result = await api(`/memories/${editId}`, { method: 'PUT', body: JSON.stringify(body) });
    } else {
      result = await api(`/rooms/${currentRoom.id}/memories`, { method: 'POST', body: JSON.stringify(body) });
    }
    if (result && result.error) {
      toast('保存失败: ' + result.error);
      return;
    }
    toast(editId ? '记忆已更新' : '记忆已添加');
    hideMemForm();
    loadMemories();
  } catch (err) {
    toast('保存失败: ' + err.message);
  }
}

async function deleteMemory(memId) {
  if (!confirm('确定删除此记忆？')) return;
  await api(`/memories/${memId}`, { method: 'DELETE' });
  toast('已删除');
  loadMemories();
}

// 点击遮罩关闭设置
document.getElementById('settingsOverlay').addEventListener('click', (e) => {
  if (e.target.id === 'settingsOverlay') closeSettings();
});

// ══════════════════════════════════════════════════
//  Connor 状态
// ══════════════════════════════════════════════════

async function checkConnor() {
  try {
    const result = await api('/connor-status');
    const online = result.online;
    connorDot.className = `connor-dot ${online ? 'online' : ''}`;
    connorStatusEl.textContent = `Connor: ${online ? '在线' : '离线'}`;
  } catch {
    connorDot.className = 'connor-dot';
    connorStatusEl.textContent = 'Connor: 离线';
  }
}

// ══════════════════════════════════════════════════
//  侧栏
// ══════════════════════════════════════════════════

function openSidebar() { sidebar.classList.add('open'); backdrop.classList.add('active'); }
function closeSidebar() { sidebar.classList.remove('open'); backdrop.classList.remove('active'); }
menuBtn.addEventListener('click', openSidebar);
backdrop.addEventListener('click', closeSidebar);

// ══════════════════════════════════════════════════
//  导航
// ══════════════════════════════════════════════════

function goHome() {
  window.location.href = '/';
}

function renderEmptyChat() {
  roomTitleEl.textContent = '聊天室';
  composer.style.display = 'none';
  aiChatBtn.style.display = 'none';
  messagesEl.innerHTML = `
    <div class="empty-state">
      <div class="icon">💬</div>
      <div>选择或创建一个聊天室开始吧</div>
    </div>`;
}

// ══════════════════════════════════════════════════
//  WebSocket 实时同步
// ══════════════════════════════════════════════════

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.onopen = () => {
    ws.send(JSON.stringify({ type: 'ping' }));
  };

  ws.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.type === 'pong') return;

      if (data.type === 'chatroom_msg_created' && currentRoom) {
        const msg = data.data;
        if (msg.room_id === currentRoom.id) {
          // 避免重复：检查是否已经在页面上
          const existing = document.getElementById(`streaming-${msg.id}`);
          if (!existing && !messagesEl.querySelector(`[data-msg-id="${msg.id}"]`)) {
            // 只在非发送状态下追加（发送时 SSE 已经处理了）
            if (!isSending && !isAiChatting) {
              appendMessage(msg);
              playRecv();
            }
          }
        }
      }

      if (data.type === 'chatroom_msg_deleted' && currentRoom) {
        const d = data.data;
        if (d.room_id === currentRoom.id) {
          const row = document.querySelector(`[data-msg-id="${d.id}"]`);
          if (row) row.remove();
        }
      }

      if (data.type === 'chatroom_room_created' || data.type === 'chatroom_room_deleted' || data.type === 'chatroom_room_updated') {
        loadRooms();
      }
    } catch {}
  };

  ws.onclose = () => setTimeout(connectWS, 3000);
  ws.onerror = () => ws.close();
}

// ══════════════════════════════════════════════════
//  图片上传 & 预览 & 查看器
// ══════════════════════════════════════════════════

function renderAttachments(atts) {
  if (!atts || !atts.length) return '';
  let html = '';
  atts.forEach(item => {
    const url = typeof item === 'string' ? item : (item.url || '');
    if (url) html += `<img src="${esc(url)}" onclick="openImageViewer(this.src)">`;
  });
  return html ? '<div class="msg-media">' + html + '</div>' : '';
}

async function handleChatroomFileSelect(input) {
  for (const file of input.files) {
    const fd = new FormData();
    fd.append('file', file);
    try {
      const res = await fetch(`${API}/upload`, { method: 'POST', body: fd });
      const data = await res.json();
      if (data.error) { toast(data.error); continue; }
      pendingAttachments.push(data);
    } catch (err) {
      toast('上传失败: ' + err.message);
    }
  }
  input.value = '';
  renderPreview();
}

function renderPreview() {
  const area = document.getElementById('previewArea');
  if (!pendingAttachments.length) { area.className = 'preview-area'; area.innerHTML = ''; return; }
  area.className = 'preview-area has-files';
  area.innerHTML = pendingAttachments.map((a, i) => {
    return `<div class="preview-item"><img src="${a.url}"><button class="preview-remove" onclick="removeChatroomAttachment(${i})">✕</button></div>`;
  }).join('');
}

function removeChatroomAttachment(i) {
  pendingAttachments.splice(i, 1);
  renderPreview();
}

function openImageViewer(src) {
  const viewer = document.getElementById('imageViewer');
  document.getElementById('viewerImg').src = src;
  viewer.classList.add('active');
}

function closeImageViewer() {
  document.getElementById('imageViewer').classList.remove('active');
}

// 文件选择绑定
document.getElementById('fileInput').addEventListener('change', function() {
  handleChatroomFileSelect(this);
});

// 粘贴图片
inputEl.addEventListener('paste', async (e) => {
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
      const res = await fetch(`${API}/upload`, { method: 'POST', body: fd });
      const data = await res.json();
      if (data.error) { toast(data.error); continue; }
      pendingAttachments.push(data);
      renderPreview();
    } catch (err) {
      toast('粘贴上传失败: ' + err.message);
    }
  }
});

// ESC 关闭图片查看器
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeImageViewer();
});

// ══════════════════════════════════════════════════
//  转义
// ══════════════════════════════════════════════════

function esc(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

/** 将文本中的 [[image:...]] 标记渲染为 <img>，其余部分转义 */
function escWithImages(str) {
  if (!str) return '';
  const imgRe = /\[\[image:(\S+?)\]\]/g;
  let result = '';
  let lastIdx = 0;
  let match;
  while ((match = imgRe.exec(str)) !== null) {
    const before = str.slice(lastIdx, match.index);
    if (before) result += esc(before);
    // Connor 端 /uploads/ 在聊天室对应 /cr-uploads/
    let imgUrl = match[1];
    if (imgUrl.startsWith('/uploads/')) imgUrl = '/cr-uploads/' + imgUrl.slice('/uploads/'.length);
    const safeUrl = esc(imgUrl);
    result += `<img class="cr-inline-img" src="${safeUrl}" onclick="openImageViewer(this.src)" loading="lazy">`;
    lastIdx = imgRe.lastIndex;
  }
  const tail = str.slice(lastIdx);
  if (tail) result += esc(tail);
  return result;
}

// ══════════════════════════════════════════════════
//  初始化
// ══════════════════════════════════════════════════

(async function init() {
  await fetchCurrentModel();
  await loadRooms();
  // 默认打开最后一次聊天的房间
  if (!currentRoom && rooms.length > 0) {
    await selectRoom(rooms[0].id);
  }
  checkConnor();
  setInterval(checkConnor, 30000);
  connectWS();
  resizeInput();
})();
