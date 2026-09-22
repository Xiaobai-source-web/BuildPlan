# -*- coding: utf-8 -*-
"""必要参数门回归：缺硬必要参数不得静默放行，试算必须全程标注。

背景（实测）：
  零参数 + 计划意图（"帮我做个施工计划"）原本会跑完 26 节点、产出 **821 条叶子 /
  1184 天** 的计划，只在编制口径里写了一行"层数暂用默认（待确认）"—— **标注了但没拦**。

修法：
  · `boundary.params_completeness()` 一处判定"缺什么、缺了会怎样"；
  · `param_review` 在缺硬必要参数时**不放行**，讲清后果并重问一轮；
    第二轮仍缺、或用户明确输入「试算」→ 才放行，且标记 `trial_mode`；
  · `meta.params_completeness` / `meta.trial_mode` 落进计划（交付物只拿得到 plan）；
  · 交付物文首打**"不可用于施工"横幅**（试算的计划看起来和正常计划一模一样，
    不加横幅流出去就是事故）。

运行：python -m pytest backend/tests/test_required_params.py -q
"""

import shutil
import sys
import threading
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline.nodes import delivery as D  # noqa: E402
from pipeline.nodes.boundary import REQUIRED_KEYS, params_completeness  # noqa: E402
from pipeline.nodes.param_review import TRIAL_HINTS, ParamReviewNode  # noqa: E402
from pipeline.registry import InteractionRegistry  # noqa: E402

_FULL = {"building_count": 12, "floors": 38, "total_area": 215000,
         "total_concrete": 82000, "planned_start_date": "2025-04-16",
         # 【第 2 批 · 域 2 / 2.1】`foundation_type`（基础类型）也是硬必要键
         # （而且是"连试算也不放行"的 `ABSOLUTE_KEYS`），所以齐备参数里必须有它。
         "foundation_type": "筏板基础",
         # 【第 2 批收口 · 用户裁决】`structure_type`（结构形式）同期进入
         # `REQUIRED_KEYS` + `ABSOLUTE_KEYS` ——「结构各类型和基础类型都是，
         # 如果没有输入，那就报错，让用户重新输入」。齐备参数里必须有它。
         "structure_type": "框架-剪力墙结构"}


# ---------------- 1. 完备性判定 ----------------
def test_空参数判定为不完备且列出缺项():
    comp = params_completeness({})
    assert comp["ok"] is False
    assert comp["missing_required"] == list(REQUIRED_KEYS)
    assert "缺层数" in comp["note"] or "层数" in comp["note"]


def test_齐备参数判定为可放行():
    comp = params_completeness(dict(_FULL))
    assert comp["ok"] is True
    assert comp["missing_required"] == []
    assert comp["missing_absolute"] == []


def test_零值不算有效项目事实():
    """0 层 / 0 面积不是有效事实，必须算缺失；0 栋同样不算有值。"""
    comp = params_completeness({"building_count": 0, "floors": 0, "total_area": 0})
    assert comp["ok"] is False
    # 4 = floors / total_area / foundation_type / structure_type（第 2 批收口后）
    assert len(comp["missing_required"]) == 4, comp["missing_required"]
    assert "building_count" in comp["missing_default"], comp


def test_缺基础类型与结构形式都算硬必要且是绝对必要():
    """【第 2 批 · 域 2 / 2.1 + 收口】基础类型 / 结构形式缺失 → 硬必要 + 绝对必要。

    用户原话：「结构各类型和基础类型都是，如果没有输入，那就报错，让用户重新输入。」
    """
    comp = params_completeness({"floors": 12, "total_area": 8500})
    assert comp["ok"] is False, comp
    assert comp["missing_required"] == ["foundation_type", "structure_type"], \
        comp["missing_required"]
    assert comp["missing_absolute"] == ["foundation_type", "structure_type"], \
        comp["missing_absolute"]
    assert "基础类型" in comp["note"], comp["note"]


def test_缺栋数不再硬拦但必须标注():
    """单栋是常态、且自带样例都不写"1 栋" —— 栋数改为取默认值 + 显著标注。"""
    comp = params_completeness({"floors": 12, "total_area": 8500,
                                "foundation_type": "独立基础",
                                "structure_type": "框架结构"})
    assert comp["ok"] is True, comp
    assert comp["missing_default"] == ["building_count"]
    assert "单栋" in comp["default_note"]


def test_缺失说明要讲后果而不只说必填():
    note = params_completeness({"building_count": 1})["note"]
    assert "倍" in note, "说明里应讲清量级后果：%s" % note


# ---------------- 2. 门的行为 ----------------
def _run_node(node, ctx):
    try:
        return {"result": node.run(ctx)}
    except Exception as exc:  # noqa: BLE001
        return {"error": exc}


