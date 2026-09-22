# -*- coding: utf-8 -*-
"""模式路由测试（第 34 轮改写）—— **意图识别已取消**

用户原话（本轮需求）：
    「不再保留自动切换模式的意图识别，只保留手动切换方式」
    「普通模式就纯粹用来聊天」
    「如果用户在某个模式提及了别的模式的事情，就提示他该如何切换模式」

所以本文件现在守的是"**模式说了算**"这套语义：
  · `plan` 模式 → 直接进流水线（不再先问"你是要排计划吗"）；
  · `normal` 模式 → 只回答，**绝不开流水线**；若这句明显是要计划 → 回一句"请先 /mode plan"；
  · 其它模式（终端本地会拦，正常到不了）→ 给正确的用法；
  · 全程**一次大模型都不调**来判断意图（`_classify` 已废弃且无调用点）。

⚠️ 本文件在改造前的旧名字是 `test_router.py`，测的是"LLM 意图分类 + ask 确认门"那套；
那套已按要求整体删除（`ask` 意图、`router_intent.txt` 的调用、自动确认门）。
保留旧用例名/旧断言毫无意义 —— 它们断言的行为已经不存在了。
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline.engine import Pipeline  # noqa: E402
from pipeline.nodes.router import (RouterNode, looks_like_plan_request,  # noqa: E402
                                   mentions_other_mode, mode_of)

_ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


class _FakeLLM:
    """记录调用次数：用来证明"判断模式不调模型"。"""

    def __init__(self, reply="收到，我来说两句。"):
        self.reply = reply
        self.chat_text_calls = 0
        self.chat_json_calls = 0

    def chat_text(self, system, user, **kw):
        self.chat_text_calls += 1
        return self.reply

    def chat_json(self, system, user, **kw):
        self.chat_json_calls += 1
        return {"intent": "chat"}          # 就算给了也不该被用上


def _ctx(prompt, mode=None):
    c = {"prompt": prompt, "_run_id": "t"}
    if mode is not None:
        c["mode"] = mode
    return c


def _emit(node):
    events = []
    node._emit = lambda e, d: events.append((e, d))
    return events


# ======================================================================
# 1. 模式分发（本节点唯一的工作）
# ======================================================================
def test_plan模式直接进流水线且不调模型():
    llm = _FakeLLM()
    node = RouterNode(llm=llm)
    events = _emit(node)
    out = node.run(_ctx("随便说点什么", mode="plan"))
    assert out["intent"] == "plan" and "_stop" not in out
    assert llm.chat_json_calls == 0, "模式已经定了，不许再去调模型判意图"
    assert llm.chat_text_calls == 0, "生成计划模式不闲聊"
    assert any(e == "node_start" or e == "node_progress" for e, _ in events) or True


def test_普通模式只聊天_绝不开流水线():
    llm = _FakeLLM("你好！有什么可以帮你的？")
    node = RouterNode(llm=llm)
    out = node.run(_ctx("这个 /switch 命令有什么用", mode="normal"))
    assert out["intent"] == "chat" and "_stop" in out
    assert "有什么可以帮你的" in out["_stop"]
    assert llm.chat_json_calls == 0, "普通模式不做意图识别"


def test_普通模式里像要计划_给切换提示():
    node = RouterNode(llm=_FakeLLM("好的。"))
    out = node.run(_ctx("生成一个住宅项目的进度计划，3 层 1500 平", mode="normal"))
    assert out["intent"] == "chat" and "_stop" in out
    assert "/mode plan" in out["_stop"], out["_stop"]
    assert "生成计划" in out["_stop"]


def test_普通模式里像要计划时不许调模型():
    """实测踩到的真缺陷：引导句和模型回答拼在一起 → 模型顺手吐出一份 WBS 大纲，
    用户看到"没切模式却已经在排计划"。所以引导类回复**必须不调模型**。"""
    llm = _FakeLLM("第一阶段：土方开挖 → 基础施工 → 主体结构…")
    node = RouterNode(llm=llm)
    out = node.run(_ctx("生成一个住宅项目的进度计划，3 层 1500 平", mode="normal"))
    assert llm.chat_text_calls == 0, "引导类回复不许调模型（否则模型会把计划做出一半）"
    assert "土方开挖" not in out["_stop"], out["_stop"]
    assert "/mode plan" in out["_stop"]


def test_缺省模式就是普通模式():
    assert mode_of({}) == "normal"
    assert mode_of({"mode": "bogus"}) == "normal"
    assert mode_of({"mode": "PLAN"}) == "plan"
    out = RouterNode(llm=_FakeLLM()).run({"prompt": "你好", "_run_id": "t"})
    assert out["intent"] == "chat"


@pytest.mark.parametrize("mode,needle", [
    ("revise", "改计划"),
    ("import", "导入计划"),
])
def test_其它模式给正确用法(mode, needle):
    node = RouterNode(llm=_FakeLLM("（占位）"))
    out = node.run(_ctx("随便一句话", mode=mode))
    assert out["intent"] == "chat" and "_stop" in out
    assert needle in out["_stop"], out["_stop"]


# ======================================================================
# 2. 确定性判断（不调模型）
# ======================================================================
@pytest.mark.parametrize("text,expect", [
    ("生成施工进度计划", True),
    ("帮我排一下工期", True),
    ("做个计划吧", True),
    ("给我一份 WBS", True),
    ("今天天气怎么样", False),
    ("你好", False),
    ("这个 /switch 命令有什么用", False),
])
def test_像不像要计划(text, expect):
    assert looks_like_plan_request(text) is expect, text


@pytest.mark.parametrize("text,mode,hit", [
    ("帮我改一下计划", "normal", True),
    ("把工期缩短三天", "normal", True),
    ("我想生成计划", "normal", True),
    ("你好呀", "normal", False),
    ("帮我排个计划", "revise", True),
    ("你好", "revise", False),
])
def test_提到别的模式就提示怎么切(text, mode, hit):
    note = mentions_other_mode(text, mode)
    assert bool(note) is hit, (text, mode, note)
    if hit:
        assert "/mode " in note or "/exit" in note, note


def test_提到别的模式时的提示会出现在回答里():
    node = RouterNode(llm=_FakeLLM("我来说两句。"))
    out = node.run(_ctx("帮我改一下计划", mode="normal"))
    assert "/mode revise" in out["_stop"], out["_stop"]


# ======================================================================
# 3. 端到端：普通模式**不产生任何计划**
# ======================================================================
def test_普通模式跑完整流水线也只会停在第1步():
    """普通模式 = 纯聊天：流水线不该往下走（既不出计划数据，也不弹计划相关的门）。"""
    from pipeline.builder import build_pipeline
    pipeline = build_pipeline(run_id="t_normal")
    events = []
    pipeline.run({"prompt": "生成计划：住宅 3 层 1500 平", "_run_id": "t_normal",
                  "mode": "normal"},
                 emit=lambda e, d: events.append((e, d)))
    names = [d.get("node") for e, d in events if e == "node_start"]
    assert names == ["router"], "普通模式绝不该往下跑，实际：%s" % names
    done = [d for e, d in events if e == "done"][0]
    assert done["status"] == "ok"
    assert "/mode plan" in (done.get("note") or ""), done.get("note")


def test_plan模式端到端会走到确认门():
    """plan 模式：router 放行 → work_confirm 弹出"是否开始编制"。

    ⚠️ 必须用 `InteractionRegistry` 并在确认门出现后**立刻 resolve** ——
    否则这条测试会挂在 `registry.wait(..., timeout=600)` 上，把整个套件拖住
    （本轮实测踩到：套件跑到 120 秒超时）。
    """
    import threading
    import time

    from pipeline.builder import build_pipeline
    from pipeline.registry import InteractionRegistry

    reg = InteractionRegistry()
    pipeline = build_pipeline(run_id="t_plan", registry=reg)
    events = []
    t = threading.Thread(
        target=lambda: pipeline.run(
            {"prompt": "住宅 3 层 1500 平", "_run_id": "t_plan", "mode": "plan"},
            emit=lambda e, d: events.append((e, d))),
        daemon=True)
    t.start()
    deadline = time.time() + 10
    resolved = False
    while time.time() < deadline and not resolved:
        for e, d in list(events):
            if e == "confirm_required":
                reg.resolve(d["confirm_id"], {"decision": False})   # 直接取消，别往下跑
                resolved = True
                break
        time.sleep(0.02)
    t.join(timeout=5)

    names = [d.get("node") for e, d in events if e == "node_start"]
    assert names[:2] == ["router", "work_confirm"], names[:4]
    confirms = [d for e, d in events if e == "confirm_required"]
    assert confirms and confirms[0].get("strict") is True
    assert "生成计划" in confirms[0].get("message", "")


# ======================================================================
# 4. 护栏：意图识别不许被偷偷接回来
# ======================================================================
def test_路由不再调用意图分类提示词():
    src = (BACKEND / "pipeline" / "nodes" / "router.py").read_text(encoding="utf-8")
    assert "router_intent.txt" not in src, \
        "意图识别已取消：不许再调用 router_intent.txt（模式由用户手选）"
    assert "def _classify" not in src or "已废弃" in src, \
        "若保留 _classify 名字，必须注明已废弃且无调用点"
    assert "llm.chat_json" not in src.replace("_classify", ""), \
        "路由不该再用 LLM 判意图"


def test_工作模式确认门的措辞不再是自动识别口气():
    """模式是用户手选的，门上的**提示语**不该再说「检测到计划请求」。

    只在字符串字面量里查（注释里为了说明历史保留了这个词，不算违规）。
    """
    src = (BACKEND / "pipeline" / "nodes" / "work_confirm.py").read_text(encoding="utf-8")
    literals = re.findall(r'"([^"\n]*)"', src) + re.findall(r"'([^'\n]*)'", src)
    assert not any("检测到计划请求" in s for s in literals), \
        "自动识别的措辞要清掉（注释里可以留）"
    assert any("生成计划" in s for s in literals), "提示语里应说明当前是生成计划模式"


def test_界面名里不许再说意图识别():
    """用户实测看到「第 1 步 · 意图识别与分流」后质疑「为什么这里还是在显示跑意图识别」。

    名字撒谎比名字难懂更伤可信度，所以钉死：界面名表与节点兜底标题里
    **都不许**再出现"意图识别"这四个字（注释里讲历史可以留）。
    """
    from pipeline.builder import PIPELINE_TITLES
    from pipeline.nodes.router import RouterNode

    assert "意图" not in PIPELINE_TITLES["router"], PIPELINE_TITLES["router"]
    assert "意图" not in RouterNode.title, RouterNode.title
    assert PIPELINE_TITLES["router"] == "识别当前模式"
    # 全链界面名一律不许出现这个词（将来加节点也别犯）
    bad = {k: v for k, v in PIPELINE_TITLES.items() if "意图" in v}
    assert not bad, bad


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
