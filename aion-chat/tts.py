"""
服务端流式 TTS 模块
- 按句子边界切分 AI 回复文本
- 异步并行调用硅基流动 TTS 合成
- 通过 WebSocket 推送音频 URL 给前端顺序播放
"""

import re, asyncio, logging
import httpx

from config import get_key, TTS_CACHE_DIR

log = logging.getLogger("tts")

# 需要从 TTS 文本中剥除的特殊标签
_STRIP_PATTERNS = [
    re.compile(r'\[CAM_CHECK\]'),
    re.compile(r'\[POI_SEARCH:[^\]]*\]'),
    re.compile(r'\[MUSIC:[^\]]*\]'),
    re.compile(r'\[ALARM:[^\]]*\]'),
    re.compile(r'\[REMINDER:[^\]]*\]'),
    re.compile(r'\[Monitor:[^\]]*\]'),
    re.compile(r'\[SCHEDULE_DEL:[^\]]*\]'),
    re.compile(r'\[SCHEDULE_LIST\]'),
    re.compile(r'\[TOY:[^\]]*\]'),
    re.compile(r'\[HEART:[^\]]*\]'),
    re.compile(r'\[MEMORY:[^\]]*\]'),
    re.compile(r'\[查看动态:\d+\]'),
    re.compile(r'<meta>[\s\S]*?</meta>'),
]

# 句子结束符（用于切分）
_SENTENCE_ENDS = set('。！？…!?')
_COMMA_CHARS = set('，,、；;：:')

def _strip_tags(text: str) -> str:
    """去除所有特殊标签，只保留纯文本"""
    for p in _STRIP_PATTERNS:
        text = p.sub('', text)
    return text.strip()


def _has_unclosed_tag(text: str) -> bool:
    """检查是否有未闭合的 [...] 或 <meta>"""
    # 检查 [TAG:... 没有闭合的 ]
    last_open = text.rfind('[')
    if last_open >= 0 and ']' not in text[last_open:]:
        return True
    # 检查 <meta> 没有闭合的 </meta>
    meta_opens = text.count('<meta>')
    meta_closes = text.count('</meta>')
    if meta_opens > meta_closes:
        return True
    return False


class TTSStreamer:
    """服务端流式 TTS：积累文本 → 按句子切分 → 异步合成 → WS 推送"""

    def __init__(self, msg_id: str, voice: str, ws_manager):
        self.msg_id = msg_id
        self.voice = voice
        self._ws = ws_manager
        self._buffer = ""       # 原始文本缓冲
        self._seq = 0           # 分段序号
        self._tasks: list[asyncio.Task] = []

    def feed(self, chunk: str):
        """喂入 AI 流式 chunk，检测到可切分的句子就异步发起合成"""
        self._buffer += chunk
        self._try_split()

    def _try_split(self):
        """尝试从 buffer 中切出完整句子送去合成"""
        while True:
            # 有未闭合的标签，先不切
            if _has_unclosed_tag(self._buffer):
                break

            # 先清除标签，计算纯文本长度
            clean = _strip_tags(self._buffer)
            if len(clean) < 100:
                break

            # 从第100个纯文字对应的原始位置开始找切分点
            cut_pos = self._find_cut_position()
            if cut_pos is None:
                break

            segment = self._buffer[:cut_pos + 1]
            self._buffer = self._buffer[cut_pos + 1:]

            cleaned = _strip_tags(segment)
            if cleaned.strip():
                self._dispatch(cleaned.strip())

    def _find_cut_position(self) -> int | None:
        """
        在原始 buffer 中找到切分位置。
        逻辑：纯文本到达 100 字后，开始找句号；最远到 200 字，找逗号；200 字还没有就强切。
        返回原始 buffer 中的切分索引。
        """
        clean_count = 0     # 已累积的纯文字数
        in_bracket = False   # 在 [...] 内
        in_meta = False      # 在 <meta>...</meta> 内
        best_sentence_cut = None
        best_comma_cut = None

        i = 0
        while i < len(self._buffer):
            ch = self._buffer[i]

            # 跟踪标签状态
            if ch == '[' and not in_meta:
                in_bracket = True
            elif ch == ']' and in_bracket:
                in_bracket = False
                i += 1
                continue
            elif self._buffer[i:i+6] == '<meta>':
                in_meta = True
                i += 6
                continue
            elif self._buffer[i:i+7] == '</meta>':
                in_meta = False
                i += 7
                continue

            if in_bracket or in_meta:
                i += 1
                continue

            # 计数纯文字
            clean_count += 1

            if clean_count >= 100:
                if ch in _SENTENCE_ENDS:
                    # 找到句子结束符，检查省略号（…或...连续的情况）
                    best_sentence_cut = i
                    # 立即用这个切分点
                    return best_sentence_cut
                if ch in _COMMA_CHARS:
                    best_comma_cut = i

            if clean_count >= 200:
                # 到达上限，优先逗号，否则强切当前位置
                if best_comma_cut is not None:
                    return best_comma_cut
                return i

            i += 1

        return None

    def _dispatch(self, text: str):
        """发起异步合成任务"""
        seq = self._seq
        self._seq += 1
        safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '', self.msg_id)
        task = asyncio.create_task(self._synthesize(text, seq, safe_id))
        self._tasks.append(task)

    async def flush(self):
        """流结束后，处理 buffer 中剩余文本并等待所有合成任务完成"""
        remaining = _strip_tags(self._buffer).strip()
        if remaining:
            self._dispatch(remaining)
        self._buffer = ""

        # 等待所有合成任务完成
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        # 通知前端该消息的 TTS 分段已全部推送完毕
        await self._ws.broadcast({
            "type": "tts_done",
            "data": {"msg_id": self.msg_id}
        })

    async def _synthesize(self, text: str, seq: int, safe_id: str):
        """调用硅基流动 TTS 合成 → 保存文件 → WS 推送"""
        key = get_key("siliconflow")
        if not key:
            log.warning("TTS: 无硅基流动 API Key，跳过合成 seq=%d", seq)
            return

        chunk_name = f"{safe_id}_s{seq}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.siliconflow.cn/v1/audio/speech",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": "FunAudioLLM/CosyVoice2-0.5B",
                        "input": text,
                        "voice": self.voice,
                        "response_format": "mp3",
                        "speed": 1.0,
                        "gain": 0
                    }
                )
            if resp.status_code != 200:
                log.warning("TTS API 错误: status=%d seq=%d", resp.status_code, seq)
                return

            cache_path = TTS_CACHE_DIR / f"{chunk_name}.mp3"
            cache_path.write_bytes(resp.content)

            await self._ws.broadcast({
                "type": "tts_chunk",
                "data": {
                    "msg_id": self.msg_id,
                    "seq": seq,
                    "url": f"/api/tts/audio/{chunk_name}"
                }
            })
            log.info("TTS chunk pushed: msg=%s seq=%d len=%d", self.msg_id, seq, len(text))

        except Exception as e:
            log.error("TTS 合成失败 seq=%d: %s", seq, e)
