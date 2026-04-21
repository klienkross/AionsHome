"""
Obsidian 日记读取：read_diary / read_recent / search_diary / summarize_diary
"""

from datetime import date, timedelta
from pathlib import Path

from config import SETTINGS


def _vault() -> Path | None:
    p = SETTINGS.get("obsidian_vault_path", "").strip()
    return Path(p) if p else None


async def read_diary(date_str: str) -> str:
    vault = _vault()
    if not vault:
        return "Obsidian 日记路径未配置，请在 settings.json 中添加 obsidian_vault_path。"
    if not vault.exists():
        return f"日记目录不存在，请检查路径配置：{vault}"
    f = vault / f"{date_str}.md"
    if not f.exists():
        return f"{date_str} 暂无日记。"
    return f.read_text(encoding="utf-8")


async def search_diary(keyword: str) -> str:
    vault = _vault()
    if not vault:
        return "Obsidian 日记路径未配置，请在 settings.json 中添加 obsidian_vault_path。"
    if not vault.exists():
        return f"日记目录不存在，请检查路径配置：{vault}"
    keyword_lower = keyword.lower()
    hits = []
    for md in sorted(vault.glob("*.md"), reverse=True):
        try:
            lines = md.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        matched = [l for l in lines if keyword_lower in l.lower()]
        if matched:
            hits.append(f"📅 {md.stem}\n" + "\n".join(f"  {l.strip()}" for l in matched[:3]))
        if len(hits) >= 10:
            break
    if not hits:
        return f"未找到含「{keyword}」的日记。"
    return f"搜索「{keyword}」共找到 {len(hits)} 篇日记：\n\n" + "\n\n".join(hits)


async def summarize_diary(date_str: str, content: str) -> str:
    """调用哨兵提取日记实质内容（跳过模板头），失败则降级截取。"""
    from sentinel import call_sentinel_text

    prompt = (
        f"以下是 {date_str} 的日记，可能有固定模板头部（如天气、习惯打卡等）。"
        f"请跳过模板内容，用100字以内提取今天实际发生的事和心情。若无实质内容则回复'（无记录）'。\n\n{content}"
    )
    result = await call_sentinel_text(prompt, timeout=15)
    return result if result else _fallback_summary(content)


def _fallback_summary(content: str) -> str:
    """摘要失败时降级：跳过开头连续的短行（模板行），取第一段实质内容。"""
    lines = content.splitlines()
    result = []
    skipping = True
    for line in lines:
        stripped = line.strip()
        if skipping and (not stripped or len(stripped) < 20 or stripped.startswith("#")):
            continue
        skipping = False
        result.append(stripped)
        if len(" ".join(result)) > 200:
            break
    return " ".join(result)[:200] if result else content[:200]


async def read_recent(n: int) -> str:
    vault = _vault()
    if not vault:
        return "Obsidian 日记路径未配置，请在 settings.json 中添加 obsidian_vault_path。"
    if not vault.exists():
        return f"日记目录不存在，请检查路径配置：{vault}"
    n = max(1, min(14, n))
    today = date.today()
    entries = []
    for i in range(n):
        d = today - timedelta(days=i)
        date_str = d.strftime("%Y-%m-%d")
        f = vault / f"{date_str}.md"
        if not f.exists():
            continue
        content = f.read_text(encoding="utf-8")
        summary = await summarize_diary(date_str, content)
        entries.append(f"📅 {date_str}：{summary}")
    if not entries:
        return f"最近 {n} 天暂无日记。"
    return f"最近 {n} 天日记摘要：\n\n" + "\n\n".join(entries)
