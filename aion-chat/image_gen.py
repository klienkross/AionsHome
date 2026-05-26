"""
AI 生图模块：支持 SELFIE（带参考图）和 DRAW（纯文本）两种模式
优先使用 DashScope Qwen-Image（国内直连），fallback 到 Gemini
"""

import base64, time
from pathlib import Path

import httpx

from config import get_key, UPLOADS_DIR, PUBLIC_DIR

REFERENCE_IMAGE_PATH = PUBLIC_DIR / "生图锚点.jpg"
GEMINI_MODEL = "gemini-3.1-flash-image-preview"
QWEN_MODEL = "qwen-image-2.0-pro"
DASHSCOPE_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
IMAGE_GEN_TIMEOUT = 120


async def generate_image(prompt: str, is_selfie: bool = False) -> str | None:
    """
    生成图片，保存到 uploads 目录，返回文件名。
    优先走 DashScope Qwen-Image，没有 key 则 fallback Gemini。
    """
    dashscope_key = get_key("dashscope")
    if dashscope_key:
        result = await _generate_qwen(prompt, is_selfie, dashscope_key)
        if result:
            return result
        print("[image_gen] Qwen-Image 失败，尝试 fallback Gemini")

    gemini_key = get_key("gemini")
    if gemini_key:
        return await _generate_gemini(prompt, is_selfie, gemini_key)

    print("[image_gen] 没有可用的生图 API Key（dashscope / gemini）")
    return None


async def _generate_qwen(prompt: str, is_selfie: bool, api_key: str) -> str | None:
    content = []

    if is_selfie and REFERENCE_IMAGE_PATH.exists():
        ref_b64 = base64.b64encode(REFERENCE_IMAGE_PATH.read_bytes()).decode()
        content.append({"image": f"data:image/jpeg;base64,{ref_b64}"})
        print(f"[image_gen] Qwen SELFIE 模式，已附带参考图: {REFERENCE_IMAGE_PATH}")
    elif is_selfie:
        print(f"[image_gen] 参考图不存在: {REFERENCE_IMAGE_PATH}，降级为 DRAW 模式")

    content.append({"text": prompt})

    payload = {
        "model": QWEN_MODEL,
        "input": {
            "messages": [{"role": "user", "content": content}]
        },
        "parameters": {
            "size": "1024*1024",
            "prompt_extend": True,
            "watermark": False,
        },
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    try:
        async with httpx.AsyncClient(timeout=IMAGE_GEN_TIMEOUT) as client:
            print(f"[image_gen] Qwen-Image 开始生图... prompt: {prompt[:80]}")
            resp = await client.post(DASHSCOPE_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            choices = data.get("output", {}).get("choices", [])
            if not choices:
                err = data.get("message", data.get("code", "未知错误"))
                print(f"[image_gen] Qwen-Image 返回空 choices: {err}")
                return None

            parts = choices[0].get("message", {}).get("content", [])
            image_url = None
            for part in parts:
                if "image" in part:
                    image_url = part["image"]
                    break

            if not image_url:
                print("[image_gen] Qwen-Image 响应中未找到图片 URL")
                return None

            # 下载图片保存到本地
            dl_resp = await client.get(image_url)
            dl_resp.raise_for_status()

            filename = f"img_gen_{int(time.time() * 1000)}.png"
            filepath = UPLOADS_DIR / filename
            filepath.write_bytes(dl_resp.content)
            print(f"[image_gen] Qwen-Image 图片已保存: {filepath}")
            return filename

    except httpx.HTTPStatusError as e:
        error_body = e.response.text[:500] if e.response else ""
        print(f"[image_gen] Qwen-Image 请求失败 ({e.response.status_code}): {error_body}")
        return None
    except Exception as e:
        print(f"[image_gen] Qwen-Image 异常: {e}")
        return None


async def _generate_gemini(prompt: str, is_selfie: bool, api_key: str) -> str | None:
    parts = [{"text": prompt}]

    if is_selfie:
        if REFERENCE_IMAGE_PATH.exists():
            ref_b64 = base64.b64encode(REFERENCE_IMAGE_PATH.read_bytes()).decode()
            parts.append({
                "inlineData": {"mimeType": "image/jpeg", "data": ref_b64}
            })
            print(f"[image_gen] Gemini SELFIE 模式，已附带参考图: {REFERENCE_IMAGE_PATH}")
        else:
            print(f"[image_gen] 参考图不存在: {REFERENCE_IMAGE_PATH}，降级为 DRAW 模式")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=IMAGE_GEN_TIMEOUT) as client:
            print(f"[image_gen] Gemini 开始生图... prompt: {prompt[:80]}")
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

            candidates = data.get("candidates", [])
            if not candidates:
                error_msg = data.get("error", {}).get("message", "未知错误")
                print(f"[image_gen] Gemini 返回空 candidates: {error_msg}")
                return None

            content_parts = candidates[0].get("content", {}).get("parts", [])
            image_data = None
            mime_type = "image/png"

            for part in content_parts:
                inline = part.get("inlineData")
                if inline and inline.get("mimeType", "").startswith("image/"):
                    image_data = inline["data"]
                    mime_type = inline["mimeType"]
                    break

            if not image_data:
                print("[image_gen] Gemini 响应中未找到图片数据")
                return None

            ext = "png"
            if "jpeg" in mime_type or "jpg" in mime_type:
                ext = "jpg"
            elif "webp" in mime_type:
                ext = "webp"

            filename = f"img_gen_{int(time.time() * 1000)}.{ext}"
            filepath = UPLOADS_DIR / filename
            filepath.write_bytes(base64.b64decode(image_data))
            print(f"[image_gen] Gemini 图片已保存: {filepath}")
            return filename

    except httpx.HTTPStatusError as e:
        error_body = e.response.text[:500] if e.response else ""
        print(f"[image_gen] Gemini 请求失败 ({e.response.status_code}): {error_body}")
        return None
    except Exception as e:
        print(f"[image_gen] Gemini 生图异常: {e}")
        return None
