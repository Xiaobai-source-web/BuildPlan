# -*- coding: utf-8 -*-
"""存档模块（pipeline.plan_store）+ 自然语言修改节点（pipeline.nodes.revise）测试。

运行：cd backend && python -m pytest tests/test_plan_store.py -q

覆盖：
  1. 基线 + 3 条修订 → rebuild(upto=1) 得到第 1 版、rebuild() 得到最新版，
     且与"逐步应用 patch"的结果完全一致
  2. undo() 退回上一版，当前版本.json 同步更新
  3. goto(plan_id, 0) 回到基线
  4. history() 返回中文可读历史，含用户原话
  5. patch 打不中 target → 不抛异常、applied=False 且有 warning
  6. 下游闭包：A→B→C 依赖，改 A → affected == [A, B, C]；改 C → affected == [C]
  7. 越界：crew 超过 workface_capacity.max_labor → 被压到上限且 warnings 非空
  8. llm=None 时规则解析仍能处理"把 5.1.1.1 的工期改成 20"
  9. 损坏的修订文件（写一段非法 JSON）→ list_revisions 不崩

注意：临时目录用 backend/_test_tmp/ 下的普通目录（不用 tempfile.mkdtemp ——
本机沙箱下它创建的目录后续写入会被拒绝），并在末尾清理。
"""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline.nodes.revise import (
    ReviseNode, _validate, build_revise_items, default_recompute,
)
from pipeline.plan_store import (
    PlanStore, apply_patch, dependency_closure, find_leaf, iter_leaves,
)

TMP_ROOT = BACKEND / "_test_tmp" / ("plan_store_p%d" % os.getpid())


# ==================== 夹具 ====================
def _leaf(tid, name, **kw):
    """一个叶子：默认带定额（1.5 单位/工日）与工作面容量上限（14 人）。"""
    leaf = {
        "id": tid,
        "name": name,
        "duration_days": 10,
        "quantity": 120.0,
        "unit": "吨",
        "work_type": "钢筋工程",
        "norm_binding": {
            "task_id": tid,
            "mode": "labor",
            "norm_value": 1.5,
            "unit": "吨/工日",
            "quantity_basis": 1.0,
            "crew": {"钢筋工": 10},
        },
        "workface_capacity": {"unit_basis": "每施工段", "max_labor": 14, "max_machine": 2},
    }
    leaf.update(kw)
    return leaf


def _plan():
    """A → B → C 三级依赖的最小计划，外加一个独立任务 D。"""
    return {
        "plan_id": "plan_store_test",
        "overview": {"project_name": "存档测试项目", "total_duration_days": 303,
                     "planned_start_date": "2026-01-01", "planned_end_date": "2026-10-30",
                     "critical_path_length": 3},
        "wbs": {"phases": [{"phase": "主体结构", "work_packages": [
            {"id": "5.1", "name": "Ⅰ区主体", "sub_packages": [
                _leaf("5.1.1.1", "Ⅰ区1-5层钢筋绑扎"),
                _leaf("5.1.1.2", "Ⅰ区混凝土浇筑", duration_days=8, quantity=200.0),
                _leaf("5.1.1.3", "Ⅰ区养护", duration_days=7, quantity=1.0, unit="项"),
                _leaf("5.1.1.4", "独立任务（无依赖）", duration_days=3),
            ]}]}]},
        "dependencies": [
            {"predecessor": "5.1.1.1", "successor": "5.1.1.2", "type": "FS", "lag_days": 0},
            {"predecessor": "5.1.1.2", "successor": "5.1.1.3", "type": "FS", "lag_days": 0},
        ],
        "cpm_result": {"total_duration_days": 303, "critical_path": ["5.1.1.1"],
                       "schedule": []},
        "meta": {"audit_status": "未审计", "plan_level": "L4"},
    }


def _patch(target, field, value, scope="auto"):
    return {"target": target, "field": field, "value": value, "scope": scope,
            "reason": "测试"}


