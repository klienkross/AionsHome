"""测试 Xiaomi MiMo TTS V2.5 语音合成

API 文档: https://platform.xiaomimimo.com/docs/zh-CN/usage-guide/speech-synthesis-v2.5

使用方式:
  1. 设置环境变量: $env:MIMO_API_KEY = "your-api-key"
  2. 运行: python test_mimo_tts.py

三种模式:
  - preset: 预置音色（冰糖/茉莉/苏打/白桦/Mia/Chloe/Milo/Dean）
  - voicedesign: 文本描述设计音色
  - voiceclone: 上传音频样本复刻音色（暂未实现，需要音频文件）
"""

import base64
import sys
import httpx

from config import get_key

BASE_URL = "https://api.xiaomimimo.com/v1"
API_KEY = get_key("mimo")

# ---- 预置音色 ----
PRESET_VOICES = {
    # 中文
    "bingtang": "冰糖",     # 女
    "moli": "茉莉",         # 女
    "soda": "苏打",         # 男
    "baihua": "白桦",       # 男
    # 英文
    "mia": "Mia",           # 女
    "chloe": "Chloe",       # 女
    "milo": "Milo",         # 男
    "dean": "Dean",         # 男
}


def tts_preset(
    text: str,
    voice: str = "冰糖",
    style_instruction: str = "",
    output_path: str = "output.wav",
) -> None:
    """使用预置音色合成语音 (mimo-v2.5-tts)"""
    messages = []
    if style_instruction:
        messages.append({"role": "user", "content": style_instruction})
    messages.append({"role": "assistant", "content": text})

    payload = {
        "model": "mimo-v2.5-tts",
        "messages": messages,
        "audio": {"format": "wav", "voice": voice},
    }

    _call_api(payload, output_path)


def tts_voicedesign(
    voice_description: str,
    text: str,
    output_path: str = "output.wav",
) -> None:
    """通过文本描述设计音色合成语音 (mimo-v2.5-tts-voicedesign)"""
    payload = {
        "model": "mimo-v2.5-tts-voicedesign",
        "messages": [
            {"role": "user", "content": voice_description},
            {"role": "assistant", "content": text},
        ],
        "audio": {"format": "wav"},
    }

    _call_api(payload, output_path)


def tts_clone(
    audio_path: str,
    text: str,
    style_instruction: str = "",
    output_path: str = "output.wav",
) -> None:
    """使用音频样本复刻音色合成语音 (mimo-v2.5-tts-voiceclone)"""
    # 读取音频并 base64 编码
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    # 根据扩展名判断 MIME
    ext = os.path.splitext(audio_path)[1].lower()
    mime_map = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4"}
    mime = mime_map.get(ext, "audio/mpeg")

    voice_data = f"data:{mime};base64,{base64.b64encode(audio_bytes).decode('utf-8')}"
    print(f"音频样本编码完成: {len(audio_bytes)} bytes -> base64")

    messages = []
    if style_instruction:
        messages.append({"role": "user", "content": style_instruction})
    messages.append({"role": "assistant", "content": text})

    payload = {
        "model": "mimo-v2.5-tts-voiceclone",
        "messages": messages,
        "audio": {"format": "wav", "voice": voice_data},
    }

    _call_api(payload, output_path)


def _call_api(payload: dict, output_path: str) -> None:
    if not API_KEY:
        print("错误: 请设置 MIMO_API_KEY 环境变量")
        print("  PowerShell: $env:MIMO_API_KEY = 'your-key'")
        sys.exit(1)

    print(f"调用模型: {payload['model']}")
    print(f"消息数量: {len(payload['messages'])}")
    print("请求中...")

    resp = httpx.post(
        f"{BASE_URL}/chat/completions",
        headers={
            "api-key": API_KEY,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120.0,
    )

    if resp.status_code != 200:
        print(f"API 错误 ({resp.status_code}): {resp.text}")
        sys.exit(1)

    data = resp.json()
    message = data["choices"][0]["message"]
    audio_base64 = message["audio"]["data"]

    # 解码并保存
    audio_bytes = base64.b64decode(audio_base64)
    with open(output_path, "wb") as f:
        f.write(audio_bytes)

    print(f"音频已保存到: {output_path} ({len(audio_bytes)} bytes)")

    # 尝试播放
    _play_audio(output_path)


def _play_audio(path: str) -> None:
    try:
        import sounddevice as sd
        import soundfile as sf
        data, samplerate = sf.read(path)
        sd.play(data, samplerate)
        sd.wait()
        print("播放完成")
    except ImportError:
        print("(未安装 soundfile，跳过播放)")
    except Exception as e:
        print(f"播放失败: {e}")


# ---- 交互式菜单 ----
def interactive():
    print("=" * 50)
    print("  Xiaomi MiMo TTS V2.5 测试工具")
    print("=" * 50)

    print("\n选择模式:")
    print("  1. 预置音色 (preset)")
    print("  2. 文本设计音色 (voicedesign)")
    print("  3. 音色复刻 (voiceclone)")
    choice = input("\n请输入 [1/2/3]: ").strip()

    if choice == "1":
        print("\n预置音色列表:")
        for i, (k, v) in enumerate(PRESET_VOICES.items(), 1):
            print(f"  {i}. {v} ({k})")
        v = input("\n音色名（直接回车默认'冰糖'）: ").strip()
        voice = v if v else "冰糖"

        text = input("合成文本: ").strip()
        if not text:
            text = "你好！欢迎使用小米MiMo语音合成服务，今天天气真不错呀。"

        style = input("风格指令（可选，直接回车跳过）: ").strip()

        out = input("输出文件（直接回车默认 output.wav）: ").strip()
        out = out if out else "output.wav"

        tts_preset(text=text, voice=voice, style_instruction=style, output_path=out)

    elif choice == "2":
        desc = input("音色描述（如：温柔的女中音，略带磁性）: ").strip()
        if not desc:
            desc = "温柔的女中音，略带磁性，像深夜电台主播"

        text = input("合成文本: ").strip()
        if not text:
            text = "夜深了，城市的灯火渐渐熄灭。我在这里陪你度过每一个安静的夜晚。"

        out = input("输出文件（直接回车默认 output.wav）: ").strip()
        out = out if out else "output.wav"

        tts_voicedesign(voice_description=desc, text=text, output_path=out)

    elif choice == "3":
        audio = input("参考音频文件路径: ").strip()
        if not audio:
            print("音色复刻必须提供音频文件")
            return

        text = input("合成文本: ").strip()
        if not text:
            text = "你好，这是我的声音克隆版本。"

        style = input("风格指令（可选，直接回车跳过）: ").strip()
        out = input("输出文件（直接回车默认 output.wav）: ").strip()
        out = out if out else "output.wav"

        tts_clone(audio_path=audio, text=text, style_instruction=style, output_path=out)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--quick":
        # 快速测试：预设音色
        tts_preset(
            text="你好！欢迎使用小米MiMo语音合成。这是一个快速测试。",
            voice="冰糖",
            output_path="test_output.wav",
        )
    else:
        interactive()
