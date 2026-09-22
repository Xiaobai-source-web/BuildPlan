"""SSE 适配器 — T-14：引擎事件 → 严格 §5.1 帧

帧规范：
- 每条事件由空行结束；data 强制单行 JSON
- 终止统一用 done 事件后关闭连接（不使用 [DONE]）
"""

import json

from .events import EV_PING


def format_sse(event: str, data: dict) -> str:
    """把一个事件格式化为 SSE 文本块（含结尾空行）。"""
    if event == EV_PING:
        return ": ping\n\n"
    payload = json.dumps(data, ensure_ascii=False)  # 单行 JSON
    return f"event: {event}\ndata: {payload}\n\n"


def iter_sse(events_iter):
    """把 (event, data) 迭代器包装为 SSE 文本迭代器。"""
    for event, data in events_iter:
        yield format_sse(event, data)
