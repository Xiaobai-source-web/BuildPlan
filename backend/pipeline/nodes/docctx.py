"""下游 LLM 资料摘要注入工具

extractor 产出 ctx["doc_summary"]（精炼文本），所有「生成施工方案/内容」的 LLM
在构造 user 消息时调用 combine(ctx, user)，把摘要拼到尾部，确保生成方拿到原始项目资料。
"""


def combine(ctx, user: str) -> str:
    """在 user 消息尾部追加项目资料摘要（若存在）。"""
    summary = (ctx.get("doc_summary") or "").strip()
    if not summary:
        return user
    return user + "\n\n【项目资料摘要】\n" + summary