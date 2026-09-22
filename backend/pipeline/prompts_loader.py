"""Prompt 文件加载（M0 提炼自 Dify YAML 的 LLM 节点 Prompt，存档于 backend/prompts/）"""

from . import config

_CACHE = {}


def load(name: str) -> str:
    """按文件名加载 prompt 文本（如 'extract_params.txt'），带缓存。"""
    if name not in _CACHE:
        _CACHE[name] = (config.PROMPTS_DIR / name).read_text(encoding="utf-8")
    return _CACHE[name]