def _three_patches():
    return [
        _patch("5.1.1.1", "duration", 20, "this"),
        _patch("5.1.1.1", "crew", {"钢筋工": 12}, "this"),
        _patch("5.1.1.2", "quantity", 260, "this"),
    ]


@pytest.fixture()
def store():
    """每个用例一个干净的档案根目录（普通目录，末尾整棵删掉）。"""
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    s = PlanStore(root=TMP_ROOT)
    yield s
    shutil.rmtree(TMP_ROOT, ignore_errors=True)


def _node(**kw):
    node = ReviseNode(**kw)
    node._emit = lambda event, data: None
    return node


def _revise(plan, text, deps=None, revise_targets=None, **kw):
    """跑一次修改节点；返回 (revision, node, ctx)。

    注意断言要看 ctx["plan_json"]（节点改的是自己的副本），不是传入的 plan 对象。
    """
    node = _node(**kw)
    ctx = {"user_instruction": text, "plan_json": plan}
    if deps is not None:
        ctx["dependencies"] = deps
    if revise_targets is not None:
        ctx["revise_targets"] = revise_targets
    node.run(ctx)
    return ctx["revision"], node, ctx


# ==================== 1. 基线 + 3 条修订 → rebuild ====================
def test_baseline_plus_three_revisions_rebuild(store):
    plan_id = "p1"
    baseline = _plan()
    current_path = store.save_baseline(plan_id, baseline)
    assert Path(current_path).is_file()
    assert store.load_current(plan_id)["wbs"] == baseline["wbs"]

    patches = _three_patches()
    for i, patch in enumerate(patches, start=1):
        store.append_revision(plan_id, patch, "第%d次修改" % i, ["5.1.1.1"], "第%d次" % i)

    assert len(store.list_revisions(plan_id)) == 3

    # 逐步应用 = 手工重放
    step = json.loads(json.dumps(baseline))
    step1 = None
    for i, patch in enumerate(patches):
        step, _changed, _p = apply_patch(step, patch)
        if i == 0:
            step1 = json.loads(json.dumps(step))

    # rebuild(upto=1) == 第 1 版；rebuild() == 最新版；两者与逐步应用一致
    assert store.rebuild(plan_id, upto=1)["wbs"] == step1["wbs"]
    latest = store.rebuild(plan_id)
    assert latest["wbs"] == step["wbs"]

    # 当前版本.json 同步为最新版
    assert store.load_current(plan_id)["wbs"] == step["wbs"]

    # 逐项核对重建出来的数值（不是"看起来对"，是具体值）
    leaves = {x["id"]: x for x in iter_leaves(latest)}
    assert leaves["5.1.1.1"]["duration_days"] == 20
    assert leaves["5.1.1.1"]["norm_binding"]["crew"] == {"钢筋工": 12}
    assert leaves["5.1.1.2"]["quantity"] == 260

    # 修订只存 patch，不存全量
    rec = store.list_revisions(plan_id)[0]
    assert set(rec.keys()) >= {"时间", "用户原话", "patch", "影响范围", "重算摘要"}
    assert "wbs" not in rec

    # 可用版本号：v0 基线 + 3 条修订
    assert [v["版本"] for v in store.versions(plan_id)] == ["v0", "v1", "v2", "v3"]


# ==================== 2. undo ====================
def test_undo_reverts_last_revision(store):
    plan_id = "p2"
    store.save_baseline(plan_id, _plan())
    for patch in _three_patches():
        store.append_revision(plan_id, patch, "改一下", ["5.1.1.1"], "改一下")

    assert len(store.list_revisions(plan_id)) == 3
    plan = store.undo(plan_id)
    assert len(store.list_revisions(plan_id)) == 2
    leaves = {x["id"]: x for x in iter_leaves(plan)}
    # 第 3 条（把 5.1.1.2 工程量改成 260）被撤销，回到基线的 200
    assert leaves["5.1.1.2"]["quantity"] == 200
    assert leaves["5.1.1.1"]["duration_days"] == 20          # 前两条仍生效
    assert leaves["5.1.1.1"]["norm_binding"]["crew"] == {"钢筋工": 12}
    # 当前版本.json 与重建结果同步
    assert store.load_current(plan_id)["wbs"] == plan["wbs"]

    store.undo(plan_id)
    store.undo(plan_id)
    assert store.list_revisions(plan_id) == []
    # 已经在基线，再 undo 不报错
    assert store.undo(plan_id)["meta"]["revision"] == 0


