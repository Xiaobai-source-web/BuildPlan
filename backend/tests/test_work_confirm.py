"""工作模式确认节点测试 —— plan 意图须强制 Y/N，非 plan 放行。

运行：python -m pytest backend/tests/test_work_confirm.py -v
"""

import sys
import threading
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline.nodes.work_confirm import WorkConfirmNode
from pipeline.registry import InteractionRegistry


def _run(node, ctx, resolve=None, timeout=3):
    """跑节点；resolve=True 确认 / False 取消 / None 不确认（用于非 plan 放行）。"""
    reg = InteractionRegistry()
    node._registry = reg
    events = []
    node._emit = lambda e, d: events.append((e, d))
    out = {}
    t = threading.Thread(target=lambda: out.update(node.run(ctx)), daemon=True)
    t.start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not t.is_alive():
            break
        hit = [d for e, d in events if e == "confirm_required"]
        if hit and resolve is not None:
            reg.resolve(hit[0]["confirm_id"], {"decision": resolve})
            break
        time.sleep(0.01)
    t.join(timeout=2)
    return out, events


def test_plan_yes_proceeds():
    """plan 意图 + Y → 进入工作模式，无 _stop。"""
    out, events = _run(WorkConfirmNode(), {"prompt": "某项目", "intent": "plan"}, resolve=True)
    assert out.get("work_mode_confirmed") is True
    assert "_stop" not in out
    assert any(e == "confirm_required" for e, _ in events)


def test_plan_no_stops():
    """plan 意图 + N → 拒绝，优雅停止并提示。"""
    out, events = _run(WorkConfirmNode(), {"prompt": "某项目", "intent": "plan"}, resolve=False)
    assert "_stop" in out
    assert "不启动" in out["_stop"]
    assert any(e == "confirm_required" for e, _ in events)


def test_non_plan_passthrough():
    """chat 意图 → 不设确认门，原样放行。"""
    out, events = _run(WorkConfirmNode(), {"prompt": "你好", "intent": "chat"})
    assert out == {}
    assert not any(e == "confirm_required" for e, _ in events)


def test_confirm_payload_strict():
    """发给终端的 confirm_required 带 strict=True（终端据此回车=取消）。"""
    _, events = _run(WorkConfirmNode(), {"prompt": "某项目", "intent": "plan"}, resolve=True)
    d = [d for e, d in events if e == "confirm_required"][0]
    assert d.get("strict") is True
    assert d.get("message")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  PASS  {fn.__name__}")
    print(f"\n全部 {len(tests)} 个 work_confirm 用例通过 ✔")