def _drive(decisions, params, timeout=10):
    """后台跑参数门，按顺序应答每一次门；decision 用尽后一律 **abort**（否则门会一直问）。

    返回 (box, 门次数, ctx, 每轮的事件载荷)。
    """
    node = ParamReviewNode()
    reg = InteractionRegistry()
    node._registry = reg
    node._run_id = "t"
    node._cancel_evt = threading.Event()
    events = []
    node._emit = lambda ev, d: (events.append(d) if ev == "param_review" else None)
    ctx = {"extracted_params": dict(params)}
    box = {}
    th = threading.Thread(target=lambda: box.update(_run_node(node, ctx)), daemon=True)
    th.start()

    seen, idx = [], 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        for d in list(events):
            rid = d.get("review_id")
            if not rid or rid in seen:
                continue
            seen.append(rid)
            dec = decisions[idx] if idx < len(decisions) else {"action": "abort"}
            idx += 1
            reg.resolve(rid, dec)
        if not th.is_alive():
            break
        time.sleep(0.02)
    th.join(timeout=5)
    return box, len(seen), ctx, events


def test_按通过不会放行_必须明说试算():
    """用户拍板选"必须明说"：连按通过多少次都只重复提示，不自动进试算。"""
    box, rounds, ctx, events = _drive([{"passed": True}, {"passed": True},
                                       {"action": "abort"}], {})
    assert rounds == 3, "按通过应继续追问（实际门次数 %d）" % rounds
    assert events[1]["message"].find("不会因为按通过而放行") >= 0, \
        "第二轮提示应说明「按通过不放行」：%s" % events[1]["message"][:60]
    assert not ctx.get("trial_mode"), "按通过绝不能自动进入试算模式"


def test_明确输入试算才进试算模式():
    box, rounds, ctx, events = _drive(
        [{"passed": True}, {"passed": False, "manual_input": "试算"}],
        {"foundation_type": "筏板基础", "structure_type": "框架-剪力墙结构"})
    assert ctx["trial_mode"] is True
    assert rounds == 2, "应先在通过上追问一次，再因「试算」放行（实际 %d）" % rounds


def test_上行trial_true也能进试算():
    _, rounds, ctx, _ = _drive([{"passed": True, "trial": True}],
                               {"foundation_type": "筏板基础",
                                "structure_type": "框架-剪力墙结构"})
    assert ctx["trial_mode"] is True
    assert rounds == 1


def test_缺基础类型与结构形式时试算也不放行():
    """【第 2 批 · 域 2 / 2.1 + 收口】`ABSOLUTE_KEYS` 连"试算"都绕不过。

    判据：基础形式与结构体系是编制前提 —— 缺它们必须**报错返回、不出计划**
    （这里表现为直接 `_stop`，而不是像 floors/total_area 那样允许"用默认值试算一版"）。
    """
    box, rounds, ctx, events = _drive(
        [{"passed": True}, {"passed": False, "manual_input": "试算"}], {})
    assert rounds == 2, "第一次按通过先追问；第二次说试算即被拒（实际 %d）" % rounds
    stop = (box.get("result") or {}).get("_stop") or ""
    assert stop, box
    assert "基础类型" in stop and "结构形式" in stop, stop
    assert not ctx.get("trial_mode"), "缺绝对必要键时不许进入试算模式"
    # 【第 2 批收口 · 用户裁决】门上必须**告诉用户怎么补齐**。
    # 修前这里断言的是 "不能在门上补" —— 那句是**错的**（实测「基础类型：筏板基础」
    # 输入即可识别），用户照着它一直输入、一直被挡，门的体验就是空转。
    # 现在的正确文案：报错 + 让用户**重新输入全套参数**。
    msg = events[0]["message"]
    assert "重新输入全套项目参数" in msg, msg
    assert "不能在门上补" not in msg, msg
    assert "基础类型" in msg and "结构形式" in msg, msg


def test_用户补齐必要参数后正常放行且不是试算():
    _, rounds, ctx, _ = _drive(
        [{"passed": False, "manual_input": "栋数 12，地上 38 层，总建筑面积 215000 ㎡"}],
        {"foundation_type": "筏板基础", "structure_type": "框架-剪力墙结构"})
    assert ctx["trial_mode"] is False, "补齐后不该是试算模式"
    assert ctx["params_completeness"]["ok"] is True
    assert rounds == 1, "补齐后应一次放行（实际 %d）" % rounds


def test_标签在前的写法也要能补齐():
    """门里推荐用户写「栋数 12，地上 38 层，总建筑面积 215000」——照做必须能过。"""
    _, _, ctx, _ = _drive(
        [{"passed": False, "manual_input": "栋数 12，地上 38 层，总建筑面积 215000"}],
        {"foundation_type": "筏板基础", "structure_type": "框架-剪力墙结构"})
    assert ctx["params_completeness"]["ok"] is True, ctx["params_completeness"]