# ==================== 3. goto 回基线 ====================
def test_goto_zero_returns_baseline(store):
    plan_id = "p3"
    baseline = _plan()
    store.save_baseline(plan_id, baseline)
    for patch in _three_patches():
        store.append_revision(plan_id, patch, "改一下", ["5.1.1.2"], "改一下")

    plan = store.goto(plan_id, 0)
    assert store.list_revisions(plan_id) == []
    assert plan["wbs"] == baseline["wbs"]
    assert plan["meta"]["revision"] == 0
    assert plan["overview"]["total_duration_days"] == 303
    assert store.load_current(plan_id)["wbs"] == baseline["wbs"]

    # 再走到第 2 版：先补两条修订
    for patch in _three_patches()[:2]:
        store.append_revision(plan_id, patch, "改一下", ["5.1.1.1"], "改一下")
    plan2 = store.goto(plan_id, 2)
    assert len(store.list_revisions(plan_id)) == 2
    assert {x["id"]: x["duration_days"] for x in iter_leaves(plan2)}["5.1.1.1"] == 20


# ==================== 4. history ====================
def test_history_is_readable_and_contains_raw_text(store):
    plan_id = "p4"
    store.save_baseline(plan_id, _plan())
    store.append_revision(plan_id, _patch("5.1.1.1", "duration", 20),
                          "把 5.1.1.1 的工期改成 20", ["5.1.1.1"],
                          "改完：总工期 303 → 297 天")

    history = store.history(plan_id)
    assert len(history) == 2                      # 基线 + 1 条修订
    assert history[0]["用户原话"] == "（基线）"
    assert history[1]["用户原话"] == "把 5.1.1.1 的工期改成 20"
    assert history[1]["摘要"] == "改完：总工期 303 → 297 天"
    assert history[1]["时间"]                     # 时间戳非空
    for item in history:
        assert isinstance(item["摘要"], str) and item["摘要"]


# ==================== 5. 打不中 target ====================
def test_patch_missing_target_does_not_raise(store):
    plan_id = "p5"
    baseline = _plan()
    store.save_baseline(plan_id, baseline)

    miss = _patch("9.9.9.9", "duration", 5)
    store.append_revision(plan_id, miss, "改一个不存在的任务", [], "没打中")

    plan = store.rebuild(plan_id)
    assert plan["wbs"] == baseline["wbs"]          # 计划未被改动

    # 直接调纯函数：不抛异常，changed_ids 为空，patch 被标 applied=False + warning
    new_plan, changed_ids, out = apply_patch(_plan(), miss)
    assert changed_ids == []
    assert out["applied"] is False
    assert "找不到" in out["warning"] and "9.9.9.9" in out["warning"]
    assert new_plan["wbs"] == _plan()["wbs"]


