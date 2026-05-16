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

const playSend = AionChat.playSend;
const playRecv = AionChat.playRecv;
