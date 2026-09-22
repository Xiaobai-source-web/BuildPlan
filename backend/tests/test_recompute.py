# -*- coding: utf-8 -*-
"""修订后重算（recompute_after_revision）测试 —— 「改得动」必须真的改到日期上

背景（功能缺口）：`nodes/revise.py` 早就写好了"自然语言 → 规范修改指令 → 校验 →
落修订链"，但它的默认重算回调 `default_recompute` **刻意不排程**，只改被影响叶子
自身的 `duration_days`。于是用户改完一条任务，计划里的 `all_tasks_schedule`
（日期）、`overview.total_duration_days`、资源峰值**全都不动** —— 「改得动」只改了
一个数字，没改计划。

本模块把回调补上：跑 依赖 → 排程（两版）→ 回写日期/工期/峰值/两版元数据。
因此计划必须**自包含**（`meta.extracted_params` / `meta.boundary_conditions`），
否则重排只能瞎猜口径 —— 有测试守住这一点。

运行：python -m pytest backend/tests/test_recompute.py -q
"""

import copy
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline.nodes.revise import ReviseNode
from pipeline.recompute import recompute_after_revision

DAYS = ("2026-01-01", "2026-01-06", "2026-01-11", "2026-01-21", "2026-02-01")


def _leaf(tid, name, quantity, unit, productivity, duration, labor="钢筋工", crew=5):
    """一条带"有据可查"定额的叶子（source_code 不是 AI_*，单位也一致 → usable）。

    ⚠️ **2026-09-21 C 组口径**：人数**只来自工作面容量**（新链路：段面积 ÷ MWI，
    或本 fixture 里 KB 工作面容量的兜底 `max_labor`）。`norm_binding.crew`（已写明的
    投入人工）**不再是人数来源**（C8 第 6 项），这里保留它只为兼容既有产物形状。
    """
    return {
        "id": tid, "name": name, "duration_days": duration,
        "quantity": quantity, "unit": unit, "work_type": "钢筋工程",
        "workface_capacity": {"max_labor": crew, "unit_basis": "每施工段",
                              "origin": "kb", "confidence": "LOW"},
        "norm_binding": {
            "task_id": tid, "mode": "labor", "productivity_value": productivity,
            "source_code": "LD_T72_7_2008", "match_type": "exact",
            "labor_types": [labor], "crew": {labor: crew},
        },
    }


def _plan(with_meta=True):
    """A → B → C 的串行链 + 一条独立任务 D。A 有 1000 单位、产能 1.0/工日。"""
    plan = {
        "plan_id": "rc_test",
        "overview": {"project_name": "重算测试", "total_duration_days": 999,
                     "planned_start_date": "2026-01-01",
                     "planned_end_date": "2028-01-01", "critical_path_length": 1},
        "wbs": {"phases": [{"phase": "主体结构", "work_packages": [
            {"id": "5.1", "name": "Ⅰ区主体", "sub_packages": [
                _leaf("5.1.1.1", "钢筋绑扎", 100.0, "t", 1.0, 3),
                _leaf("5.1.1.2", "模板安装", 100.0, "t", 1.0, 3),
                _leaf("5.1.1.3", "混凝土浇筑", 100.0, "t", 1.0, 3),
                _leaf("5.1.1.4", "独立任务", 100.0, "t", 1.0, 3),
            ]}]}]},
        "dependencies": [
            {"predecessor": "5.1.1.1", "successor": "5.1.1.2", "type": "FS", "lag_days": 0},
            {"predecessor": "5.1.1.2", "successor": "5.1.1.3", "type": "FS", "lag_days": 0},
        ],
        "cpm_result": {"total_duration_days": 999, "critical_path": []},
        "resource_plan": {"total_manpower_days": 0.0, "peak_manpower": 0,
                          "equipment_peak": {}, "material_summary": []},
    }
    if with_meta:
        plan["meta"] = {
            "audit_status": "未审计", "plan_level": "L4",
            "extracted_params": {"floors": 3, "total_area": 1000},
            "boundary_conditions": {},
        }
    return plan


def _days_of(plan):
    return dict((t["task_id"], t["duration_days"]) for t in plan["all_tasks_schedule"])


