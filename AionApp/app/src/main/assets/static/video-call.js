/**
 * 视频通话模块 — video-call.js
 * 独立于语音唤醒的全新视频通话功能
 * 依赖 chat.html 中的全局变量: currentConvId, _clientId, ttsEnabled, ttsVoiceId, ws, sending, $()
 */

const videoCall = (() => {
  // ── 状态 ──
  let _active = false;        // 是否在视频通话中
  let _ringing = false;       // 是否在响铃中
  let _ringStartTime = 0;     // 响铃开始时间（用于判断 <5s / ≥5s 接听）
  let _overlay = null;        // DOM 遮罩层
  let _ringAudio = null;      // 铃声 Audio 对象
  let _cameraStream = null;   // 摄像头 MediaStream
  let _facingMode = 'environment'; // 默认后置摄像头
  let _swapped = false;       // 大小画面是否互换
  let _convId = null;         // 当前通话关联的对话 ID
  let _useNativeCamera = false; // 是否使用原生摄像头桥接
  let _nativeCamTimer = null;   // 原生摄像头 rAF ID

  // ── 语音/录制状态 ──
  let _voiceStream = null;
  let _voiceCtx = null;
  let _voiceProcessor = null;
  let _sampleRate = 48000;
  let _useNativeAudio = false;
  let _ownNativeBridge = false;
  let _aiSpeaking = false;
  let _callStartTime = 0;

  // ── 视频录制状态 ──
  let _videoRecording = false;    // 是否正在录制视频
  let _videoRecorder = null;      // 浏览器 MediaRecorder（视频+音频）
  let _audioForASR = null;        // 浏览器 MediaRecorder（纯音频，用于 ASR）
  let _videoChunks = [];
  let _audioChunks = [];
  let _nativeAudioFrames = [];    // Android 原生桥接录制期间收集的 PCM base64 帧
  let _recordStartTime = 0;
  let _recordTimerEl = null;
  let _recordTimerInterval = null;
  let _processing = false;        // 是否正在处理发送
  let _lastInteractionTime = 0;   // 最后交互时间（用于不活跃超时）
  let _inactivityTimer = null;
  const MAX_RECORD_SECONDS = 60;

  // ── 获取 AI 名称 ──
  function _getAiName() {
    if (typeof worldBook !== 'undefined' && worldBook.ai_name) return worldBook.ai_name;
    return 'AI';
  }

  // ── 工具函数 ──
  function _createElement(tag, attrs = {}, styles = {}) {
    const el = document.createElement(tag);
    Object.entries(attrs).forEach(([k, v]) => {
      if (k === 'textContent') el.textContent = v;
      else if (k === 'innerHTML') el.innerHTML = v;
      else el.setAttribute(k, v);
    });
    Object.assign(el.style, styles);
    return el;
  }

  // ── 来电界面 ──
  function _showIncomingUI(onAccept, onReject) {
    _removeOverlay();
    _overlay = _createElement('div', { id: 'videoCallOverlay' }, {
      position: 'fixed', top: 0, left: 0, width: '100%', height: '100%',
      zIndex: 99999, display: 'flex', flexDirection: 'column',
      alignItems: 'center', justifyContent: 'center',
      background: '#000'
    });

    // 背景图
    const bg = _createElement('img', { src: '/public/视频通话背景.jpg' }, {
      position: 'absolute', top: 0, left: 0, width: '100%', height: '100%',
      objectFit: 'cover', opacity: '0.4'
    });
    _overlay.appendChild(bg);

    // 内容容器
    const content = _createElement('div', {}, {
      position: 'relative', zIndex: 1, display: 'flex', flexDirection: 'column',
      alignItems: 'center', gap: '16px'
    });

    // 来电头像
    const avatar = _createElement('img', { src: '/public/视频来电头像.jpg' }, {
      width: '120px', height: '120px', borderRadius: '50%', objectFit: 'cover',
      border: '3px solid rgba(255,255,255,0.3)'
    });
    content.appendChild(avatar);

    // AI 名字 + 来电
    const aiName = _getAiName();
    const nameEl = _createElement('div', { textContent: `${aiName} 来电` }, {
      color: '#fff', fontSize: '22px', fontWeight: '500', marginTop: '8px'
    });
    content.appendChild(nameEl);

    // 按钮区域
    const btnArea = _createElement('div', {}, {
      display: 'flex', gap: '60px', marginTop: '60px', alignItems: 'center'
    });

    // 挂断按钮
    const rejectBtn = _createElement('div', {}, {
      display: 'flex', flexDirection: 'column', alignItems: 'center', cursor: 'pointer'
    });
    const rejectImg = _createElement('img', { src: '/public/挂断.png' }, {
      width: '64px', height: '64px'
    });
    const rejectLabel = _createElement('div', { textContent: '挂断' }, {
      color: '#fff', fontSize: '13px', marginTop: '8px'
    });
    rejectBtn.appendChild(rejectImg);
    rejectBtn.appendChild(rejectLabel);
    rejectBtn.onclick = onReject;

    // 接听按钮（晃动动画）
    const acceptBtn = _createElement('div', {}, {
      display: 'flex', flexDirection: 'column', alignItems: 'center', cursor: 'pointer'
    });
    const acceptImg = _createElement('img', { src: '/public/接听.png' }, {
      width: '64px', height: '64px', animation: 'vcShake 0.8s ease-in-out infinite'
    });
    const acceptLabel = _createElement('div', { textContent: '接听' }, {
      color: '#fff', fontSize: '13px', marginTop: '8px'
    });
    acceptBtn.appendChild(acceptImg);
    acceptBtn.appendChild(acceptLabel);
    acceptBtn.onclick = onAccept;

    btnArea.appendChild(rejectBtn);
    btnArea.appendChild(acceptBtn);
    content.appendChild(btnArea);

    _overlay.appendChild(content);

    // 注入 CSS 动画
    if (!document.getElementById('vcStyles')) {
      const style = document.createElement('style');
      style.id = 'vcStyles';
      style.textContent = `
        @keyframes vcShake {
          0%, 100% { transform: rotate(0deg); }
          15% { transform: rotate(15deg); }
          30% { transform: rotate(-15deg); }
          45% { transform: rotate(12deg); }
          60% { transform: rotate(-10deg); }
          75% { transform: rotate(5deg); }
        }
        #videoCallOverlay * { user-select: none; -webkit-user-select: none; }
      `;
      document.head.appendChild(style);
    }

    document.body.appendChild(_overlay);
  }

  // ── 视频通话界面 ──
  async function _showCallUI(initialStatus) {
    _removeOverlay();
    _active = true;
    _swapped = false;

    _overlay = _createElement('div', { id: 'videoCallOverlay' }, {
      position: 'fixed', top: 0, left: 0, width: '100%', height: '100%',
      zIndex: 99999, background: '#000', overflow: 'hidden'
    });

    // 大画面容器（默认 AI 照片）
    const mainView = _createElement('div', { id: 'vcMainView' }, {
      position: 'absolute', top: 0, left: 0, width: '100%', height: '100%'
    });
    const aiImg = _createElement('img', { id: 'vcAiPhoto', src: '/public/视频背景照片.jpg' }, {
      width: '100%', height: '100%', objectFit: 'cover'
    });
    mainView.appendChild(aiImg);
    _overlay.appendChild(mainView);

    // 小画面容器（默认用户摄像头，右上角）
    const pipView = _createElement('div', { id: 'vcPipView' }, {
      position: 'absolute', top: '50px', right: '16px', width: '120px', height: '170px',
      borderRadius: '12px', overflow: 'hidden', border: '2px solid rgba(255,255,255,0.3)',
      cursor: 'pointer', zIndex: 2, background: '#222'
    });
    const userVideo = _createElement('video', {
      id: 'vcUserVideo', autoplay: '', playsinline: '', muted: ''
    }, {
      width: '100%', height: '100%', objectFit: 'cover',
      transform: 'scaleX(-1)' // 前置摄像头镜像
    });
    pipView.appendChild(userVideo);
    // 原生摄像头回退用的 <img>（默认隐藏）
    const userImg = _createElement('img', { id: 'vcUserImg' }, {
      width: '100%', height: '100%', objectFit: 'cover', display: 'none',
      position: 'absolute', top: 0, left: 0
    });
    pipView.appendChild(userImg);
    _overlay.appendChild(pipView);

    // PiP 中的 AI 照片（互换时使用，默认隐藏）
    const pipAi = _createElement('img', { id: 'vcPipAi', src: '/public/视频背景照片.jpg' }, {
      width: '100%', height: '100%', objectFit: 'cover', display: 'none',
      position: 'absolute', top: 0, left: 0
    });
    pipView.appendChild(pipAi);

    // 主画面中的用户视频（互换时使用，默认隐藏）
    const mainVideo = _createElement('video', {
      id: 'vcMainVideo', autoplay: '', playsinline: '', muted: ''
    }, {
      width: '100%', height: '100%', objectFit: 'cover', display: 'none',
      position: 'absolute', top: 0, left: 0
    });
    mainView.appendChild(mainVideo);
    // 原生摄像头回退用的大画面 <img>（默认隐藏）
    const mainImg = _createElement('img', { id: 'vcMainImg' }, {
      width: '100%', height: '100%', objectFit: 'cover', display: 'none',
      position: 'absolute', top: 0, left: 0
    });
    mainView.appendChild(mainImg);

    // 点击 PiP 互换大小画面
    pipView.onclick = () => _toggleSwap();

    // 通话状态指示
    const statusBar = _createElement('div', { id: 'vcStatus' }, {
      position: 'absolute', top: '12px', left: '50%', transform: 'translateX(-50%)',
      color: '#fff', fontSize: '14px', background: 'rgba(0,0,0,0.5)',
      padding: '4px 16px', borderRadius: '16px', zIndex: 3,
      whiteSpace: 'nowrap'
    });
    statusBar.textContent = initialStatus || '通话中';
    _overlay.appendChild(statusBar);

    // 底部按钮栏
    const bottomBar = _createElement('div', { id: 'vcBottomBar' }, {
      position: 'absolute', bottom: '40px', left: 0, width: '100%',
      display: 'flex', justifyContent: 'center', alignItems: 'center',
      gap: '40px', zIndex: 3
    });

    // 翻转摄像头按钮
    const flipBtn = _createElement('div', {}, {
      width: '50px', height: '50px', borderRadius: '50%',
      background: 'rgba(255,255,255,0.2)', display: 'flex',
      alignItems: 'center', justifyContent: 'center',
      cursor: 'pointer', fontSize: '24px', color: '#fff',
      position: 'absolute', bottom: '130px', right: '20px'
    });
    flipBtn.textContent = '🔄';
    flipBtn.onclick = () => _flipCamera();
    _overlay.appendChild(flipBtn);

    // 按住录制按钮（居中，最大）
    const recordBtn = _createElement('div', { id: 'vcRecordBtn' }, {
      width: '72px', height: '72px', borderRadius: '50%',
      background: 'rgba(255,255,255,0.15)', border: '3px solid rgba(255,255,255,0.6)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      cursor: 'pointer', flexDirection: 'column', userSelect: 'none',
      WebkitUserSelect: 'none', touchAction: 'none', transition: 'all 0.15s'
    });
    recordBtn.innerHTML = '<span style="font-size:28px;line-height:1">🎙</span><span style="font-size:10px;color:#fff;margin-top:2px">按住录制</span>';
    bottomBar.appendChild(recordBtn);

    // 挂断按钮
    const hangupBtn = _createElement('div', {}, {
      display: 'flex', flexDirection: 'column', alignItems: 'center', cursor: 'pointer'
    });
    const hangupImg = _createElement('img', { src: '/public/挂断.png' }, {
      width: '64px', height: '64px'
    });
    const hangupLabel = _createElement('div', { textContent: '挂断' }, {
      color: '#fff', fontSize: '13px', marginTop: '6px'
    });
    hangupBtn.appendChild(hangupImg);
    hangupBtn.appendChild(hangupLabel);
    hangupBtn.onclick = () => _hangup();
    bottomBar.appendChild(hangupBtn);
    _overlay.appendChild(bottomBar);

    // 录制浮层（录制中显示计时器 + 垃圾桶取消区）
    const recordOverlay = _createElement('div', { id: 'vcRecordOverlay' }, {
      position: 'absolute', top: 0, left: 0, width: '100%', height: '100%',
      zIndex: 4, display: 'none', flexDirection: 'column',
      alignItems: 'center', justifyContent: 'flex-end',
      background: 'rgba(0,0,0,0.3)', pointerEvents: 'none'
    });
    // 计时器
    const recTimer = _createElement('div', { id: 'vcRecTimer' }, {
      color: '#fff', fontSize: '18px', fontWeight: '500',
      background: 'rgba(220,50,50,0.7)', padding: '4px 16px',
      borderRadius: '16px', position: 'absolute', top: '60px',
      display: 'flex', alignItems: 'center', gap: '6px'
    });
    recTimer.innerHTML = '<span style="width:8px;height:8px;border-radius:50%;background:#ff4444;animation:vcRecDot 1s infinite"></span> 0:00';
    recordOverlay.appendChild(recTimer);
    // 上滑取消提示
    const cancelHint = _createElement('div', { id: 'vcCancelHint' }, {
      color: 'rgba(255,255,255,0.7)', fontSize: '13px',
      marginBottom: '140px', transition: 'all 0.2s'
    });
    cancelHint.textContent = '↑ 上滑取消';
    recordOverlay.appendChild(cancelHint);
    // 垃圾桶取消区
    const trashZone = _createElement('div', { id: 'vcTrashZone' }, {
      position: 'absolute', top: 0, left: 0, width: '100%', height: '35%',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      fontSize: '40px', opacity: 0, transition: 'opacity 0.2s'
    });
    trashZone.textContent = '🗑';
    recordOverlay.appendChild(trashZone);
    _overlay.appendChild(recordOverlay);

    // CSS 动画
    if (!document.getElementById('vcRecStyles')) {
      const s = document.createElement('style');
      s.id = 'vcRecStyles';
      s.textContent = `
        @keyframes vcRecDot { 0%,100%{opacity:1} 50%{opacity:0.3} }
        #vcRecordBtn.recording { background: rgba(220,50,50,0.6) !important; border-color: #ff4444 !important; transform: scale(1.1); }
        #vcRecordBtn.disabled { opacity: 0.4; pointer-events: none; }
        #vcTrashZone.active { opacity: 1 !important; background: rgba(220,50,50,0.3); }
        #vcCancelHint.in-trash { color: #ff4444 !important; font-weight: 600; }
      `;
      document.head.appendChild(s);
    }

    document.body.appendChild(_overlay);

    // 启动摄像头
    await _startCamera();
    // 不在这里启动语音侦听，由调用方决定时机
  }

  // ── 画面互换 ──
  function _toggleSwap() {
    _swapped = !_swapped;
    const aiImg = document.getElementById('vcAiPhoto');
    const pipAi = document.getElementById('vcPipAi');

    if (_useNativeCamera) {
      // 原生摄像头模式：切换 <img> 元素显示位置
      const userImg = document.getElementById('vcUserImg');
      const mainImg = document.getElementById('vcMainImg');
      if (!aiImg) return;
      if (_swapped) {
        aiImg.style.display = 'none';
        if (mainImg) { mainImg.style.display = 'block'; mainImg.style.transform = _facingMode === 'user' ? 'scaleX(-1)' : 'none'; }
        if (userImg) userImg.style.display = 'none';
        if (pipAi) pipAi.style.display = 'block';
      } else {
        aiImg.style.display = 'block';
        if (mainImg) { mainImg.style.display = 'none'; mainImg.src = ''; }
        if (userImg) { userImg.style.display = 'block'; userImg.style.transform = _facingMode === 'user' ? 'scaleX(-1)' : 'none'; }
        if (pipAi) pipAi.style.display = 'none';
      }
      return;
    }

    // getUserMedia 模式
    const userVideo = document.getElementById('vcUserVideo');
    const mainVideo = document.getElementById('vcMainVideo');
    if (!aiImg || !userVideo) return;

    if (_swapped) {
      aiImg.style.display = 'none';
      mainVideo.style.display = 'block';
      mainVideo.srcObject = _cameraStream;
      mainVideo.style.transform = _facingMode === 'user' ? 'scaleX(-1)' : 'none';
      mainVideo.play().catch(() => {});
      userVideo.style.display = 'none';
      pipAi.style.display = 'block';
    } else {
      aiImg.style.display = 'block';
      mainVideo.style.display = 'none';
      mainVideo.srcObject = null;
      userVideo.style.display = 'block';
      pipAi.style.display = 'none';
    }
  }

  // ── 摄像头管理 ──
  async function _startCamera() {
    // 1) 先尝试 getUserMedia
    try {
      _cameraStream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: _facingMode, width: { ideal: 640 }, height: { ideal: 480 } },
        audio: false
      });
      const userVideo = document.getElementById('vcUserVideo');
      if (userVideo) {
        userVideo.srcObject = _cameraStream;
        userVideo.style.transform = _facingMode === 'user' ? 'scaleX(-1)' : 'none';
        userVideo.play().catch(() => {});
      }
      console.log('[VideoCall] Camera started with getUserMedia');
      return;
    } catch (e) {
      console.warn('[VideoCall] getUserMedia failed:', e);
    }

    // 2) 回退到原生 CameraBridge
    if (window.AionCamera) {
      const facing = _facingMode === 'user' ? 'user' : 'environment';
      const ok = window.AionCamera.start(facing);
      if (ok) {
        _useNativeCamera = true;
        // 隐藏 <video>，显示 <img>
        const vid = document.getElementById('vcUserVideo');
        const img = document.getElementById('vcUserImg');
        if (vid) vid.style.display = 'none';
        if (img) {
          img.style.display = 'block';
          img.style.transform = _facingMode === 'user' ? 'scaleX(-1)' : 'none';
        }
        // 启动 requestAnimationFrame 轮询
        _pollNativeFrame();
        console.log('[VideoCall] Camera started with native bridge');
        return;
      }
    }

    console.warn('[VideoCall] No camera available (getUserMedia + native bridge both failed)');
  }

  function _pollNativeFrame() {
    if (!_useNativeCamera || !window.AionCamera) return;
    const frame = window.AionCamera.getFrame();
    if (frame) {
      const src = 'data:image/jpeg;base64,' + frame;
      if (_swapped) {
        const img = document.getElementById('vcMainImg');
        if (img) img.src = src;
      } else {
        const img = document.getElementById('vcUserImg');
        if (img) img.src = src;
      }
    }
    _nativeCamTimer = requestAnimationFrame(_pollNativeFrame);
  }

  function _stopCamera() {
    if (_nativeCamTimer) { cancelAnimationFrame(_nativeCamTimer); _nativeCamTimer = null; }
    if (_useNativeCamera && window.AionCamera) {
      window.AionCamera.stop();
      _useNativeCamera = false;
    }
    if (_cameraStream) {
      _cameraStream.getTracks().forEach(t => t.stop());
      _cameraStream = null;
    }
  }

  async function _flipCamera() {
    _facingMode = _facingMode === 'environment' ? 'user' : 'environment';
    if (_useNativeCamera && window.AionCamera) {
      window.AionCamera.flip();
      const img = _swapped ? document.getElementById('vcMainImg') : document.getElementById('vcUserImg');
      if (img) img.style.transform = _facingMode === 'user' ? 'scaleX(-1)' : 'none';
    } else {
      _stopCamera();
      await _startCamera();
    }
  }

  // ── 录制按钮交互 ──
  function _initRecordButton() {
    const btn = document.getElementById('vcRecordBtn');
    if (!btn) return;
    let _inTrash = false;
    let _startY = 0;

    function onDown(e) {
      if (_aiSpeaking || _processing || _videoRecording) return;
      e.preventDefault();
      _startY = (e.touches ? e.touches[0].clientY : e.clientY);
      _inTrash = false;
      _startRecord();
    }
    function onMove(e) {
      if (!_videoRecording) return;
      e.preventDefault();
      const y = (e.touches ? e.touches[0].clientY : e.clientY);
      const dy = _startY - y;
      const overlay = document.getElementById('vcRecordOverlay');
      const trash = document.getElementById('vcTrashZone');
      const hint = document.getElementById('vcCancelHint');
      _inTrash = dy > 120;
      if (trash) trash.classList.toggle('active', _inTrash);
      if (hint) hint.classList.toggle('in-trash', _inTrash);
      if (hint) hint.textContent = _inTrash ? '松手取消' : '↑ 上滑取消';
    }
    function onUp(e) {
      if (!_videoRecording) return;
      e.preventDefault();
      if (_inTrash) {
        _cancelRecord();
      } else {
        _stopRecord();
      }
    }

    btn.addEventListener('mousedown', onDown);
    btn.addEventListener('touchstart', onDown, { passive: false });
    document.addEventListener('mousemove', onMove);
    document.addEventListener('touchmove', onMove, { passive: false });
    document.addEventListener('mouseup', onUp);
    document.addEventListener('touchend', onUp);
  }

  // ── 启动音频流（简化版，只为录制服务） ──
  function _startAudioStream() {
    // Android 原生桥接
    if (window.AionAudio) {
      if (window.AionAudio.isRecording()) {
        _useNativeAudio = true;
        _ownNativeBridge = false;
        _sampleRate = 16000;
        return;
      }
      const ok = window.AionAudio.start();
      if (ok) {
        _useNativeAudio = true;
        _ownNativeBridge = true;
        _sampleRate = 16000;
        return;
      }
    }
    // getUserMedia
    navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    }).then(stream => {
      _voiceStream = stream;
      _sampleRate = 48000;
      console.log('[VideoCall] Audio stream ready');
    }).catch(e => {
      console.error('[VideoCall] Microphone unavailable:', e);
    });
  }

  function _stopAudioStream() {
    if (_useNativeAudio && window.AionAudio && _ownNativeBridge) {
      window.AionAudio.stop();
    }
    _useNativeAudio = false;
    _ownNativeBridge = false;
    if (_voiceProcessor) { _voiceProcessor.disconnect(); _voiceProcessor = null; }
    if (_voiceCtx) { _voiceCtx.close().catch(() => {}); _voiceCtx = null; }
    if (_voiceStream) { _voiceStream.getTracks().forEach(t => t.stop()); _voiceStream = null; }
  }

  // ── 视频录制 ──
  function _startRecord() {
    if (_videoRecording) return;
    _videoRecording = true;
    _videoChunks = [];
    _audioChunks = [];
    _nativeAudioFrames = [];
    _recordStartTime = Date.now();

    // UI
    const btn = document.getElementById('vcRecordBtn');
    if (btn) btn.classList.add('recording');
    const overlay = document.getElementById('vcRecordOverlay');
    if (overlay) overlay.style.display = 'flex';
    _updateRecordTimer();
    _recordTimerInterval = setInterval(_updateRecordTimer, 1000);
    _updateStatus('录制中...');

    if (_useNativeCamera && window.AionVideo) {
      // ── Android 原生录制 ──
      // AionVideo 复用 CameraBridge + AudioBridge 的帧
      const w = window.AionCamera ? window.AionCamera.getRotatedWidth() : 480;
      const h = window.AionCamera ? window.AionCamera.getRotatedHeight() : 640;
      const ok = window.AionVideo.startRecord(w, h);
      if (!ok) {
        console.error('[VideoCall] AionVideo.startRecord failed');
        _cancelRecord();
        return;
      }
      console.log(`[VideoCall] Native recording started ${w}x${h}`);
    } else {
      // ── 浏览器 MediaRecorder ──
      try {
        // 合并视频流 + 音频流
        const tracks = [];
        if (_cameraStream) tracks.push(..._cameraStream.getVideoTracks());
        if (_voiceStream) tracks.push(..._voiceStream.getAudioTracks());

        if (tracks.length === 0) {
          console.error('[VideoCall] No tracks for recording');
          _cancelRecord();
          return;
        }

        const combined = new MediaStream(tracks);
        _videoRecorder = new MediaRecorder(combined, {
          mimeType: MediaRecorder.isTypeSupported('video/webm;codecs=vp9,opus')
            ? 'video/webm;codecs=vp9,opus'
            : 'video/webm'
        });
        _videoRecorder.ondataavailable = (e) => { if (e.data.size > 0) _videoChunks.push(e.data); };
        _videoRecorder.start(500);

        // 同时录纯音频（用于 ASR）
        if (_voiceStream && _voiceStream.getAudioTracks().length > 0) {
          const audioStream = new MediaStream(_voiceStream.getAudioTracks());
          _audioForASR = new MediaRecorder(audioStream, {
            mimeType: MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
              ? 'audio/webm;codecs=opus' : 'audio/webm'
          });
          _audioForASR.ondataavailable = (e) => { if (e.data.size > 0) _audioChunks.push(e.data); };
          _audioForASR.start(500);
        }

        console.log('[VideoCall] Browser recording started');
      } catch (e) {
        console.error('[VideoCall] MediaRecorder error:', e);
        _cancelRecord();
        return;
      }
    }

    // 最长录制时间限制
    setTimeout(() => {
      if (_videoRecording) {
        console.log('[VideoCall] Max record time reached');
        _stopRecord();
      }
    }, MAX_RECORD_SECONDS * 1000);
  }

  async function _stopRecord() {
    if (!_videoRecording) return;
    _videoRecording = false;
    _clearRecordUI();

    const duration = (Date.now() - _recordStartTime) / 1000;
    if (duration < 0.5) {
      _updateStatus('录制太短');
      setTimeout(() => _updateStatus('等待录制...'), 1500);
      return;
    }

    _processing = true;
    _updateStatus('处理中...');
    _setRecordBtnDisabled(true);

    try {
      let videoBlob, audioBlob;

      if (_useNativeCamera && window.AionVideo) {
        // ── Android：停止录制，获取 MP4 base64 ──
        const b64 = window.AionVideo.stopRecord();
        if (!b64) { _processing = false; _setRecordBtnDisabled(false); return; }
        const bin = atob(b64);
        const arr = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
        videoBlob = new Blob([arr], { type: 'video/mp4' });

        // 用收集的 PCM 帧构建 WAV
        if (_nativeAudioFrames.length > 0) {
          audioBlob = _buildWavFromNativeChunks(_nativeAudioFrames);
          _nativeAudioFrames = [];
        }
      } else {
        // ── 浏览器：停止 MediaRecorder ──
        videoBlob = await _stopMediaRecorder(_videoRecorder, _videoChunks);
        _videoRecorder = null;
        audioBlob = await _stopMediaRecorder(_audioForASR, _audioChunks);
        _audioForASR = null;
      }

      if (!videoBlob || videoBlob.size < 1000) {
        _processing = false;
        _setRecordBtnDisabled(false);
        _updateStatus('等待录制...');
        return;
      }

      // 上传视频
      _updateStatus('上传中...');
      const videoUrl = await _uploadFile(videoBlob, 'video_clip');
      if (!videoUrl) {
        _processing = false;
        _setRecordBtnDisabled(false);
        _updateStatus('上传失败');
        return;
      }

      // ASR 转写
      _updateStatus('识别中...');
      let transcript = '';
      if (audioBlob && audioBlob.size > 100) {
        transcript = await _transcribeAudio(audioBlob);
      }

      // 检查挂断关键词
      const hangupWords = ['再见', '拜拜', '挂断', '结束通话', '挂了'];
      if (transcript && hangupWords.some(kw => transcript.includes(kw))) {
        const att = { type: 'video_clip', url: videoUrl, duration: Math.round(duration), transcript };
        await _sendToChat('', att);
        _hangup();
        return;
      }

      // 发送给模型
      _aiSpeaking = true;
      _updateStatus('AI 思考中...');
      const att = { type: 'video_clip', url: videoUrl, duration: Math.round(duration), transcript };
      await _sendToChat(transcript, att);
    } catch (e) {
      console.error('[VideoCall] Record process error:', e);
      _updateStatus('⚠ 处理出错');
    } finally {
      _processing = false;
      _resetInactivityTimer();
    }
  }

  function _cancelRecord() {
    if (!_videoRecording && !_processing) return;
    _videoRecording = false;
    _clearRecordUI();

    if (_useNativeCamera && window.AionVideo) {
      window.AionVideo.cancel();
    } else {
      if (_videoRecorder && _videoRecorder.state !== 'inactive') _videoRecorder.stop();
      if (_audioForASR && _audioForASR.state !== 'inactive') _audioForASR.stop();
      _videoRecorder = null;
      _audioForASR = null;
    }
    _videoChunks = [];
    _audioChunks = [];
    _nativeAudioFrames = [];
    _updateStatus('已取消');
    setTimeout(() => { if (_active && !_processing) _updateStatus('等待录制...'); }, 1000);
    console.log('[VideoCall] Recording cancelled');
  }

  // ── 录制 UI 辅助 ──
  function _clearRecordUI() {
    const btn = document.getElementById('vcRecordBtn');
    if (btn) btn.classList.remove('recording');
    const overlay = document.getElementById('vcRecordOverlay');
    if (overlay) overlay.style.display = 'none';
    const trash = document.getElementById('vcTrashZone');
    if (trash) trash.classList.remove('active');
    const hint = document.getElementById('vcCancelHint');
    if (hint) { hint.classList.remove('in-trash'); hint.textContent = '↑ 上滑取消'; }
    if (_recordTimerInterval) { clearInterval(_recordTimerInterval); _recordTimerInterval = null; }
  }

  function _updateRecordTimer() {
    const el = document.getElementById('vcRecTimer');
    if (!el) return;
    const sec = Math.floor((Date.now() - _recordStartTime) / 1000);
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    el.innerHTML = `<span style="width:8px;height:8px;border-radius:50%;background:#ff4444;animation:vcRecDot 1s infinite"></span> ${m}:${String(s).padStart(2, '0')}`;
  }

  function _setRecordBtnDisabled(disabled) {
    const btn = document.getElementById('vcRecordBtn');
    if (btn) btn.classList.toggle('disabled', disabled);
  }

  // ── MediaRecorder 停止并返回 Blob ──
  function _stopMediaRecorder(recorder, chunks) {
    return new Promise(resolve => {
      if (!recorder || recorder.state === 'inactive') {
        resolve(chunks.length > 0 ? new Blob(chunks, { type: chunks[0].type }) : null);
        return;
      }
      recorder.onstop = () => {
        resolve(chunks.length > 0 ? new Blob(chunks, { type: chunks[0].type }) : null);
      };
      recorder.stop();
    });
  }

  // ── 上传文件 ──
  async function _uploadFile(blob, prefix) {
    try {
      const ext = blob.type.includes('mp4') ? 'mp4' : 'webm';
      const form = new FormData();
      form.append('file', blob, `${prefix}_${Date.now()}.${ext}`);
      const resp = await fetch('/api/upload', { method: 'POST', body: form });
      const data = await resp.json();
      return data.url || null;
    } catch (e) {
      console.error('[VideoCall] Upload failed:', e);
      return null;
    }
  }

  // ── ASR 转写 ──
  async function _transcribeAudio(audioBlob) {
    try {
      const form = new FormData();
      form.append('file', audioBlob, 'vc_audio.wav');
      const resp = await fetch('/api/voice/transcribe', { method: 'POST', body: form });
      const data = await resp.json();
      const text = (data.text || '').trim();
      console.log(`[VideoCall] ASR: "${text}"`);
      return text;
    } catch (e) {
      console.error('[VideoCall] Transcribe error:', e);
      return '';
    }
  }

  // ── 从原生 PCM 帧构建 WAV ──
  function _buildWavFromNativeChunks(chunks) {
    const totalSamples = chunks.reduce((s, b64) => s + atob(b64).length / 2, 0);
    const buf = new ArrayBuffer(44 + totalSamples * 2);
    const v = new DataView(buf);
    const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    const sr = 16000;
    w(0, 'RIFF');
    v.setUint32(4, 36 + totalSamples * 2, true);
    w(8, 'WAVE'); w(12, 'fmt ');
    v.setUint32(16, 16, true);
    v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, sr, true); v.setUint32(28, sr * 2, true);
    v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    w(36, 'data');
    v.setUint32(40, totalSamples * 2, true);
    let off = 44;
    for (const b64 of chunks) {
      const bin = atob(b64);
      for (let i = 0; i < bin.length; i++) {
        v.setUint8(off++, bin.charCodeAt(i));
      }
    }
    return new Blob([buf], { type: 'audio/wav' });
  }

  // ── 不活跃超时 ──
  function _resetInactivityTimer() {
    _lastInteractionTime = Date.now();
    if (_inactivityTimer) clearTimeout(_inactivityTimer);
    _inactivityTimer = setTimeout(() => {
      if (_active && !_videoRecording && !_processing && !_aiSpeaking) {
        console.log('[VideoCall] Inactivity timeout, hanging up');
        _hangup();
      }
    }, 120000); // 2 分钟不活跃自动挂断
  }

  // ── Android 原生桥推送音频帧（录制期间收集用于 ASR） ──
  function _onNativeChunk(b64) {
    if (_videoRecording) {
      _nativeAudioFrames.push(b64);
    }
  }

  async function _sendToChat(text, videoAtt) {
    const convId = _convId || currentConvId;
    if (!convId) return;

    // 构建附件
    const attachments = [];
    if (videoAtt) attachments.push(videoAtt);

    // 等待上一条消息发完（最多等 10 秒）
    if (typeof sending !== 'undefined' && sending) {
      let waited = 0;
      while (sending && waited < 10000) {
        await new Promise(r => setTimeout(r, 200));
        waited += 200;
      }
    }

    try {
      const contextLimit = parseInt(document.getElementById('contextSlider')?.value) || 30;
      const body = {
        content: text || '',
        context_limit: contextLimit,
        attachments,
        whisper_mode: false,
        fast_mode: true,
        tts_enabled: true,
        tts_voice: typeof ttsVoiceId !== 'undefined' ? ttsVoiceId : '',
        client_id: typeof _clientId !== 'undefined' ? _clientId : ''
      };

      const res = await fetch(`/api/conversations/${convId}/send`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });

      const reader = res.body.getReader();
      while (true) {
        const { done } = await reader.read();
        if (done) break;
      }
    } catch (e) {
      console.error('[VideoCall] Send failed:', e);
    }
  }

  // ── 通知 AI 说话状态（被 chat.html 的 TTS 系统调用） ──
  function setAiSpeaking(speaking) {
    if (!_active) return;
    _aiSpeaking = speaking;
    if (!speaking) {
      _processing = false;
      _setRecordBtnDisabled(false);
      _updateStatus('等待录制...');
      _resetInactivityTimer();
    } else {
      _setRecordBtnDisabled(true);
      _updateStatus('AI 说话中...');
    }
  }

  // ── 挂断 ──
  function _hangup() {
    const wasActive = _active;
    const callDuration = _callStartTime > 0 ? Math.floor((Date.now() - _callStartTime) / 1000) : 0;
    const convId = _convId || currentConvId;

    _active = false;
    _ringing = false;
    _callStartTime = 0;

    // 取消正在进行的录制
    if (_videoRecording) {
      _videoRecording = false;
      _clearRecordUI();
      if (_useNativeCamera && window.AionVideo) window.AionVideo.cancel();
      if (_videoRecorder && _videoRecorder.state !== 'inactive') _videoRecorder.stop();
      if (_audioForASR && _audioForASR.state !== 'inactive') _audioForASR.stop();
      _videoRecorder = null;
      _audioForASR = null;
    }

    if (_inactivityTimer) { clearTimeout(_inactivityTimer); _inactivityTimer = null; }

    _stopRingbell();
    _stopCamera();
    _stopAudioStream();
    _removeOverlay();

    // 播放挂断音
    const audio = new Audio('/public/挂断音.mp3');
    audio.play().catch(() => {});

    // 通话结束后插入系统消息（仅实际接通过的通话）
    if (wasActive && convId && callDuration > 0) {
      fetch('/api/video-call-sys-msg', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conv_id: convId, duration: callDuration })
      }).catch(e => console.error('[VideoCall] sys msg failed:', e));
    }
  }

  function _removeOverlay() {
    if (_overlay) {
      _overlay.remove();
      _overlay = null;
    }
  }

  // ── 铃声 ──
  function _startRingbell() {
    _ringAudio = new Audio('/public/ringbell.mp3');
    _ringAudio.loop = true;
    _ringAudio.play().catch(() => {});
  }

  function _stopRingbell() {
    if (_ringAudio) {
      _ringAudio.pause();
      _ringAudio.currentTime = 0;
      _ringAudio = null;
    }
  }

  // ── 切断当前 TTS 播放 ──
  function _stopCurrentTTS() {
    try {
      if (typeof ttsAudio !== 'undefined') {
        ttsAudio.pause();
        ttsAudio.src = '';
      }
      if (typeof ttsChunkQueues !== 'undefined') {
        // 清空所有等待中的 TTS 分段
        for (const k of Object.keys(ttsChunkQueues)) delete ttsChunkQueues[k];
      }
      if (typeof ttsPlayOrder !== 'undefined') ttsPlayOrder.length = 0;
      if (typeof ttsPlaying !== 'undefined') ttsPlaying = false;
    } catch(e) {
      console.warn('[VideoCall] stopCurrentTTS error:', e);
    }
  }

  // ── 状态更新 ──
  function _updateStatus(text) {
    const el = document.getElementById('vcStatus');
    if (el) el.textContent = text;
  }

  // ═══════════════════════════════════════════
  // 公开 API
  // ═══════════════════════════════════════════

  /**
   * 用户主动发起视频通话
   */
  function userInitiate() {
    if (_active || _ringing) return;
    _convId = currentConvId;

    // 插入「你拨打了视频电话」系统消息
    if (_convId) {
      fetch('/api/video-call-init-sys-msg', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conv_id: _convId })
      }).catch(e => console.error('[VideoCall] init sys msg failed:', e));
    }

    // 显示来电界面（模拟 AI 接听等待 3 秒）
    _ringing = true;
    _startRingbell();
    _showIncomingUI(
      // 接听（用户发起时不需要，3秒后自动进入，但仍然显示按钮）
      () => {
        _ringing = false;
        _stopRingbell();
        _enterCall(true);
      },
      // 挂断
      () => {
        _ringing = false;
        _stopRingbell();
        _hangup();
      }
    );

    // 3 秒后自动进入通话
    setTimeout(() => {
      if (_ringing) {
        _ringing = false;
        _stopRingbell();
        _enterCall(true);
      }
    }, 3000);
  }

  /**
   * AI 发起视频通话（通过 WS video_call_ring 触发）
   */
  function aiInitiate(data) {
    if (_active || _ringing) return;
    _convId = data.conv_id || currentConvId;
    _ringing = true;
    _ringStartTime = Date.now();

    // 掐断正在播放的 TTS 语音（避免铃声和 TTS 重叠）
    _stopCurrentTTS();

    // 开始循环播放铃声
    _startRingbell();

    // 显示来电界面
    _showIncomingUI(
      // 接听
      () => {
        const elapsed = Date.now() - _ringStartTime;
        _ringing = false;
        _stopRingbell();
        _enterCall(elapsed < 5000);
      },
      // 挂断
      () => {
        _ringing = false;
        _hangup();
      }
    );
  }

  /**
   * 进入通话
   * @param {boolean} fast - true: <5s 接起（接电话1.mp3），false: ≥5s（接电话2.mp3）
   */
  async function _enterCall(fast) {
    // 掉断正在播放的 TTS 语音
    _stopCurrentTTS();

    // 播放接听音效
    const pickupAudio = new Audio(fast ? '/public/接电话1.mp3' : '/public/接电话2.mp3');
    pickupAudio.play().catch(() => {});

    // 显示通话界面（摄像头启动，但不启动语音侦听）
    await _showCallUI('视频通话连接中...');

    // 等待接听音效播放完毕（至少 3 秒）
    await new Promise(resolve => {
      const minDelay = new Promise(r => setTimeout(r, 3000));
      const audioEnd = new Promise(r => {
        pickupAudio.onended = r;
        pickupAudio.onerror = r;
        // 安全超时：如果音频加载失败或时长超过 8 秒
        setTimeout(r, 8000);
      });
      Promise.all([minDelay, audioEnd]).then(resolve);
    });

    // 记录通话开始时间
    _callStartTime = Date.now();

    // 启动音频流 + 初始化录制按钮
    if (_active) {
      _startAudioStream();
      _initRecordButton();
      _updateStatus('等待录制...');
      _resetInactivityTimer();
    }
  }

  /**
   * SSE 收到 video_call_incoming 时显示指示器（📹 AI 正在发起视频通话...）
   */
  function handleIncomingIndicator(data) {
    // 在消息下方添加指示器
    const msgId = data.msg_id;
    if (!msgId) return;

    // 延迟等待 DOM 渲染
    setTimeout(() => {
      const msgEl = document.getElementById(`m_${msgId}`);
      if (!msgEl) return;
      const existing = document.getElementById('vc_incoming_indicator');
      if (existing) existing.remove();

      const indicator = _createElement('div', { id: 'vc_incoming_indicator' }, {
        display: 'flex', alignItems: 'center', gap: '8px',
        padding: '6px 14px', margin: '6px 0 6px 48px',
        background: 'rgba(76,175,80,0.12)', color: '#388e3c',
        borderRadius: '12px', fontSize: '13px', fontWeight: '500',
        width: 'fit-content'
      });
      indicator.innerHTML = `📹 ${_getAiName()} 正在发起视频通话<span style="margin-left:4px" class="vc-dots">●</span><span class="vc-dots">●</span><span class="vc-dots">●</span>`;

      // 弹跳动画
      if (!document.getElementById('vcDotsStyle')) {
        const s = document.createElement('style');
        s.id = 'vcDotsStyle';
        s.textContent = `
          .vc-dots { animation: vcDotBounce 1.2s ease-in-out infinite; font-size: 10px; }
          .vc-dots:nth-child(2) { animation-delay: 0.2s; }
          .vc-dots:nth-child(3) { animation-delay: 0.4s; }
          @keyframes vcDotBounce { 0%,80%,100% { opacity: 0.3; } 40% { opacity: 1; } }
        `;
        document.head.appendChild(s);
      }

      msgEl.after(indicator);

      // 插入指示器后滚动到底部，确保用户能看到
      if (typeof scrollBottom === 'function') scrollBottom();

      // 5 秒后自动移除（3秒延迟后弹出来电UI时指示器应已消失）
      setTimeout(() => {
        const el = document.getElementById('vc_incoming_indicator');
        if (el) el.remove();
      }, 5000);
    }, 200);
  }

  // ── 暴露给 Android 原生桥接的方法 ──
  const _origChunkHandler = window.onAionAudioChunk;
  window.onAionAudioChunk = function(b64) {
    if (_active) {
      _onNativeChunk(b64);
      // 同时仍然推给 videoCall 的回调（如果需要）
    } else if (_origChunkHandler) {
      _origChunkHandler(b64);
    } else if (typeof remoteVoice !== 'undefined') {
      remoteVoice._onNativeChunk(b64);
    }
  };

  return {
    userInitiate,
    aiInitiate,
    handleIncomingIndicator,
    setAiSpeaking,
    _onNativeChunk,
    get active() { return _active; }
  };
})();
