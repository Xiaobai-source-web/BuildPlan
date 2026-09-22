"""节点基类 — 引擎与节点共用，避免循环导入。

节点契约：
- `name`: 唯一英文标识（用于 SSE 事件 node 字段）
- `title`: 中文显示名
- `pause_point`: 是否为暂停点（完成后发 node_paused 并等待 /resume）
- `run(ctx) -> dict | None`: 读取 ctx，返回要合并回 ctx 的 dict
- 需要推送进度时调用 self.emit("node_progress", {...})
"""


class BaseNode:
    name = "base"
    title = "未命名节点"
    pause_point = False

    _emit = None  # 由引擎注入：def emit(event: str, data: dict) -> None

    def __init__(self):
        self.done_summary = ""  # 引擎在 run() 后读取，作为 node_done 的 summary

    def emit(self, event, data):
        if self._emit is not None:
            self._emit(event, data)
        return data

    def run(self, ctx):
        raise NotImplementedError(f"{self.name} 未实现 run()")
