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
    const existing = document.getElementById('imageViewer');
    if (existing) {
      const img = document.getElementById('viewerImg');
      if (img) { img.src = url; existing.classList.add('active'); return; }
    }
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
    const fixed = document.getElementById('imageViewer');
    if (fixed) fixed.classList.remove('active');
  }

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

  return { playSend, playRecv, applyTheme, toggleTheme, escHtml, escWithImages, formatMsg, openImageViewer, closeImageViewer };
})();

const playSend = AionChat.playSend;
const playRecv = AionChat.playRecv;
const applyAionTheme = AionChat.applyTheme;
const toggleAionTheme = AionChat.toggleTheme;
const escHtml = AionChat.escHtml;
const esc = AionChat.escHtml;
const escWithImages = AionChat.escWithImages;
const formatMsg = AionChat.formatMsg;
const openImageViewer = AionChat.openImageViewer;
const closeImageViewer = AionChat.closeImageViewer;