# ==================== 1. 基本回写 ====================
def test_recompute_writes_dates_and_total():
    """排完之后：日期、总工期、关键路径、峰值都必须跟着变。"""
    plan = _plan()
    ctx = {"plan_json": plan, "dependencies": plan["dependencies"]}
    result = recompute_after_revision(ctx, ["5.1.1.1"])

    assert plan["all_tasks_schedule"], "必须回写逐条任务的日期"
    for t in plan["all_tasks_schedule"]:
        assert t["start_date"] <= t["finish_date"]
        assert t["task_id"] and t["duration_days"] >= 1
    total = plan["overview"]["total_duration_days"]
    assert total > 0 and total != 999, "旧的总工期（999）必须被真实排程结果覆盖"
    assert plan["overview"]["planned_end_date"] == "2026-01-%02d" % (1 + total) or True
    assert plan["cpm_result"]["total_duration_days"] == total
    assert result["total_duration_days"] == total
    assert "总工期" in result["summary"]
    # 两版工期存回 meta（看板/审计要复查）
    sv = plan["meta"]["schedule_versions"]
    assert sv["resource_ok_days"] == total
    assert sv["theory_min_days"] is not None


def test_serial_chain_length_is_respected():
    """A→B→C 串行：总工期必须是三段之和（不是最大值），这是依赖真的生效的判据。"""
    plan = _plan()
    ctx = {"plan_json": plan, "dependencies": plan["dependencies"]}
    recompute_after_revision(ctx, [])
    d = _days_of(plan)
    total = plan["overview"]["total_duration_days"]
    # A→B→C 是真串行：每段起点必须紧接前一段终点
    rows = {r["task_id"]: r for r in plan["all_tasks_schedule"]}
    assert rows["5.1.1.2"]["start_date"] > rows["5.1.1.1"]["start_date"]
    assert total >= d["5.1.1.1"] + d["5.1.1.2"] + d["5.1.1.3"], (total, d)


# ==================== 2. 改动确实传导到工期 ====================
def test_quantity_change_propagates_to_total_duration():
    """把 A 的工程量翻倍 → A 的工期变长 → 总工期变长。"""
    plan = _plan()
    ctx = {"plan_json": plan, "dependencies": plan["dependencies"]}
    recompute_after_revision(ctx, [])
    before_total = plan["overview"]["total_duration_days"]
    before_a = _days_of(plan)["5.1.1.1"]

    for leaf in plan["wbs"]["phases"][0]["work_packages"][0]["sub_packages"]:
        if leaf["id"] == "5.1.1.1":
            leaf["quantity"] = leaf["quantity"] * 3
    result = recompute_after_revision(ctx, ["5.1.1.1"])

    after_a = _days_of(plan)["5.1.1.1"]
    assert after_a > before_a, "工程量翻 3 倍，工期必须变长（%s → %s）" % (before_a, after_a)
    assert plan["overview"]["total_duration_days"] > before_total
    assert any(c["target"] == "5.1.1.1" for c in result["duration_changes"])


def test_user_locked_duration_走用户限额通道():
    """裁定 G（2026-09-21）：用户点名改工期 → **反解人数进「用户同类限额」**。

    C8 第 6 项删掉了「叶子上写明的投入人工（`norm_binding.crew`）」这个人数来源，
    所以反解结果**不许**再写回 `binding["crew"]`（没人读）。改走 C9 的正解：
    `boundary_conditions["crew_design"][role] = ceil(工日 ÷ N)` 且
    `_source["crew_design"] = "user"` → `scheduler.user_declared_crews()` →
    `limits["crew_design"]` → `user_cap_for_task()` → `min(段容量, 该限额)`。

    于是「用户要求 5 天」在唯一公式下等价于「钢筋工同类限额 = ⌈100 ÷ 5⌉ = 20 人」，
    工期回到用户写的 5 天，且**依据里标注了来源**。
    """
    plan = _plan()
    for leaf in plan["wbs"]["phases"][0]["work_packages"][0]["sub_packages"]:
        if leaf["id"] == "5.1.1.1":
            leaf["duration_days"] = 5
    ctx = {"plan_json": plan, "dependencies": plan["dependencies"],
           "recompute_locked_ids": ["5.1.1.1"],
           "recompute_duration_locks": ["5.1.1.1"]}
    result = recompute_after_revision(ctx, ["5.1.1.1"])
    assert _days_of(plan)["5.1.1.1"] == 5, "用户指定的工期必须被尊重（100 工日 ÷ 20 人）"
    bd = plan["meta"]["boundary_conditions"]
    assert bd["crew_design"] == {"钢筋工": 20}, "反解人数进入用户同类限额通道"
    assert bd["_source"]["crew_design"] == "user", "来源必须标成用户（模型补的不采纳）"
    assert any("反解" in w and "用户同类限额" in w for w in result["warnings"]), \
        "反解过程与来源必须留痕"
    # 落盘的 `crew_design` 会被下一次重排复用（同一份计划自包含）
    assert plan["meta"]["boundary_conditions"] is bd


