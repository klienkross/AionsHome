"""
阿里云百炼（DashScope）OpenAI 兼容端点：统一哨兵（JSON / 纯文本 / 视觉）与向量模型。

对外：
  - call_sentinel(prompt, ..., image_b64=None)  → dict | None   （JSON mode）
  - call_sentinel_text(prompt_or_messages, ...) → str  | None   （纯文本，可选 system）
  - get_embedding(text)                         → list[float] | None
  - _pack_embedding / _unpack_embedding（供 memory 模块复用）
"""

import asyncio, json, time, struct
import httpx

from config import get_key

# ── 常量 ──────────────────────────────────────────
DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
SENTINEL_MODEL = "qwen-flash"
SENTINEL_VL_MODEL = "qwen3-vl-flash"
EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_DIMS = 1024

_MIN_INTERVAL = 0.3  # 全局软节流；DashScope qwen-flash 共享 1200 RPM，0.3s 足够留边
_LAST_CALL = 0.0


# ── 向量打包 ──────────────────────────────────────
def _pack_embedding(values: list[float]) -> bytes:
    return struct.pack(f'{len(values)}f', *values)


def _unpack_embedding(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f'{n}f', blob))


# ── 内部：节流与 chat 调用 ───────────────────────
async def _throttle():
    global _LAST_CALL
    elapsed = time.time() - _LAST_CALL
    if elapsed < _MIN_INTERVAL:
        await asyncio.sleep(_MIN_INTERVAL - elapsed)
    _LAST_CALL = time.time()


async def _chat(messages: list, model: str, timeout: int, max_retries: int,
                json_mode: bool) -> str | None:
    """统一走 DashScope OpenAI 兼容 chat/completions；返回 assistant 文本或 None。"""
    key = get_key("dashscope")
    if not key:
        return None

    url = f"{DASHSCOPE_BASE}/chat/completions"
    headers = {"Authorization": f"Bearer {key}"}
    body = {"model": model, "messages": messages}
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    for attempt in range(max_retries + 1):
        await _throttle()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, json=body)
            if resp.status_code in (429, 503):
                if attempt < max_retries:
                    await asyncio.sleep(4 * (2 ** attempt))
                    continue
                return None
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError):
            if attempt < max_retries:
                await asyncio.sleep(4 * (2 ** attempt))
                continue
            return None
        except Exception:
            return None
    return None


# ── 对外：JSON 哨兵（可选图像） ─────────────────
async def call_sentinel(
    prompt: str,
    *,
    timeout: int = 30,
    max_retries: int = 2,
    model: str = SENTINEL_MODEL,
    image_b64: str | None = None,
) -> dict | None:
    """调用哨兵，返回 JSON dict；失败返回 None。
    若传入 image_b64，则自动切到视觉模型（qwen3-vl-flash）。
    """
    if image_b64:
        if model == SENTINEL_MODEL:
            model = SENTINEL_VL_MODEL
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ],
        }]
    else:
        messages = [{"role": "user", "content": prompt}]

    raw = await _chat(messages, model, timeout, max_retries, json_mode=True)
    if not raw:
        return None

    raw = raw.strip()
    # 兜底：如果模型包了 ```json ... ``` 仍尝试提取
    if "```" in raw or not raw.startswith("{"):
        s = raw.find("{")
        e = raw.rfind("}") + 1
        if s >= 0 and e > s:
            raw = raw[s:e]

    try:
        return json.loads(raw)
    except Exception:
        return None


# ── 对外：纯文本哨兵（允许 system prompt） ────
async def call_sentinel_text(
    prompt: str | list[dict],
    *,
    timeout: int = 30,
    max_retries: int = 2,
    model: str = SENTINEL_MODEL,
    system: str | None = None,
) -> str | None:
    """调用哨兵，返回纯文本；失败返回 None。
    prompt 可以是字符串或完整 messages 数组。
    """
    if isinstance(prompt, list):
        messages = prompt
    else:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

    text = await _chat(messages, model, timeout, max_retries, json_mode=False)
    return text.strip() if text else None


# ── 对外：向量 ─────────────────────────────────
async def get_embedding(text: str) -> list[float] | None:
    """DashScope text-embedding-v4，固定 1024 维。"""
    key = get_key("dashscope")
    if not key:
        return None
    url = f"{DASHSCOPE_BASE}/embeddings"
    headers = {"Authorization": f"Bearer {key}"}
    body = {
        "model": EMBEDDING_MODEL,
        "input": text,
        "dimensions": EMBEDDING_DIMS,
        "encoding_format": "float",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
    except Exception:
        return None
