"""token 用量与费用记账 —— 进程级累加器。

为什么放在这里：
  `llm.py` 的 `_post()` 是**所有 LLM 调用的唯一出口**，在那一处记账即可覆盖
  chat_text / chat_json / chat_tools 全部路径，节点代码一行都不用改。

归属到节点：
  引擎在执行节点前调用 `set_current_node(node.name)`，模块用 threading.local 记录，
  这样能算出"哪个环节最烧钱"。拿不到归属时记为 "(未归属)"。
"""

import threading

from . import pricing

_local = threading.local()
_lock = threading.Lock()


class UsageMeter:
    """累计调用次数、token 与费用。线程安全（流水线在子线程里跑）。"""

    def __init__(self):
        self.reset()

    def reset(self):
        with _lock:
            self.calls = 0
            self.prompt_tokens = 0
            self.completion_tokens = 0
            self.total_tokens = 0
            self.cost_cny = 0.0
            self.by_node = {}       # 节点名 -> token 数
            self.by_model = {}      # 模型名 -> token 数
            self.failed_calls = 0   # 失败的调用（不计费，但要让人知道）
            self.retries = 0        # 瞬时错误重试次数（5xx/429/网络抖动，见 llm.py）
            self.notes = []         # 运行环境提示（如端点不支持关思考）

    def record(self, model, usage, node=None):
        """记录一次成功调用的用量。

        usage: OpenAI 兼容响应里的 usage 字段（可能缺失）。
        """
        if not isinstance(usage, dict):
            usage = {}
        try:
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("completion_tokens") or 0)
            tt = int(usage.get("total_tokens") or (pt + ct))
        except (TypeError, ValueError):
            pt = ct = tt = 0
        if pt == 0 and ct == 0:
            # 有些兼容实现不返回 usage：只记次数，不编造 token 数
            with _lock:
                self.calls += 1
            return
        name = node or current_node() or "(未归属)"
        with _lock:
            self.calls += 1
            self.prompt_tokens += pt
            self.completion_tokens += ct
            self.total_tokens += tt
            self.cost_cny = round(
                self.cost_cny + pricing.cost_of(model, pt, ct), 4)
            self.by_node[name] = self.by_node.get(name, 0) + tt
            self.by_model[model or "?"] = self.by_model.get(model or "?", 0) + tt

    def record_failure(self):
        with _lock:
            self.failed_calls += 1

    def record_retry(self, n=1):
        """记录一次**瞬时错误重试**（HTTP 5xx / 429 / 网络超时，见 llm.py）。

        为什么单独计数而不混进 failed_calls：重试往往是"最终成功但端点正在抖"。
        真实故障里 boundary 节点一次瞬时 500 就被静默降级成关键词兜底，计划里
        labor / equipment / 材料清单 全空 —— 有这个计数，用户能一眼看出"这次跑重试过
        N 次"，而不是去猜结果为什么变差。
        """
        with _lock:
            self.retries += int(n)

    def record_note(self, text):
        """记一条**运行环境**提示（如"端点不支持关思考，速度会偏慢"）。

        为什么放在记账器里：`llm.py::_post` 是所有调用的唯一出口，那里探测到的
        端点能力（支持/不支持关思考）只有这里能带到终端 —— 否则用户永远不知道
        自己为什么在慢速档。去重，避免每次调用都堆一条。
        """
        t = str(text or "").strip()
        if not t:
            return
        with _lock:
            if t not in self.notes:
                self.notes.append(t)

    def top_nodes(self, n=5):
        """最烧钱的几个环节（按 token 降序）。"""
        with _lock:
            items = sorted(self.by_node.items(), key=lambda kv: -kv[1])
        return items[:n]

    def summary_line(self):
        """一行中文摘要，终端直接用。"""
        if self.calls == 0:
            return "本次运行未调用大模型（0 token / ¥0）"
        line = ("本次运行：{} 次调用 · 输入 {:,} tok · 输出 {:,} tok · 合计 {:,} tok · 约 ¥{:.4f}"
                .format(self.calls, self.prompt_tokens, self.completion_tokens,
                        self.total_tokens, self.cost_cny))
        if self.retries:
            line += " · 瞬时错误重试 {} 次".format(self.retries)
        return line

    def snapshot(self):
        """转成 schemas.Usage 可用的 dict。"""
        with _lock:
            return {
                "calls": self.calls,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "cost_cny": self.cost_cny,
                "retries": self.retries,
                "by_node": dict(self.by_node),
                "model": ", ".join(sorted(self.by_model)) or "",
                "note": pricing.describe(),
                "notes": list(self.notes),
            }


_METER = UsageMeter()


def meter():
    """取进程级累加器。"""
    return _METER


def reset():
    _METER.reset()


def set_current_node(name):
    """由引擎在节点执行前调用，用于把用量归属到具体环节。"""
    _local.node = name


def current_node():
    return getattr(_local, "node", None)


# ---------------- 模块级便捷函数（给 llm.py 这类唯一出口用，少一层调用） ----------------

def record(model, usage_dict, node=None):
    """记录一次成功调用的用量。"""
    return _METER.record(model, usage_dict, node=node)


def record_failure():
    """记录一次失败的调用（不计费）。"""
    return _METER.record_failure()


def record_retry(n=1):
    """记录一次瞬时错误重试（5xx / 429 / 网络超时）。"""
    return _METER.record_retry(n)


def record_note(text):
    """记录一条运行环境提示（去重）。"""
    return _METER.record_note(text)