# ==================== 6. 下游闭包 ====================
def test_downstream_closure_only_affected(store):
    deps = _plan()["dependencies"]
    # 纯函数：改 A → A 的下游闭包含 A、B、C；改 C → 只有 C
    assert dependency_closure(deps, ["5.1.1.1"]) == ["5.1.1.1", "5.1.1.2", "5.1.1.3"]
    assert dependency_closure(deps, ["5.1.1.2"]) == ["5.1.1.2", "5.1.1.3"]
    assert dependency_closure(deps, ["5.1.1.3"]) == ["5.1.1.3"]
    assert dependency_closure(deps, []) == []

    # 节点里：改 A → affected 是 A→B→C 的闭包
    rev, _node_obj, _ctx = _revise(_plan(), "把 5.1.1.1 的工期改成 20", deps=deps)
    assert rev["affected"] == ["5.1.1.1", "5.1.1.2", "5.1.1.3"]
    assert len(rev["applied"]) == 1

    # 改 C → 只有 C（不影响上游）
    rev2, _n2, _ctx2 = _revise(_plan(), "把 5.1.1.3 的工期改成 4", deps=deps)
    assert rev2["affected"] == ["5.1.1.3"]

    # 独立任务 D 没有后继 → 闭包只有自己
    rev3, _n3, _ctx3 = _revise(_plan(), "把 独立任务 的工期改成 6", deps=deps)
    assert rev3["affected"] == ["5.1.1.4"]


def test_recompute_callback_receives_only_affected_ids():
    """只重算受影响部分：回调拿到的 ids 就是闭包，且改的只有这些任务。"""
    seen = {}

    def fake_recompute(ctx, affected_ids):
        seen["ids"] = list(affected_ids)
        plan = ctx["plan_json"]
        for leaf in iter_leaves(plan):
            if leaf["id"] in affected_ids:
                leaf["duration_days"] = 99
        return {"total_duration_days": 250, "summary": "假重算"}

    plan = _plan()
    rev, _n, ctx = _revise(plan, "把 5.1.1.1 的工期改成 20",
                     deps=plan["dependencies"], recompute=fake_recompute)

    assert seen["ids"] == ["5.1.1.1", "5.1.1.2", "5.1.1.3"]
    # 回调只被调一次，且只传闭包
    assert "5.1.1.4" not in seen["ids"]
    assert "250" in rev["summary"]


def test_default_recompute_keeps_duration_when_no_norm():
    """默认重算：拿不到产能（没有 norm_binding）就保持原值，不猜。"""
    plan = _plan()
    leaf = find_leaf(plan, "5.1.1.1")
    leaf.pop("norm_binding")
    leaf["quantity"] = 500.0
    leaf["duration_days"] = 9
    rev, _n, ctx = _revise(plan, "把 5.1.1.1 的工程量改成 500", deps=plan["dependencies"])
    assert rev["applied"][0]["field"] == "quantity"
    assert find_leaf(ctx["plan_json"], "5.1.1.1")["duration_days"] == 9


def test_default_recompute_uses_quantity_over_productivity():
    """默认重算：duration = ceil(quantity / 产能)，产能 = (1/norm_value) × 人数。

    第 37 轮修正：`norm_value` 是**工日/单位**（KB 留档不变式
    `raw_value / raw_quantity_basis == labor_norm_value`），所以产能 = `1/norm_value`。
    旧夹具把 `norm_value` 当"单位/工日"直接用（产能与其倒数搞反），
    于是 1.5 工日/吨 × 4 人 被算成 6 吨/日 → 10 天；正确是 90 工日 ÷ 4 人 = 23 天。
    """
    plan = _plan()
    leaf = find_leaf(plan, "5.1.1.1")
    leaf["norm_binding"]["norm_value"] = 1.5       # 1.5 工日/吨
    leaf["norm_binding"]["crew"] = {"钢筋工": 4}   # 产能 1/1.5 × 4 = 2.667 吨/日
    leaf["quantity"] = 60.0
    leaf["duration_days"] = 10
    default_recompute({"plan_json": plan}, ["5.1.1.1"])
    assert leaf["duration_days"] == 23             # ceil(60 × 1.5 / 4) = ceil(90/4)

    # 定额改为 1 工日/吨 × 4 人 → ceil(60 × 1 / 4) = 15
    leaf["norm_binding"]["norm_value"] = 1.0
    default_recompute({"plan_json": plan}, ["5.1.1.1"])
    assert leaf["duration_days"] == 15

    # 完全拿不到产能 → 保持原值，不猜
    leaf2 = find_leaf(plan, "5.1.1.2")
    leaf2.pop("norm_binding")
    leaf2["duration_days"] = 8
    default_recompute({"plan_json": plan}, ["5.1.1.2"])
    assert leaf2["duration_days"] == 8

    out = default_recompute({"plan_json": {"wbs": {"phases": []}}}, ["5.1.1.1"])
    assert out["changed"] == []