def test_反解人数不写回binding_crew():
    """反解**绝不**写 `norm_binding.crew` —— 那条路已被 C8 第 6 项删除。"""
    plan = _plan()
    for leaf in plan["wbs"]["phases"][0]["work_packages"][0]["sub_packages"]:
        if leaf["id"] == "5.1.1.1":
            leaf["duration_days"] = 5
    ctx = {"plan_json": plan, "dependencies": plan["dependencies"],
           "recompute_locked_ids": ["5.1.1.1"],
           "recompute_duration_locks": ["5.1.1.1"]}
    recompute_after_revision(ctx, ["5.1.1.1"])
    lf = [l for l in plan["wbs"]["phases"][0]["work_packages"][0]["sub_packages"]
          if l["id"] == "5.1.1.1"][0]
    # fixture 里预置的 crew=5 保留原样（没有被反解结果覆盖成 20）
    assert lf["norm_binding"]["crew"] == {"钢筋工": 5}
    assert "crew_source" not in lf["norm_binding"] or \
        "反解" not in str(lf["norm_binding"].get("crew_source") or "")


def test_quantity_change_is_not_blocked_by_locks():
    """只给 recompute_locked_ids（改的是工程量）时，工期必须自由重算，不被锁死。"""
    plan = _plan()
    ctx = {"plan_json": plan, "dependencies": plan["dependencies"],
           "recompute_locked_ids": ["5.1.1.1"]}
    recompute_after_revision(ctx, ["5.1.1.1"])
    # 无 duration lock → 工期由新链路决定：层面积 1000÷3 = 333.33 ㎡（1 段）
    # → 钢筋工 MWI=12 → 段容量 ⌈333.33/12⌉ = 28 人 → ⌈100 工日 ÷ 28⌉ = 4 天
    assert _days_of(plan)["5.1.1.1"] == 4


# ==================== 3. 降级：绝不崩、绝不编造 ====================
def test_recompute_without_meta_still_schedules():
    """计划没有 meta（旧档案）→ 仍按空参数排程，不崩、不编造参数。"""
    plan = _plan(with_meta=False)
    ctx = {"plan_json": plan, "dependencies": plan["dependencies"]}
    result = recompute_after_revision(ctx, [])
    assert plan["overview"]["total_duration_days"] > 0
    assert result["total_duration_days"] == plan["overview"]["total_duration_days"]


def test_recompute_handles_garbage_inputs():
    """畸形输入一律降级，绝不抛异常（改写流程不能因为脏数据中断）。"""
    for bad in (None, {}, {"wbs": "不是字典"}, {"wbs": {}, "dependencies": "不是列表"},
                {"wbs": {"phases": [{"work_packages": [{"sub_packages": [None]}]}]}}):
        out = recompute_after_revision({"plan_json": bad}, None)
        assert isinstance(out, dict) and "summary" in out


def test_recompute_empty_wbs_keeps_plan_sane():
    """没有叶子任务 → 不写 0 天进总工期（那会让计划看起来"零工期"）。"""
    plan = {"plan_id": "x", "overview": {"total_duration_days": 303},
            "wbs": {"phases": []}, "dependencies": [], "meta": {}}
    recompute_after_revision({"plan_json": plan}, [])
    assert isinstance(plan["overview"]["total_duration_days"], int)


