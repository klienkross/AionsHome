import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock, patch


def test_read_file_success():
    """read_file should decode base64 content from GitHub API response."""
    async def _run():
        mock_content = base64.b64encode(b'{"hello": "world"}').decode()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json = lambda: {"content": mock_content + "\n", "sha": "abc123"}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("github_sync._ensure_client", new=AsyncMock(return_value=mock_client)), \
             patch("github_sync._headers", return_value={"Authorization": "token test"}), \
             patch("github_sync._base_url", return_value="https://api.github.com/repos/test/repo"):
            from github_sync import read_file
            result = await read_file("test.json")
            assert result == {"content": '{"hello": "world"}', "sha": "abc123"}

    asyncio.run(_run())


def test_read_file_not_found():
    """read_file should return None for 404."""
    async def _run():
        mock_response = MagicMock()
        mock_response.status_code = 404

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("github_sync._ensure_client", new=AsyncMock(return_value=mock_client)), \
             patch("github_sync._headers", return_value={}), \
             patch("github_sync._base_url", return_value="https://api.github.com/repos/test/repo"):
            from github_sync import read_file
            result = await read_file("nonexistent.json")
            assert result is None

    asyncio.run(_run())


def test_write_file_new():
    """write_file should PUT base64-encoded content and return sha."""
    async def _run():
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.raise_for_status = MagicMock()
        mock_response.json = lambda: {"content": {"sha": "new_sha_456"}}

        mock_client = AsyncMock()
        mock_client.put = AsyncMock(return_value=mock_response)

        with patch("github_sync._ensure_client", new=AsyncMock(return_value=mock_client)), \
             patch("github_sync._headers", return_value={"Authorization": "token test"}), \
             patch("github_sync._base_url", return_value="https://api.github.com/repos/test/repo"):
            from github_sync import write_file
            sha = await write_file("test.json", '{"data": 1}', "test commit")
            assert sha == "new_sha_456"

    asyncio.run(_run())
