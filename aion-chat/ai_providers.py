"""
AI 模型调用：硅基流动 / Gemini 流式 + 多模态消息构建
"""

import asyncio, json, base64, logging, mimetypes
from pathlib import Path

import httpx

logger = logging.getLogger("ai_providers")

MAX_RETRIES = 2
RETRY_DELAY = 1.5

from config import get_key, MODELS, UPLOADS_DIR


# ── 图片转文本（纯文本模型用） ──────────────────
async def convert_images_to_text(history: list):
    """将 history 中的图片附件替换为哨兵 VL 模型生成的文字描述，原地修改。"""
    from sentinel import describe_image_b64
    for m in history:
        atts = m.get("attachments", [])
        if not atts or m["role"] != "user":
            continue
        descs = []
        for att in atts:
            fpath = UPLOADS_DIR / Path(att).name
            if not fpath.exists():
                continue
            mime = mimetypes.guess_type(str(fpath))[0] or ""
            if not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(fpath.read_bytes()).decode()
            desc = await describe_image_b64(b64)
            if desc:
                descs.append(desc)
        if descs:
            desc_text = "\n".join(f"[图片描述] {d}" for d in descs)
            m["content"] = f"{m['content']}\n{desc_text}" if m["content"] else desc_text
            m["attachments"] = []


# ── 多模态消息构建 ────────────────────────────────
def build_multimodal_messages(history: list):
    """将带附件的历史记录转换为 OpenAI 兼容多模态格式"""
    result = []
    for m in history:
        attachments = m.get("attachments", [])
        if isinstance(attachments, str):
            try: attachments = json.loads(attachments) if attachments else []
            except: attachments = []
        if attachments and m["role"] == "user":
            parts = []
            if m["content"]:
                parts.append({"type": "text", "text": m["content"]})
            for att in attachments:
                fpath = UPLOADS_DIR / Path(att).name
                if fpath.exists():
                    mime = mimetypes.guess_type(str(fpath))[0] or "image/jpeg"
                    b64 = base64.b64encode(fpath.read_bytes()).decode()
                    if mime.startswith("image/"):
                        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                    elif mime.startswith("video/"):
                        parts.append({"type": "video_url", "video_url": {"url": f"data:{mime};base64,{b64}"}})
            result.append({"role": m["role"], "content": parts if parts else m["content"]})
        else:
            result.append({"role": m["role"], "content": m["content"]})
    return result


def build_gemini_contents(history: list):
    """将带附件的历史记录转换为 Gemini 格式"""
    contents = []
    for m in history:
        role = "user" if m["role"] == "user" else "model"
        attachments = m.get("attachments", [])
        if isinstance(attachments, str):
            try: attachments = json.loads(attachments) if attachments else []
            except: attachments = []
        parts = []
        if m["content"]:
            parts.append({"text": m["content"]})
        if attachments and m["role"] == "user":
            for att in attachments:
                fpath = UPLOADS_DIR / Path(att).name
                if fpath.exists():
                    mime = mimetypes.guess_type(str(fpath))[0] or "image/jpeg"
                    b64 = base64.b64encode(fpath.read_bytes()).decode()
                    parts.append({"inline_data": {"mime_type": mime, "data": b64}})
        contents.append({"role": role, "parts": parts if parts else [{"text": m["content"]}]})
    return contents


# ── 硅基流动 ──────────────────────────────────────
async def call_siliconflow(messages: list, model: str, meta: dict | None = None, temperature: float | None = None):
    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {"Authorization": f"Bearer {get_key('siliconflow')}", "Content-Type": "application/json"}
    api_messages = build_multimodal_messages(messages)
    payload = {"model": model, "messages": api_messages, "stream": True,
               "stream_options": {"include_usage": True}}
    if temperature is not None:
        payload["temperature"] = temperature
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                try:
                    err = json.loads(body).get("error", {}).get("message", body.decode())
                except:
                    err = body.decode(errors="replace")[:500]
                yield f"[硅基流动错误 {resp.status_code}] {err}"
                return
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                        if meta is not None and "usage" in chunk and chunk["usage"]:
                            u = chunk["usage"]
                            meta["prompt_tokens"] = u.get("prompt_tokens", 0)
                            meta["completion_tokens"] = u.get("completion_tokens", 0)
                            meta["total_tokens"] = u.get("total_tokens", 0)
                            meta["raw"] = u  # 保留原始 usage 数据
                        delta = chunk["choices"][0].get("delta", {}) if chunk.get("choices") else {}
                        if "content" in delta and delta["content"]:
                            yield delta["content"]
                    except:
                        pass


# ── Gemini ────────────────────────────────────────
async def call_gemini(messages: list, model: str, meta: dict | None = None, temperature: float | None = None):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse&key={get_key('gemini')}"
    contents = build_gemini_contents(messages)
    payload = {"contents": contents}
    if temperature is not None:
        payload["generationConfig"] = {"temperature": temperature}
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", url, json=payload) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                try:
                    err = json.loads(body).get("error", {}).get("message", body.decode())
                except:
                    err = body.decode(errors="replace")[:500]
                yield f"[Gemini错误 {resp.status_code}] {err}"
                return
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    try:
                        chunk = json.loads(line[6:])
                        if meta is not None and "usageMetadata" in chunk:
                            u = chunk["usageMetadata"]
                            meta["prompt_tokens"] = u.get("promptTokenCount", 0)
                            meta["completion_tokens"] = u.get("candidatesTokenCount", 0)
                            meta["total_tokens"] = u.get("totalTokenCount", 0)
                            meta["raw"] = u  # 保留原始 usageMetadata 数据
                        text = chunk.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                        if text:
                            yield text
                    except:
                        pass