# ==================== 7. 越界 → 压到上限 + warnings ====================
def test_crew_over_capacity_clamped_with_warning():
    plan = _plan()
    assert find_leaf(plan, "5.1.1.1")["workface_capacity"]["max_labor"] == 14

    rev, _n, ctx = _revise(plan, "把 5.1.1.1 的钢筋工加到 30 人", deps=plan["dependencies"])

    assert rev["warnings"], "越界必须回报，不能静默"
    assert any("上限" in w and "14" in w for w in rev["warnings"])
    patch = rev["applied"][0]
    assert patch["field"] == "crew"
    assert patch["value"] == {"钢筋工": 14}         # 按上限执行
    assert find_leaf(ctx["plan_json"], "5.1.1.1")["norm_binding"]["crew"] == {"钢筋工": 14}

    # 没越界时不应产生越界回报（LLM 不可用之类的提示不算）
    plan2 = _plan()
    rev2, _n2, _ctx2 = _revise(plan2, "把 5.1.1.1 的钢筋工加到 8 人", deps=plan2["dependencies"])
    assert not [w for w in rev2["warnings"] if "上限" in w], rev2["warnings"]
    assert rev2["applied"][0]["value"] == {"钢筋工": 8}


# ==================== 8. llm=None 的规则解析 ====================
def test_rule_fallback_without_llm():
    plan = _plan()
    rev, node, ctx = _revise(plan, "把 5.1.1.1 的工期改成 20", deps=plan["dependencies"])
    assert node.llm is None
    assert len(rev["patches"]) == 1
    patch = rev["applied"][0]
    assert patch["target"] == "5.1.1.1"
    assert patch["field"] == "duration"
    assert patch["value"] == 20
    assert find_leaf(ctx["plan_json"], "5.1.1.1")["duration_days"] == 20
    assert any("LLM 不可用" in w for w in rev["warnings"])
    assert rev["summary"].startswith("改完：")

    # 中文数字 + 任务名（不是 id）也能解析
    plan2 = _plan()
    rev2, _n2, ctx2 = _revise(plan2, "把Ⅰ区混凝土浇筑的工期改成十五天", deps=plan2["dependencies"])
    assert rev2["applied"][0]["target"] == "5.1.1.2"
    assert find_leaf(ctx2["plan_json"], "5.1.1.2")["duration_days"] == 15

    # 一句话两处修改：工种人数 + 工期
    plan3 = _plan()
    rev3, _n3, _ctx3 = _revise(plan3, "把Ⅰ区1-5层钢筋绑扎的钢筋工加到12人，工期压到15天",
                       deps=plan3["dependencies"])
    fields = {p["field"]: p["value"] for p in rev3["applied"]}
    assert fields.get("crew") == {"钢筋工": 12}
    assert fields.get("duration") == 15

    # 数值非法 → 进 rejected
    rev4, _n4, _ctx4 = _revise(_plan(), "把 5.1.1.1 的工期改成 -3")
    assert rev4["applied"] == []
    assert rev4["rejected"] and "负" in rev4["rejected"][0]["reason"]


def test_invalid_patch_rejected_not_silent():
    """校验不静默通过：不存在的 target / 不受支持的字段 / 非法数值都要有 rejected 理由。"""
    plan = _plan()
    items = build_revise_items(plan)

    ok, reason, _p = _validate(plan, items, _patch("0.0.0.0", "duration", 12))
    assert ok is False and "没有任务" in reason

    ok2, reason2, _p2 = _validate(plan, items, _patch("5.1.1.1", "工期", 12))
    assert ok2 is False and "字段" in reason2

    ok3, reason3, _p3 = _validate(plan, items, _patch("5.1.1.1", "crew", "很多人"))
    assert ok3 is False and "非法" in reason3

    ok4, _r4, p4 = _validate(plan, items, _patch("5.1.1.1", "duration", 12))
    assert ok4 is True and p4["value"] == 12

    # 端到端：负数工期被拒，且理由写清楚（rejected 非空、applied 为空）
    rev, _n, ctx = _revise(_plan(), "把 5.1.1.1 的工期改成 -3")
    assert rev["applied"] == []
    assert rev["rejected"] and "负" in rev["rejected"][0]["reason"]


