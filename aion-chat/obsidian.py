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


def _strip_template(content: str) -> str:
    """去掉 frontmatter 和模板打卡行，保留实质正文。"""
    lines = content.splitlines()
    result = []
    in_frontmatter = False
    skipping_template = True
    for i, line in enumerate(lines):
        stripped = line.strip()
        if i == 0 and stripped == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if stripped == "---":
                in_frontmatter = False
            continue
        if skipping_template:
            if not stripped or stripped.startswith("- ["):
                continue
            skipping_template = False
        result.append(line)
    return "\n".join(result).strip()


def _build_diary_summary_prompt(entries: list[tuple[str, str]], user_name: str) -> str:
    diary_block = "\n\n".join(
        f"## {date_str}\n{text}" for date_str, text in entries
    )
    return (
        f"以下是{user_name}最近 {len(entries)} 天的日记正文（已去除模板打卡部分）。\n"
        f"请按以下分类整理出结构化摘要，每个分类只列要点，不展开原文：\n\n"
        f"【完成的事】有明确证据做了的（含日期）\n"
        f"【画饼/未完成】说了想做但没动的、flag、反复提到但没推进的（标注状态：纯想象/已否决/搁置）\n"
        f"【持续推进中】跨天出现的项目或事务，用最后一次动作一句话概括\n"
        f"【情绪/走神】只列话题关键词，不摘录原文，不评论\n"
        f"【身体/作息】caffeine、喝水、睡眠等纯数据记录\n\n"
        f"规则：\n"
        f"- 某个分类无内容则写（无）\n"
        f"- 不要劝学、不评判、不补充日记里没有的内容\n"
        f"- 画饼是中性分类不是贬义\n"
        f"- 整体控制在 300 字以内\n\n"
        f"{diary_block}"
    )


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
        stripped = _strip_template(content)
        if stripped:
            entries.append((date_str, stripped))
    if not entries:
        return f"最近 {n} 天暂无日记。"

    from sentinel import call_sentinel_text
    from config import load_worldbook
    user_name = load_worldbook().get("user_name", "用户")
    prompt = _build_diary_summary_prompt(entries, user_name)
    result = await call_sentinel_text(prompt, timeout=30)
    if result:
        return f"最近 {n} 天日记结构化摘要：\n\n{result}"
    fallback = "\n\n".join(
        f"📅 {d}: {_fallback_summary(t)}" for d, t in entries
    )
    return f"最近 {n} 天日记摘要（降级）：\n\n{fallback}"
