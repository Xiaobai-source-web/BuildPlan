# -*- coding: utf-8 -*-
"""第 33 轮回归：三道门对「/abort」的行为必须一致（真中止）

真实缺陷（用户实测会踩）：每道门的提示都写着「输入 /abort → 中止本次运行」，
但只有参数门真的认它：
  · 文件门（doc_load）把 `/abort` 当**文件路径**去读 → 读不到就"沿用输入数据"继续；
  · 审计门（audit_gate）把 `/abort` 当**审计意见** → 计划被标「未审计」。
判定已收口到 `pipeline.nodes.boundary.is_abort_decision`，本文件守它的三条边界：
整条命中、带尾巴不命中、长句里的"退出/中止"不命中。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
for p in (str(BACKEND), str(ROOT / "terminal")):
    if p not in sys.path:
        sys.path.insert(0, p)

from pipeline.nodes.boundary import (abort_exit, hits_abort_command,  # noqa: E402
                                     is_abort_decision)
from pipeline.nodes.doc_load import DocLoadNode  # noqa: E402


# ======================================================================
# 1. 判定本身
# ======================================================================
@pytest.mark.parametrize("text", ["/abort", "/ABORT", "  /abort  ", "/cancel",
                                  "abort", "退出", "中止", "退出。", "/abort！"])
def test_整条输入是中止命令(text):
    assert hits_abort_command(text) is True, text


@pytest.mark.parametrize("text", [
    "/abort 顺便说一句",            # 带尾巴：是"顺便补一句话"，不是要中止
    "我要退出这个模式吗",            # 长句里的"退出"
    "这个阶段要不要中止一下",        # 长句里的"中止"
    "为什么地上主体只有 3 条？",     # 审计门里的普通提问
    "什么意思",                      # 用户实测里出现过的疑问句
    "怎么会这样呢",
    "把 5.1.1.1 的工期改成 20",
    "",
    None,
])
def test_不是中止命令(text):
    assert hits_abort_command(text) is False, text


def test_注册表给的abort也算():
    assert is_abort_decision({"action": "abort"}) is True
    assert is_abort_decision({"action": "abort", "manual_input": "随便"}) is True
    assert is_abort_decision({"passed": True}) is False
    assert is_abort_decision(None) is False
    assert is_abort_decision("不是字典") is False


def test_abort_exit工厂给出可用的返回值():
    check = abort_exit("参数复核门")
    assert check({"passed": True}) is None
    out = check({"action": "abort"})
    assert out and "_stop" in out and "中止" in out["_stop"]


# ======================================================================
# 2. 文件门：手输 /abort 必须真中止（真缺陷）
# ======================================================================
class _FakeRegistry:
    """假交互登记：`wait` 直接返回剧本里的决策。"""

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.registered = []

    def register(self, rid):
        self.registered.append(rid)

    def wait(self, rid, cancel_evt=None, timeout=None):
        return self.decisions.pop(0) if self.decisions else {"action": "abort"}


def _run_doc_load(decision):
    node = DocLoadNode()
    node._registry = _FakeRegistry([decision])
    node._run_id = "t"
    return node, node.run({"prompt": "给我排个计划", "_run_id": "t"})


def test_文件门手输abort真的中止():
    node, out = _run_doc_load({"passed": False, "manual_input": "/abort"})
    assert "_stop" in out, out
    assert "中止" in out["_stop"]
    assert "中止" in node.done_summary
    # 关键：不能把 /abort 当文件路径去读（老缺陷）
    assert "doc_content" not in out or out.get("doc_content") is None


def test_文件门注册表abort照旧取消():
    node, out = _run_doc_load({"action": "abort"})
    assert "_stop" in out and "中止" in out["_stop"]
    assert "中止" in node.done_summary


def test_文件门带尾巴的abort不当中止():
    """/abort 顺便说一句 → 是补充说明，走原路径（不当中止）。"""
    node, out = _run_doc_load({"passed": False, "manual_input": "/abort 顺便说一句"})
    assert "_stop" not in out, out


def test_文件门打Y照旧放行():
    node, out = _run_doc_load({"passed": True})
    assert "_stop" not in out
    assert "确认输入为全部数据" in node.done_summary


# ======================================================================
# 3. 审计门：同一判定（细节见 test_audit_gate_dialogue.py 的用例）
# ======================================================================
def test_审计门也共用同一份判定():
    import pipeline.nodes.audit_gate as ag
    src = Path(ag.__file__).read_text(encoding="utf-8")
    assert "is_abort_decision" in src, "审计门必须共用 boundary 的中止判定"


def test_文件门也共用同一份判定():
    src = Path(__file__).resolve().parents[1] / "pipeline" / "nodes" / "doc_load.py"
    assert "is_abort_decision" in src.read_text(encoding="utf-8")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