# ==================== 9. 损坏的修订文件 ====================
def test_damaged_revision_file_is_tolerated(store):
    plan_id = "p9"
    baseline = _plan()
    store.save_baseline(plan_id, baseline)
    store.append_revision(plan_id, _patch("5.1.1.1", "duration", 20), "第一次", ["5.1.1.1"], "ok")

    # 手工塞一个非法 JSON 的修订文件
    broken = Path(store.revisions_dir(plan_id)) / "002.json"
    broken.write_text("{ 这不是合法 JSON ", encoding="utf-8")

    records = store.list_revisions(plan_id)          # 不崩
    assert len(records) == 2
    assert records[0]["损坏"] is False
    assert records[1]["损坏"] is True

    # 重建跳过损坏文件，历史继续可用（好修订照常生效）
    plan = store.rebuild(plan_id)
    assert find_leaf(plan, "5.1.1.1")["duration_days"] == 20
    assert plan["meta"]["rebuilt_damaged"] == ["002.json"]

    # 当前版本 / 历史 / 版本列表都还能读
    assert store.load_current(plan_id)["wbs"] == plan["wbs"]
    assert len(store.history(plan_id)) == 3
    assert len(store.versions(plan_id)) == 3

    # 当前版本.json 本身被写坏也要能降级回基线
    Path(store.current_path(plan_id)).write_text("坏文件", encoding="utf-8")
    assert store.load_current(plan_id)["wbs"] == baseline["wbs"]


def test_damaged_middle_revision_does_not_shift_chain(store):
    """损坏文件被跳过后，后面的修订仍按自己的序号照常重放（不被顶位）。"""
    plan_id = "p9b"
    store.save_baseline(plan_id, _plan())
    store.append_revision(plan_id, _patch("5.1.1.1", "duration", 20), "1", [], "1")
    store.append_revision(plan_id, _patch("5.1.1.2", "quantity", 260), "2", [], "2")
    store.append_revision(plan_id, _patch("5.1.1.1", "crew", {"钢筋工": 12}), "3", [], "3")

    Path(store.revisions_dir(plan_id), "002.json").write_text("{坏", encoding="utf-8")

    plan = store.rebuild(plan_id)
    leaves = {x["id"]: x for x in iter_leaves(plan)}
    assert leaves["5.1.1.1"]["duration_days"] == 20                 # 001 生效
    assert leaves["5.1.1.2"]["quantity"] == 200.0                   # 002 被跳过，保持基线
    assert leaves["5.1.1.1"]["norm_binding"]["crew"] == {"钢筋工": 12}  # 003 仍生效
    assert plan["meta"]["rebuilt_damaged"] == ["002.json"]

    # 不存在的档案：返回 None / 空历史，不抛异常
    assert store.rebuild("不存在的档案") is None
    assert store.list_revisions("不存在的档案") == []
    assert store.load_current("不存在的档案") is None
    assert store.history("不存在的档案") == [
        {"时间": "", "用户原话": "（基线）", "摘要": "初始计划，版本 v0"}]


