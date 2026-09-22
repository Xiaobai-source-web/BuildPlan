# -*- coding: utf-8 -*-
"""plan_json 契约的"不许静默丢字段"测试

背景（真实缺陷）：plan_json 落盘前会过 `PlanJson.model_validate(...).model_dump()`，
而 pydantic v2 默认 `extra="ignore"` —— **没在契约里声明的字段会被静默丢掉**。
实测被丢的恰恰是产品最要紧的几样：

  · 叶子的 `workface_capacity` / `_crew_design`  → 排程输入没了
  · 叶子的 `_qty_source` / `_qty_formula`        → **逐值溯源没了**（核心卖点）
  · 定额的 `productivity_value`                   → 修订重算算不出工期
  · meta 的编制口径 / 审计链 / 参数 / 边界条件     → 交付物与 /revise 都受影响

丢得不报错、不警告，落盘的计划看起来完全正常。本文件把这四类钉住。

运行：python -m pytest backend/tests/test_schema_lossless.py -q
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline import schemas  # noqa: E402


def _leaf(**kw):
    leaf = {
        "id": "5.1.1.1", "name": "Ⅰ区 1-1层 钢筋绑扎", "duration_days": 5,
        "quantity": 22.5, "unit": "t", "work_type": "钢筋工程",
        "kb_activity_id": "REBAR_NEW_SLAB",
        # 溯源（产品核心卖点，必须活着到落盘）
        "_qty_source": "参数推算",
        "_qty_formula": "单栋标准层471.5㎡…",
        "_qty_per_floor": 22.5,
        # 排程输入
        "workface_capacity": {"max_labor": 14, "source_type": "ai_estimate"},
        "_crew_design": 12,
        "_beat": True, "_zone": 1, "_segment": 1, "_step": 1,
        "norm_binding": {
            "task_id": "5.1.1.1", "mode": "labor",
            "norm_value": 7.8125, "productivity_value": 0.128,
            "unit": "工日/t", "quantity_basis": 1.0,
            "source_code": "LD_T72_7_2008", "match_type": "exact",
            "labor_types": ["钢筋工"], "crew": {"钢筋工": 12},
            "crew_source": "按用户指定工期反解（定额工日不变）",
            "provenance": {"value": 7.8125, "origin": "kb", "ref": "LD_T72_7_2008",
                           "confidence": "高", "note": "人工定额"},
        },
    }
    leaf.update(kw)
    return leaf


def _plan():
    return {
        "plan_id": "lossless",
        "overview": {"project_name": "契约测试", "total_duration_days": 100,
                     "planned_start_date": "2026-01-01", "planned_end_date": "2026-04-11",
                     "critical_path_length": 1},
        "wbs": {"phases": [{"phase": "地上主体结构", "work_packages": [
            {"id": "5.1", "name": "Ⅰ区主体", "sub_packages": [_leaf()]}]}]},
        "dependencies": [],
        "cpm_result": {"total_duration_days": 100, "critical_path": ["5.1.1.1"],
                       "schedule": []},
        "resource_demand": {"tasks": []},
        "meta": {
            "audit_status": "已审计",
            "audit_rounds": [{"round": 1, "name": "WBS 结构", "passed": True},
                             {"round": 2, "name": "两版工期", "passed": True},
                             {"round": 3, "name": "Word 草案（不含图表）", "passed": True}],
            "audit_comments": [],
            "building_count": 12, "floors": 38,
            "caliber_note": "全项目共 12 栋…各栋平行施工",
            "extracted_params": {"floors": 38, "total_area": 215000, "building_count": 12},
            "boundary_conditions": {"labor": {"peak_total": 480}},
            "schedule_versions": {"theory_min_days": 583, "resource_ok_days": 743,
                                  "delta_days": 160},
            "norm_coverage": {"total": 415, "bound": 278, "bound_pct": 67.0},
            "usage": {"calls": 12, "total_tokens": 4300, "cost_cny": 0.0172,
                      "by_node": {"wbs_agent": 3000}},
            "revision": 0, "revision_label": "基线",
        },
    }


def _roundtrip(plan):
    return schemas.PlanJson.model_validate(plan).model_dump()


# ==================== 1. 叶子的溯源与排程输入 ====================
def test_leaf_traceability_survives_the_roundtrip():
    leaf = _roundtrip(_plan())["wbs"]["phases"][0]["work_packages"][0]["sub_packages"][0]
    assert leaf["_qty_source"] == "参数推算", "逐值溯源不能被静默丢掉"
    assert leaf["_qty_formula"], "中文公式不能丢"
    assert leaf["_qty_per_floor"] == 22.5


def test_leaf_scheduler_inputs_survive_the_roundtrip():
    leaf = _roundtrip(_plan())["wbs"]["phases"][0]["work_packages"][0]["sub_packages"][0]
    assert leaf["workface_capacity"]["max_labor"] == 14, "工作面容量是排程输入"
    assert leaf["_crew_design"] == 12, "设计班组是工期唯一杠杆，丢了改计划就重算不出来"
    assert leaf["kb_activity_id"] == "REBAR_NEW_SLAB"


def test_norm_binding_productivity_survives():
    """修订重算要用 productivity_value；丢了它 /revise 就算不出工期。"""
    leaf = _roundtrip(_plan())["wbs"]["phases"][0]["work_packages"][0]["sub_packages"][0]
    b = leaf["norm_binding"]
    assert b["productivity_value"] == 0.128
    assert b["norm_value"] == 7.8125
    assert b["labor_types"] == ["钢筋工"]
    assert b["crew"] == {"钢筋工": 12}
    assert b["crew_source"], "班组是怎么来的（反解/分摊）必须留痕"
    assert b["provenance"]["origin"] == "kb"


# ==================== 2. meta 自包含与审计链 ====================
def test_meta_keeps_caliber_audit_and_params():
    meta = _roundtrip(_plan())["meta"]
    assert meta["audit_status"] == "已审计"
    assert [r["round"] for r in meta["audit_rounds"]] == [1, 2, 3]
    assert meta["building_count"] == 12 and meta["floors"] == 38
    assert "12 栋" in meta["caliber_note"]
    # 修订重算要靠这两个：丢了 /revise 只能按空参数排，口径全变
    assert meta["extracted_params"]["total_area"] == 215000
    assert meta["boundary_conditions"]["labor"]["peak_total"] == 480
    assert meta["schedule_versions"]["resource_ok_days"] == 743
    assert meta["norm_coverage"]["bound_pct"] == 67.0
    # /cost 要读 usage
    assert meta["usage"]["cost_cny"] == 0.0172
    assert meta["usage"]["by_node"]["wbs_agent"] == 3000


# ==================== 3. 未来新增字段也不再被静默丢掉 ====================
def test_unknown_future_fields_are_preserved_not_dropped():
    """基类必须 extra="allow"：否则下次加字段又会静默消失（这才是这个缺陷的根因）。"""
    plan = _plan()
    leaf = plan["wbs"]["phases"][0]["work_packages"][0]["sub_packages"][0]
    leaf["_brand_new_field"] = {"anything": [1, 2, 3]}
    plan["meta"]["some_future_meta"] = "保留我"
    plan["overview"]["future_overview_field"] = 7

    out = _roundtrip(plan)
    got = out["wbs"]["phases"][0]["work_packages"][0]["sub_packages"][0]
    assert got["_brand_new_field"] == {"anything": [1, 2, 3]}
    assert out["meta"]["some_future_meta"] == "保留我"
    assert out["overview"]["future_overview_field"] == 7


# ==================== 4. 与真实流水线产物对照 ====================
def test_real_pipeline_plan_keeps_traceability():
    """跑一遍离线全链路，确认落盘的 plan_json 里溯源与定额都还在。

    单测里手搓的 plan 证明不了"真实产物"没问题 —— 节点可能在别处把字段洗掉了。
    """
    import threading
    import time

    from pipeline.builder import build_pipeline
    from pipeline.llm import LLMError

    class _NoLLM(object):
        def chat_json(self, *a, **k):
            raise LLMError("no llm")

        def chat_text(self, *a, **k):
            raise LLMError("no llm")

    pipeline = build_pipeline(run_id="lossless_e2e", llm=_NoLLM())
    events = []
    resolved = set()

    def emit(e, d):
        events.append((e, d))

    # 【第 2 批 · 域 2 / 2.1 + 收口】必写基础类型与结构形式（两者都是硬必要键，
    # 缺了参数门直接中断；本文件跑的是无 LLM 的确定性全链路）。
    ctx = {"prompt": "某住宅项目，共 12 栋，地上 38 层，基础类型：筏板基础，"
                     "结构形式：框架-剪力墙结构，"
                     "总建筑面积12.8万㎡，混凝土5.2万m³，"
                     "钢筋7.5万吨，总劳动力峰值929人，开工2025-04-16",
           "_run_id": "lossless_e2e",
           # 第 34 轮：意图识别已取消 —— 要跑完整流水线必须显式进 plan 模式
           "mode": "plan"}
    t = threading.Thread(target=lambda: pipeline.run(ctx, emit=emit), daemon=True)
    t.start()
    deadline = time.time() + 120
    while time.time() < deadline:
        if not t.is_alive():
            break
        for e, d in list(events):
            key = d.get("pause_id") or d.get("confirm_id") or d.get("review_id")
            if not key or key in resolved:
                continue
            resolved.add(key)
            try:
                if e == "node_paused":
                    pipeline.registry.resolve(key, {"action": "continue"})
                elif e == "confirm_required":
                    pipeline.registry.resolve(key, {"decision": True})
                elif e == "param_review":
                    # 本用例扮演**人**（它就是"人点 Y"的替身），所以必须显式声明
                    # answered_by="human"。第 43 轮（用户审计 P0-A）起，审计门除
                    # `passed` 之外还要记「谁答的门」：不声明就按 unknown 记账，
                    # `meta.audit_status` 只能是「未审计」（脚本代答不算人工复审）。
                    pipeline.registry.resolve(key, {"passed": True,
                                                    "answered_by": "human"})
            except Exception:
                pass
        time.sleep(0.05)
    t.join(timeout=5)
    # 第 43 轮补：跑不完就别继续断言 —— 机器繁忙时 120s 的死线可能没到 R3，
    # 后半段断言会以"审计状态不对"这种**误导性**原因失败（实测踩过一次）。
    assert not t.is_alive(), "流水线 120s 内没跑完，后面的断言会失真（不是产品缺陷）"

    plan = ctx.get("plan_json") or {}
    assert plan, "流水线没有产出 plan_json"
    leaves = [l for ph in plan["wbs"]["phases"] for wp in ph["work_packages"]
              for l in wp["sub_packages"]]
    assert leaves, "没有叶子任务"
    with_norm = [l for l in leaves if l.get("norm_binding")]
    assert with_norm, "落盘的计划里一条定额锚定都不剩 —— 契约又把字段丢了"
    # 定额产能必须落盘（norm_bind 给的是 norm_value = 工日/单位，
    # 排程器按 1/norm_value 得到"每人每天产量"，两者有其一就够，都不能丢）
    assert any(l["norm_binding"].get("productivity_value") or
               l["norm_binding"].get("norm_value") for l in with_norm), \
        "定额产能必须落盘，否则 /revise 重算不了"
    assert any((l["norm_binding"].get("provenance") or {}).get("origin") == "kb"
               for l in with_norm), "定额来源（kb）要留痕"
    # 溯源：节拍叶子应带单层量来源
    assert any(l.get("_qty_source") for l in leaves), "逐值溯源必须活着到落盘"
    # meta 自包含
    meta = plan.get("meta") or {}
    assert meta.get("extracted_params"), "meta 必须自带项目参数（/revise 要用）"
    # 第 43 轮（用户审计 P0-A）：审计状态的判据是"三轮都通过 **且** 每轮
    # answered_by == human"。本用例的应答器扮演人，已在上面显式声明。
    _rounds = meta.get("audit_rounds") or []
    assert meta.get("audit_status") == "已审计", (
        "audit_status=%r rounds=%s answered_by=%s"
        % (meta.get("audit_status"), [(r.get("round"), r.get("passed")) for r in _rounds],
           [r.get("answered_by") for r in _rounds if isinstance(r, dict)]))
    assert meta.get("schedule_versions"), "两版工期要进 meta"