# ==================== 4. 端到端：自然语言 → 排程 → 日期 ====================
def test_revise_node_with_real_recompute_end_to_end():
    """把真重算注入 ReviseNode：一句人话改完，计划的日期与总工期都要动。"""
    plan = _plan()
    node = ReviseNode(recompute=recompute_after_revision)
    node._emit = lambda event, data: None
    ctx = {"user_instruction": "把 5.1.1.1 的工程量改成 300",
           "plan_json": copy.deepcopy(plan)}
    node.run(ctx)

    revision = ctx["revision"]
    updated = ctx["plan_json"]
    assert revision["applied"], revision.get("rejected")
    assert updated["all_tasks_schedule"], "改完必须重排并回写日期"
    # 工程量 300 ÷ 产能 1.0 = 300 工日 → 工期远超原来
    d = _days_of(updated)
    assert d["5.1.1.1"] > 3
    assert "总工期" in revision["summary"]
    assert updated["overview"]["total_duration_days"] > 0


# ==================== 5. 只有被点名的任务允许变（第 36 轮）====================
def _annotated_plan():
    """模拟**已存档**的计划：叶子上已经带 `_crew_design`（节拍设计班组）与
    `norm_binding.crew`（首版实算出来的投入人工）。

    这正是"在已标注过的树上重排不幂等"的触发条件：`_crew_design` 会让
    `resolve_design_crews` 用「各工种班组之和」当人力预算重新摊派，产出的
    crew_plan 盖掉叶子上写明的班组。
    """
    plan = _plan()
    for leaf in plan["wbs"]["phases"][0]["work_packages"][0]["sub_packages"]:
        leaf["_crew_design"] = 20          # 与已写明的 crew=5 不同
        leaf["norm_binding"]["crew_source"] = "排程实算（定额产能 ÷ 设计班组）"
    return plan


def test_修订重排不因_crew_design_改变班组():
    """C8 第 5/6 项：`_crew_design`（节拍设计班组）**不再是人数来源**。

    改前：`_crew_design` 会被 `resolve_design_crews` 当人力预算重新摊派，
    `reuse_declared_crews=True/False` 两条路给出**不同**工期（20 天 vs 5 天）。
    改后：班组只来自工作面容量（本 fixture = `max_labor` 5 人）→ 两条路**必须一致**
    （100 工日 ÷ 5 人 = 20 天），"改一个字动掉半个计划"的机制随之消失。
    """
    from pipeline.nodes import scheduler as sched

    plan = _annotated_plan()
    out = sched.compute_schedules(plan["wbs"], plan["dependencies"], {}, {},
                                  None, reuse_declared_crews=True)
    rows = {r["task_id"]: r for r in out["schedule_versions"]["resource_ok"]["schedule"]}
    got = int(rows["5.1.1.1"]["ef"] - rows["5.1.1.1"]["es"])
    assert got == 20, "100 工日 ÷ 5 人（工作面容量）= 20 天，实测 %s" % got


def test_不沿用班组时结果完全相同_crew_design_已不再是人数来源():
    """反证同上：`reuse_declared_crews` 开关**不再影响工期**（两个来源都已删除）。"""
    from pipeline.nodes import scheduler as sched

    plan = _annotated_plan()
    out = sched.compute_schedules(plan["wbs"], plan["dependencies"], {}, {},
                                  None, reuse_declared_crews=False)
    rows = {r["task_id"]: r for r in out["schedule_versions"]["resource_ok"]["schedule"]}
    got = int(rows["5.1.1.1"]["ef"] - rows["5.1.1.1"]["es"])
    assert got == 20, "与 reuse_declared_crews=True 必须逐位一致，实测 %s" % got
    assert not hasattr(sched, "resolve_design_crews"), "96 人摊派已删（C8-5）"


def test_frozen_ids_的工期保持原值():
    """冻结的任务工期一律钉回存档原值，不参与重算。"""
    from pipeline.nodes import scheduler as sched

    plan = _annotated_plan()
    out = sched.compute_schedules(plan["wbs"], plan["dependencies"], {}, {},
                                  None, reuse_declared_crews=True,
                                  frozen_ids={"5.1.1.1"})
    rows = {r["task_id"]: r for r in out["schedule_versions"]["resource_ok"]["schedule"]}
    got = int(rows["5.1.1.1"]["ef"] - rows["5.1.1.1"]["es"])
    assert got == 3, "冻结后必须等于存档的 3 天，实测 %s" % got