# ==================== 附加：审计记录 / level·cost·segment 只记 meta ====================
def test_audit_log_and_meta_only_fields(store):
    plan_id = "p10"
    store.save_baseline(plan_id, _plan())
    audit = store.audit_log(plan_id)
    assert audit["audit_status"] == "未审计"
    assert audit["events"] and "基线" in audit["events"][0]["轮次"]

    before_wbs = json.loads(json.dumps(store.load_baseline(plan_id)["wbs"]))
    for field, value in (("level", "L3"), ("cost", 1234567), ("segment", "Ⅰ区")):
        store.append_revision(plan_id, _patch("", field, value), "只记 meta", [], "meta")
    plan = store.rebuild(plan_id)
    assert plan["meta"]["level"] == "L3"
    assert plan["meta"]["cost"] == 1234567
    assert plan["meta"]["segment"] == "Ⅰ区"
    assert plan["wbs"] == before_wbs                    # 树结构没被改

    store.set_audit_status(plan_id, "已审计")
    after = store.audit_log(plan_id)
    assert after["audit_status"] == "已审计"
    assert any("审计状态" in e["轮次"] for e in after["events"])


def test_add_and_remove_task_ladder(store):
    """后台阶：加/删任务的 patch 也能重放（insert 顺序稳定，重建结果一致）。"""
    plan_id = "p12"
    store.save_baseline(plan_id, _plan())
    store.append_revision(plan_id, {"target": "5.1.1.1", "field": "add_task",
                                    "value": {"id": "5.1.1.9", "name": "新增：夜间浇筑",
                                              "duration_days": 2, "quantity": 30,
                                              "crew": {"混凝土工": 6}}},
                          "加一个新任务", ["5.1.1.9"], "新增 1 项")
    store.append_revision(plan_id, {"target": "5.1.1.3", "field": "remove_task",
                                    "value": None}, "删掉养护", ["5.1.1.3"], "删除 1 项")

    plan = store.rebuild(plan_id)
    ids = [x["id"] for x in iter_leaves(plan)]
    assert "5.1.1.9" in ids and "5.1.1.3" not in ids
    assert ids.index("5.1.1.9") == ids.index("5.1.1.1") + 1     # 插在目标后面
    new_leaf = find_leaf(plan, "5.1.1.9")
    assert new_leaf["name"] == "新增：夜间浇筑"
    assert new_leaf["norm_binding"]["crew"] == {"混凝土工": 6}
    # 重建两次结果一致（重放顺序稳定）
    assert [x["id"] for x in iter_leaves(store.rebuild(plan_id))] == ids

    # 删不存在的任务 → 不抛异常，标 applied=False
    _p, changed, out = apply_patch(_plan(), {"target": "无此任务", "field": "remove_task"})
    assert changed == [] and out["applied"] is False


def test_revise_node_persists_revision_chain(store):
    """节点 + 存档串联：落修订链 → 当前版本更新 → 可回退。"""
    plan = _plan()
    plan_id = "p11"
    store.save_baseline(plan_id, plan)

    node = _node(store=store)
    ctx = {"plan_id": plan_id, "user_instruction": "把 5.1.1.1 的工期改成 20",
           "plan_json": plan, "dependencies": plan["dependencies"]}
    node.run(ctx)

    assert len(store.list_revisions(plan_id)) == 1
    rec = store.list_revisions(plan_id)[0]
    assert rec["用户原话"] == "把 5.1.1.1 的工期改成 20"
    assert rec["patch"]["field"] == "duration"
    assert rec["影响范围"] == ["5.1.1.1", "5.1.1.2", "5.1.1.3"]
    assert ctx["revision"]["revision_files"]
    assert ctx["revision"]["plan_id"] == plan_id
    assert find_leaf(ctx["plan_json"], "5.1.1.1")["duration_days"] == 20

    # 存档只是记下 patch：当前版本.json 由 rebuild 按修订链重放出来
    current = store.rebuild(plan_id)
    assert find_leaf(current, "5.1.1.1")["duration_days"] == 20
    assert find_leaf(store.load_current(plan_id), "5.1.1.1")["duration_days"] == 20

    back = store.undo(plan_id)
    assert find_leaf(back, "5.1.1.1")["duration_days"] == 10
    assert store.load_current(plan_id)["meta"]["revision"] == 0
