"""GitHub REST API client for Aions_memory repo sync."""

import base64
import logging

import httpx

from config import SETTINGS

log = logging.getLogger("github_sync")

_client: httpx.AsyncClient | None = None


def _get_config() -> dict:
    token = SETTINGS.get("github_sync_token", "")
    repo = SETTINGS.get("sync_repo", "")
    if not token or not repo:
        raise ValueError("github_sync_token and sync_repo must be set in settings.json")
    return {"token": token, "repo": repo}


def _headers() -> dict:
    cfg = _get_config()
    return {
        "Authorization": f"token {cfg['token']}",
        "Accept": "application/vnd.github.v3+json",
    }


def _base_url() -> str:
    cfg = _get_config()
    return f"https://api.github.com/repos/{cfg['repo']}"


async def _ensure_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


async def read_file(path: str) -> dict | None:
    """Read a file from the repo. Returns {"content": str, "sha": str} or None if not found."""
    client = await _ensure_client()
    url = f"{_base_url()}/contents/{path}"
    resp = await client.get(url, headers=_headers())
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return {"content": content, "sha": data["sha"]}


async def write_file(path: str, content: str, message: str, sha: str | None = None) -> str:
    """Write/update a file in the repo. Returns new sha."""
    client = await _ensure_client()
    url = f"{_base_url()}/contents/{path}"
    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode(),
    }
    if sha:
        body["sha"] = sha
    resp = await client.put(url, headers=_headers(), json=body)
    resp.raise_for_status()
    return resp.json()["content"]["sha"]


async def batch_commit(files: dict[str, str], message: str) -> str:
    """
    Commit multiple files at once using Git Trees API.
    files: {path: content_string}
    Returns the new commit sha.
    """
    client = await _ensure_client()
    headers = _headers()
    base = _base_url()

    # 1. Get ref for main branch
    ref_resp = await client.get(f"{base}/git/ref/heads/main", headers=headers)
    ref_resp.raise_for_status()
    head_sha = ref_resp.json()["object"]["sha"]

    # 2. Get current commit's tree sha
    commit_resp = await client.get(f"{base}/git/commits/{head_sha}", headers=headers)
    commit_resp.raise_for_status()
    base_tree_sha = commit_resp.json()["tree"]["sha"]

    # 3. Create tree with all file blobs
    tree_items = []
    for path, content in files.items():
        tree_items.append({
            "path": path,
            "mode": "100644",
            "type": "blob",
            "content": content,
        })

    tree_resp = await client.post(
        f"{base}/git/trees",
        headers=headers,
        json={"base_tree": base_tree_sha, "tree": tree_items},
    )
    tree_resp.raise_for_status()
    new_tree_sha = tree_resp.json()["sha"]

    # 4. Create commit
    commit_create_resp = await client.post(
        f"{base}/git/commits",
        headers=headers,
        json={
            "message": message,
            "tree": new_tree_sha,
            "parents": [head_sha],
        },
    )
    commit_create_resp.raise_for_status()
    new_commit_sha = commit_create_resp.json()["sha"]

    # 5. Update ref
    await client.patch(
        f"{base}/git/ref/heads/main",
        headers=headers,
        json={"sha": new_commit_sha},
    )

    return new_commit_sha


async def close():
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None
