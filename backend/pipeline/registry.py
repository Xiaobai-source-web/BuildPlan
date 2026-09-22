"""交互登记（confirm_id / pause_id）— 单用户本地，内存 + TTL 清理

- register(key)    创建登记项（线程安全）
- resolve(key, v)  写入决策并唤醒（幂等：重复提交忽略）
- wait(key)        阻塞等待决策；同时观察 cancel_evt；超时返回 abort（带 reason）
                  上限默认 1 小时，可用环境变量 BUILDPLAN_GATE_TIMEOUT 覆盖
- cleanup()        清理超过 TTL 的过期登记项，防内存泄漏
"""

import os
import threading
import time

# 人工门的等待上限（秒）。第 35 轮从 600 提到 1800，第 36 轮再提到 3600。
#
# 为什么一升再升 —— 同一个缺陷被用户实测打回来三次：
#   「我输入 1，流程反而取消了」（WBS 复评门）
#   「为什么我输入 Y，却直接退出了计划」（R2 两版工期审计门）
#   「修改模式十分鸡肋」（R1 门输入疑问句）
# 三次机制完全一样：门在**用户还在看内容的时候**就超时作废了，引擎随即判定取消、
# 线程退出；用户之后输入的那一行，答的是一道**已经过期**的题。
# 对本地单用户工具来说，"用超时来兜底"本身就是错的设计 —— 用户可能正在读 40 条
# 问题清单、正在翻资料核对参数，凭什么 10 分钟就作废他的整轮计算？
# 所以：默认拉到 1 小时（够慢的人慢慢看），并且允许用环境变量继续放宽/收紧；
# 真的超时了也必须**把原因说出来**，不能只留一句"流程已取消"。
def _gate_timeout():
    raw = os.environ.get("BUILDPLAN_GATE_TIMEOUT", "").strip()
    if raw:
        try:
            value = int(float(raw))
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return 3600


GATE_TIMEOUT_SECONDS = _gate_timeout()


class InteractionRegistry:
    def __init__(self, ttl=600):
        self._events = {}      # key -> threading.Event
        self._decisions = {}   # key -> dict
        self._created = {}     # key -> time.monotonic()
        self._lock = threading.Lock()
        self.ttl = ttl

    def register(self, key):
        with self._lock:
            self._events[key] = threading.Event()
            self._decisions[key] = None
            self._created[key] = time.monotonic()

    def resolve(self, key, decision):
        with self._lock:
            if key not in self._events:
                return False  # 幂等：未知/已消费的键忽略
            self._decisions[key] = decision
            self._events[key].set()
            return True

    def wait(self, key, cancel_evt=None, timeout=None):
        """阻塞直到 resolve() / 取消 / 超时。返回决策 dict。

        超时与取消都返回 `{"action": "abort"}`，但**分开**告诉调用方原因
        （`reason: "timeout"` / `"cancelled"`）—— 用户看到的文案不一样：
        "这道门等太久了，已作废"和"你取消的运行"是两件事，不该混成一句。
        """
        if timeout is None:
            timeout = GATE_TIMEOUT_SECONDS
        ev = self._events.get(key)
        deadline = time.monotonic() + timeout
        while ev is not None and time.monotonic() < deadline:
            if cancel_evt is not None and cancel_evt.is_set():
                return {"action": "abort", "reason": "cancelled"}
            if ev.is_set():
                with self._lock:
                    return self._decisions.pop(key, {"action": "continue"})
            time.sleep(0.1)
        return {"action": "abort", "reason": "timeout"}

    def cleanup(self):
        """清理过期登记项，返回清理数量。"""
        now = time.monotonic()
        with self._lock:
            stale = [k for k, t in self._created.items() if now - t > self.ttl]
            for k in stale:
                self._events.pop(k, None)
                self._decisions.pop(k, None)
                self._created.pop(k, None)
        return len(stale)
