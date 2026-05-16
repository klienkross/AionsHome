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

from config import get_key, SETTINGS

# ── 默认值（可通过前端设置覆盖）────────────────────
# 上游 736d862 的 sentinel/embedding 可配置化在此实现；未来合并可跳过上游对应部分。
_DEFAULT_SENTINEL_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_DEFAULT_SENTINEL_MODEL = "qwen-flash"
_DEFAULT_SENTINEL_VL_MODEL = "qwen3-vl-flash"
_DEFAULT_EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_DIMS = 1024


def _sentinel_base() -> str:
    return (SETTINGS.get("sentinel_base_url") or "").strip() or _DEFAULT_SENTINEL_BASE


def _sentinel_model() -> str:
    return (SETTINGS.get("sentinel_model") or "").strip() or _DEFAULT_SENTINEL_MODEL


def _sentinel_vl_model() -> str:
    return (SETTINGS.get("sentinel_vl_model") or "").strip() or _DEFAULT_SENTINEL_VL_MODEL


def _sentinel_key() -> str:
    return (SETTINGS.get("sentinel_api_key") or "").strip() or get_key("dashscope")


def _embedding_base() -> str:
    return (SETTINGS.get("embedding_base_url") or "").strip() or _DEFAULT_SENTINEL_BASE


def _embedding_model() -> str:
    return (SETTINGS.get("embedding_model") or "").strip() or _DEFAULT_EMBEDDING_MODEL


def _embedding_key() -> str:
    return (SETTINGS.get("embedding_api_key") or "").strip() or get_key("dashscope")

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
    """统一走 OpenAI 兼容 chat/completions；返回 assistant 文本或 None。"""
    key = _sentinel_key()
    if not key:
        return None

    url = f"{_sentinel_base()}/chat/completions"
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
    model: str | None = None,
    image_b64: str | None = None,
) -> dict | None:
    """调用哨兵，返回 JSON dict；失败返回 None。
    若传入 image_b64，则自动切到视觉模型。
    """
    if model is None:
        model = _sentinel_model()
    if image_b64:
        if model == _sentinel_model():
            model = _sentinel_vl_model()
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
    model: str | None = None,
    system: str | None = None,
) -> str | None:
    """调用哨兵，返回纯文本；失败返回 None。
    prompt 可以是字符串或完整 messages 数组。
    """
    if model is None:
        model = _sentinel_model()
    if isinstance(prompt, list):
        messages = prompt
    else:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

    text = await _chat(messages, model, timeout, max_retries, json_mode=False)
    return text.strip() if text else None


# ── 对外：图片描述（VL 哨兵） ──────────────────
async def describe_image_b64(
    image_b64: str,
    *,
    timeout: int = 30,
    max_retries: int = 1,
) -> str | None:
    """用视觉模型描述一张 base64 图片，返回纯文本描述。"""
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "请简要描述这张图片的内容，包括场景、人物动作和关键细节。用中文回答，2-3句话即可。"},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ],
    }]
    text = await _chat(messages, _sentinel_vl_model(), timeout, max_retries, json_mode=False)
    return text.strip() if text else None


# ── 对外：向量 ─────────────────────────────────
async def get_embedding(text: str) -> list[float] | None:
    """向量模型调用，支持前端配置覆盖。"""
    key = _embedding_key()
    if not key:
        return None
    url = f"{_embedding_base()}/embeddings"
    headers = {"Authorization": f"Bearer {key}"}
    body = {
        "model": _embedding_model(),
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


# ── 对外：重排序 ─────────────────────────────────
_RERANK_API = "https://api.siliconflow.cn/v1/rerank"
_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


async def fetch_rerank(query: str, documents: list[str], top_n: int = 10) -> list[dict]:
    """SiliconFlow BGE-reranker-v2-m3，返回 [{'index': int, 'relevance_score': float}, ...] 按分降序"""
    from config import get_key
    key = get_key("siliconflow")
    if not key:
        return []
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {"model": _RERANK_MODEL, "query": query, "documents": documents, "top_n": top_n}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(_RERANK_API, headers=headers, json=body)
            resp.raise_for_status()
            return resp.json().get("results", [])
    except Exception:
        return []