# ── AiPro 中转站 ────────────────────────────────────────
async def call_aipro(messages: list, model: str, meta: dict | None = None, temperature: float | None = None):
    url = "https://vip.aipro.love/v1/chat/completions"
    headers = {"Authorization": f"Bearer {get_key('aipro')}", "Content-Type": "application/json"}
    api_messages = build_multimodal_messages(messages)
    payload = {"model": model, "messages": api_messages, "stream": True}
    if temperature is not None:
        payload["temperature"] = temperature
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                try:
                    err = json.loads(body).get("error", {}).get("message", body.decode())
                except:
                    err = body.decode(errors="replace")[:500]
                yield f"[中转站错误 {resp.status_code}] {err}"
                return
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                        if meta is not None and "usage" in chunk and chunk["usage"]:
                            u = chunk["usage"]
                            meta["prompt_tokens"] = u.get("prompt_tokens", 0)
                            meta["completion_tokens"] = u.get("completion_tokens", 0)
                            meta["total_tokens"] = u.get("total_tokens", 0)
                            meta["raw"] = u
                        delta = chunk["choices"][0].get("delta", {}) if chunk.get("choices") else {}
                        if "content" in delta and delta["content"]:
                            yield delta["content"]
                    except:
                        pass

# ── 自定义 OpenAI 兼容端点 ────────────────────────
async def call_custom(messages: list, model: str, base_url: str, key_name: str,
                      meta: dict | None = None, temperature: float | None = None):
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {get_key(key_name)}", "Content-Type": "application/json"}
    api_messages = build_multimodal_messages(messages)
    payload = {"model": model, "messages": api_messages, "stream": True}
    if temperature is not None:
        payload["temperature"] = temperature
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                try:
                    err = json.loads(body).get("error", {}).get("message", body.decode())
                except:
                    err = body.decode(errors="replace")[:500]
                yield f"[自定义端点错误 {resp.status_code}] {err}"
                return
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                        if meta is not None and "usage" in chunk and chunk["usage"]:
                            u = chunk["usage"]
                            meta["prompt_tokens"] = u.get("prompt_tokens", 0)
                            meta["completion_tokens"] = u.get("completion_tokens", 0)
                            meta["total_tokens"] = u.get("total_tokens", 0)
                            meta["raw"] = u
                        delta = chunk["choices"][0].get("delta", {}) if chunk.get("choices") else {}
                        if "content" in delta and delta["content"]:
                            yield delta["content"]
                    except:
                        pass


# ── 非流式调用（收集流式输出） ────────────────────
async def simple_ai_call(messages: list, model_key: str, temperature: float | None = None) -> str:
    """收集 stream_ai 的全部 chunk，返回完整文本"""
    full_text = ""
    async for chunk in stream_ai(messages, model_key, temperature=temperature):
        full_text += chunk
    return full_text


# ── 统一调度 ──────────────────────────────────────
def _pick_provider(cfg, normalized, meta, temperature):
    """根据配置返回对应的 provider async generator"""
    p = cfg["provider"]
    if p == "siliconflow":
        return call_siliconflow(normalized, cfg["model"], meta, temperature)
    elif p == "gemini":
        return call_gemini(normalized, cfg["model"], meta, temperature)
    elif p == "aipro":
        return call_aipro(normalized, cfg["model"], meta, temperature)
    elif p == "custom":
        return call_custom(normalized, cfg["model"], cfg.get("base_url", ""),
                           cfg.get("key_name", ""), meta, temperature)
    return None


async def stream_ai(messages: list, model_key: str, meta: dict | None = None, temperature: float | None = None):
    normalized = []
    for m in messages:
        nm = dict(m)
        if nm["role"] in ("cam_user", "cam_trigger"):
            nm["role"] = "user"
        elif nm["role"] == "cam_log":
            nm["role"] = "assistant"
        normalized.append(nm)
    cfg = MODELS.get(model_key)
    if not cfg:
        yield f"[错误] 未知模型: {model_key}"
        return
    if cfg["provider"] == "custom" and not cfg.get("base_url"):
        yield f"[错误] 模型 {model_key} 缺少 base_url 配置"
        return

    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            got_data = False
            gen = _pick_provider(cfg, normalized, meta, temperature)
            if gen is None:
                yield f"[错误] 未知 provider: {cfg['provider']}"
                return
            async for chunk in gen:
                got_data = True
                yield chunk
            return
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                httpx.RemoteProtocolError, httpx.PoolTimeout) as e:
            last_err = e
            if got_data or attempt >= MAX_RETRIES:
                break
            logger.warning(f"stream_ai 连接失败 (attempt {attempt+1}/{MAX_RETRIES+1}): {e}")
            await asyncio.sleep(RETRY_DELAY * (attempt + 1))

    raise last_err if last_err else RuntimeError("stream_ai failed")
