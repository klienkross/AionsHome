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

  return { playSend, playRecv, applyTheme, toggleTheme };
})();

const playSend = AionChat.playSend;
const playRecv = AionChat.playRecv;
const applyAionTheme = AionChat.applyTheme;
const toggleAionTheme = AionChat.toggleTheme;