def test_手输的项目事实必须真的进计划():
    """门让用户手输「栋数 12，地上 38 层」，值就**必须**落到 extracted_params。

    修前实测（用交付包里的真实工程文档跑）：merged 只用来判断完备性，值本身仅随
    _manual_param_input 送给边界条件的 LLM；而 floors/building_count 属于
    _DOC_ONLY_KEYS（禁止 LLM 补全）→ 用户照着门上的提示输入也等于没输，
    计划仍按"层数未知"编制（821 条叶子 / 5735 天）。用户明确给出的数值应当优先。
    """
    _, _, ctx, _ = _drive(
        [{"passed": False, "manual_input": "栋数 12，地上 38 层，总建筑面积 215000 ㎡"}],
        {"total_area": 301354.26, "foundation_type": "筏板基础",
         "structure_type": "框架-剪力墙结构"})
    p = ctx["extracted_params"]
    assert p.get("floors") == 38, p
    assert p.get("building_count") == 12, p
    assert p.get("total_area") == 215000, p          # 用户明确给出 → 覆盖文档值
    assert ctx.get("manual_params_applied", {}).get("floors") == 38, ctx


def test_没手输时不动抽取结果():
    """没有手输就不能凭空改写 extracted_params（防止把文档值改掉）。"""
    _, _, ctx, _ = _drive([{"passed": True}], dict(_FULL))
    assert ctx["extracted_params"] == _FULL, ctx["extracted_params"]
    assert not ctx.get("manual_params_applied"), ctx


def test_输入abort会中止():
    box, _, _, _ = _drive([{"passed": False, "manual_input": "/abort"}], {})
    assert (box.get("result") or {}).get("_stop"), box


def test_参数齐备时一次通过():
    _, rounds, ctx, _ = _drive([{"passed": True}], dict(_FULL))
    assert rounds == 1
    assert ctx["trial_mode"] is False
    assert ctx["params_completeness"]["ok"] is True


def test_试算提示词表不为空():
    assert TRIAL_HINTS, "至少要有一个可识别的试算说法"


# ---------------- 3. 交付物横幅 ----------------
def _plan(trial=False, missing=None):
    meta = {"audit_status": "已审计",
            "params_completeness": {"ok": not (missing or trial),
                                    "missing_required": list(missing or []),
                                    "note": "缺层数将按配置默认层数推算"},
            "trial_mode": trial}
    if not trial and not missing:
        meta["params_completeness"] = {"ok": True, "missing_required": [], "note": ""}
    return {
        "plan_id": "plan_test_required",
        "overview": {"project_name": "测试", "total_duration_days": 10,
                     "planned_start_date": "2026-03-01", "planned_end_date": "2026-03-11",
                     "critical_path_length": 1},
        "wbs": {"phases": [{"phase": "主体", "work_packages": [
            {"id": "1.1", "name": "主体", "sub_packages": [
                {"id": "1.1.1", "name": "钢筋绑扎", "duration_days": 2,
                 "quantity": 10.0, "unit": "t", "work_type": "钢筋工程"}]}]}]},
        "dependencies": [],
        "cpm_result": {"total_duration_days": 10, "critical_path": ["1.1.1"],
                       "schedule": [{"task_id": "1.1.1", "es": 0, "ef": 2}]},
        "all_tasks_schedule": [{"task_id": "1.1.1", "task_name": "钢筋绑扎",
                                "start_date": "2026-03-01", "finish_date": "2026-03-03",
                                "duration_days": 2, "assigned_resources": {"钢筋工": 4}}],
        "resource_demand": {"tasks": []},
        "resource_plan": {"total_manpower_days": 8, "peak_manpower": 4,
                          "equipment_peak": {}, "material_summary": []},
        "report": "测试报告",
        "meta": meta,
    }


def test_正常计划没有横幅():
    assert D.params_banner(_plan()) == ""


def test_试算计划必须有不可用于施工横幅():
    text = D.params_banner(_plan(trial=True, missing=["floors", "total_area"]))
    assert "不可用于施工" in text
    # 第 32 轮：横幅（会写进 Word）里改用**中文参数名**，不再露 `floors` / `total_area`
    # 这种内部键名（用户实测："不要刻意使用一些英文和专业术语"）。
    assert "层数" in text and "总建筑面积" in text
    assert "floors" not in text and "total_area" not in text


def test_横幅真的出现在交付物文件里():
    import docx
    out = None
    try:
        p = _plan(trial=True, missing=["floors"])
        out = Path(D.build_plan_docx(p))
        text = "\n".join(x.text for x in docx.Document(str(out)).paragraphs)
        assert "不可用于施工" in text, "Word 里没有试算横幅"
        html_path = Path(D.build_plan_html(p))
        html = html_path.read_text(encoding="utf-8")
        assert "不可用于施工" in html, "看板里没有试算横幅"
    finally:
        if out is not None:
            shutil.rmtree(out.parent, ignore_errors=True)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok  %s" % name)
    print("全部通过")
