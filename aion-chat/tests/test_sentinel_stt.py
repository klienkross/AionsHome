"""sentinel transcribe_audio / transcribe_audio_sync 单元测试"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


class TestTranscribeAudioSync:

    @patch("sentinel.get_key", return_value=None)
    def test_no_key_returns_none(self, _):
        from sentinel import transcribe_audio_sync
        assert transcribe_audio_sync(b"fake audio") is None

    @patch("sentinel.httpx.post")
    @patch("sentinel.get_key", return_value="sk-test")
    def test_success(self, _, mock_post):
        from sentinel import transcribe_audio_sync
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"text": "你好世界"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = transcribe_audio_sync(b"audio data")
        assert result == "你好世界"

        call_kwargs = mock_post.call_args
        assert call_kwargs[1]["data"]["model"] == "FunAudioLLM/SenseVoiceSmall"
        assert call_kwargs[1]["data"]["language"] == "zh"

    @patch("sentinel.httpx.post")
    @patch("sentinel.get_key", return_value="sk-test")
    def test_emoji_stripped(self, _, mock_post):
        from sentinel import transcribe_audio_sync
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"text": "你好😀世界🎉"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = transcribe_audio_sync(b"audio data")
        assert result == "你好世界"

    @patch("sentinel.httpx.post")
    @patch("sentinel.get_key", return_value="sk-test")
    def test_empty_text_returns_none(self, _, mock_post):
        from sentinel import transcribe_audio_sync
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"text": "  "}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        assert transcribe_audio_sync(b"audio data") is None

    @patch("sentinel.httpx.post", side_effect=Exception("network error"))
    @patch("sentinel.get_key", return_value="sk-test")
    def test_exception_returns_none(self, _, __):
        from sentinel import transcribe_audio_sync
        assert transcribe_audio_sync(b"audio data") is None

    @patch("sentinel.httpx.post")
    @patch("sentinel.get_key", return_value="sk-test")
    def test_custom_filename_and_mime(self, _, mock_post):
        from sentinel import transcribe_audio_sync
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"text": "test"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        transcribe_audio_sync(b"data", filename="voice.webm", mime="audio/webm")
        files_arg = mock_post.call_args[1]["files"]["file"]
        assert files_arg[0] == "voice.webm"
        assert files_arg[2] == "audio/webm"


class TestTranscribeAudioAsync:

    @pytest.mark.asyncio
    @patch("sentinel.get_key", return_value=None)
    async def test_no_key_returns_none(self, _):
        from sentinel import transcribe_audio
        assert await transcribe_audio(b"fake audio") is None

    @pytest.mark.asyncio
    @patch("sentinel.get_key", return_value="sk-test")
    async def test_success(self, _):
        from sentinel import transcribe_audio

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"text": "异步测试"}
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("sentinel.httpx.AsyncClient", return_value=mock_client):
            result = await transcribe_audio(b"audio data")
            assert result == "异步测试"

    @pytest.mark.asyncio
    @patch("sentinel.get_key", return_value="sk-test")
    async def test_emoji_stripped(self, _):
        from sentinel import transcribe_audio

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"text": "🎵音乐🎶很好听"}
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("sentinel.httpx.AsyncClient", return_value=mock_client):
            result = await transcribe_audio(b"audio data")
            assert result == "音乐很好听"

    @pytest.mark.asyncio
    @patch("sentinel.get_key", return_value="sk-test")
    async def test_exception_returns_none(self, _):
        from sentinel import transcribe_audio

        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("timeout")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("sentinel.httpx.AsyncClient", return_value=mock_client):
            assert await transcribe_audio(b"data") is None