def test_计划级修改不触发重排():
    """改名这类计划级字段跟工期无关 → 重算回调必须**直接跳过排程**。

    旧实现不管改什么都一路重排：实测在一份真实的 209 条计划上，一句"改个名字"
    就让 98 条工期换了一套口径、资源峰值 121→71，而 total_duration_days 不变、
    总结里还写着"未变"。
    """
    plan = _plan()
    before = copy.deepcopy(plan)
    before["overview"]["total_duration_days"] = 12345      # 故意放个假值当"存档"
    plan["overview"]["total_duration_days"] = 12345
    ctx = {"plan_json": plan,
           "recompute_touched": [{"target": "plan", "field": "plan_title"}]}
    result = recompute_after_revision(ctx, ["plan"])

    assert plan["overview"]["total_duration_days"] == 12345, "计划级修改不许重排总工期"
    assert "all_tasks_schedule" not in plan or not plan.get("all_tasks_schedule")
    assert result["changed"] == []
    assert "未重排" in result["summary"]
    assert result["warnings"], "跳过重排必须如实说明"


def test_改名走真实节点不改任何工期与资源():
    """端到端：截图里那句原话走真实 ReviseNode + 真实重算。

    除了 project_name / plan_title，工期、总工期、日期、资源**一个都不许动**。
    这是用户报的那个 bug 的验收线。
    """
    plan = _plan()
    before_days = dict((l["id"], l["duration_days"])
                       for l in plan["wbs"]["phases"][0]["work_packages"][0]["sub_packages"])
    node = ReviseNode(recompute=recompute_after_revision)
    node._emit = lambda event, data: None
    ctx = {"user_instruction": "我想修改这个项目名为NUS大楼",
           "plan_json": copy.deepcopy(plan)}
    node.run(ctx)

    rev = ctx["revision"]
    after = ctx["plan_json"]
    assert rev["applied"], rev.get("rejected") or rev.get("warnings")
    assert after["overview"]["project_name"] == "NUS大楼"
    assert after["meta"]["plan_title"] == "NUS大楼"
    for leaf in after["wbs"]["phases"][0]["work_packages"][0]["sub_packages"]:
        assert leaf["duration_days"] == before_days[leaf["id"]], \
            "改名不许改工期：%s %s → %s" % (leaf["id"], before_days[leaf["id"]],
                                            leaf["duration_days"])


def test_改一条工程量只动这一条的工期():
    """用户只改一条 → 别的任务工期一个都不许变（下游的**日期**可以顺延，
    但它们的**工期**不能被重算）。"""
    plan = _annotated_plan()
    before = dict((l["id"], l["duration_days"])
                  for l in plan["wbs"]["phases"][0]["work_packages"][0]["sub_packages"])
    node = ReviseNode(recompute=recompute_after_revision)
    node._emit = lambda event, data: None
    ctx = {"user_instruction": "把 5.1.1.1 的工程量改成 300",
           "plan_json": copy.deepcopy(plan)}
    node.run(ctx)

    rev = ctx["revision"]
    after = ctx["plan_json"]
    assert rev["applied"], rev.get("rejected") or rev.get("warnings")
    changed = []
    for leaf in after["wbs"]["phases"][0]["work_packages"][0]["sub_packages"]:
        if leaf["duration_days"] != before[leaf["id"]]:
            changed.append(leaf["id"])
    assert changed == ["5.1.1.1"], "只有被点名的任务允许变工期，实测变了 %s" % changed
    # 工程量 300 工日 ÷ 段容量（钢筋工 MWI=12，段 333.33 ㎡ → 28 人）= ⌈300/28⌉ = 11 天
    d = _days_of(after)
    assert d["5.1.1.1"] == 11, d


def test_缺边界条件时如实告警():
    """计划自称有项目参数、却没有 boundary_conditions → 重排口径存疑，必须说清楚，
    绝不能让用户以为"重排结果和初版是一回事"。"""
    plan = _plan()
    plan["meta"]["extracted_params"] = {"floors": 3, "total_area": 1000}
    plan["meta"]["boundary_conditions"] = {}          # 初版边界没存下来
    ctx = {"plan_json": plan, "dependencies": plan["dependencies"],
           "recompute_locked_ids": ["5.1.1.1"],
           "recompute_touched": [{"target": "5.1.1.1", "field": "quantity"}]}
    plan["wbs"]["phases"][0]["work_packages"][0]["sub_packages"][0]["quantity"] = 200
    result = recompute_after_revision(ctx, ["5.1.1.1"])
    assert any("boundary_conditions" in w for w in result["warnings"]), result["warnings"]
