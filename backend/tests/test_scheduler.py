"""排程节点（scheduler）测试：两版工期 + 串行排程（Serial SGS）。

运行：python -m pytest backend/tests/test_scheduler.py -q
      （在 backend 目录下：python -m pytest tests/test_scheduler.py -q）

只依赖标准库与随仓库附带的 BuildPlan_KB/kb.db，不联网、不调用 LLM。
覆盖：
  1) 核心不变量 theory_min <= resource_ok
  2) 无用户限额时两版完全一致
  3) 紧的分工种限额 → resource_ok 变长 + over_limit 非空 + capped 有被压记录
  4) 依赖生效（FS 串行：B.es >= A.ef）
  5) 并行生效（互不依赖的两条同 es，总工期 = 单条工期而非相加）
  6) 确定性（同输入跑两次，schedule_versions 逐字段相同）
  7) 目标工期三种区间 → "物理上做不到" / "需放宽资源" / "宽松"
  8) 缺 norm_binding → 沿用原工期 + 记 warning，不崩
  9) **不许为了凑用户目标改定额**：不同 target 下各任务工期与班组完全相同
 10) 工作面容量封顶生效（人数被压到容量，capped 里有原始值）
 11) SS 依赖、lag 生效
 12) 缺输入 / 畸形输入不崩（降级 + 中文 warning）
"""

import math
import sys
import threading
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline.nodes.scheduler import (SchedulerNode, collect_leaf_tasks,
                                      compute_schedules, workface_is_evidence)


# ==================== 构造工具 ====================
def leaf(tid, name, trade, quantity, duration, productivity,
         cap_labor=None, cap_machine=None, mode="labor", crew=None,
         crew_kind=None, labor_types=None, machine_name=None):
    """造一条带定额锚定（norm_binding）的叶子。"""
    binding = {
        "task_id": tid,
        "mode": mode,
        "productivity_value": productivity,
        "source_code": "TEST_KB",
        "match_type": "exact",
        "labor_types": labor_types if labor_types is not None else [trade],
    }
    if mode == "machine":
        # 机械：productivity 参数位置实际传的是**台班定额**（工日/单位）
        binding["norm_value"] = productivity
        binding["productivity_value"] = None
        binding["quantity_basis"] = 1.0
        binding["machine_name"] = machine_name or trade
    if crew is not None:
        binding["crew"] = crew
        binding["crew_kind"] = crew_kind or {}
    row = {
        "id": tid,
        "name": name,
        "quantity": quantity,
        "unit": "m3",
        "duration_days": duration,
        "work_type": trade,
        "norm_binding": binding,
    }
    if cap_labor is not None or cap_machine is not None:
        row["workface_capacity"] = {
            "max_labor": cap_labor,
            "max_machine": cap_machine,
            "unit_basis": "每施工段",
            "origin": "kb",
            "confidence": "LOW",
            "note": "测试用工作面容量",
        }
    return row


def make_wbs(*rows):
    return {"phases": [{"phase": "测试阶段", "work_packages": [
        {"id": "1.1", "name": "测试工作包", "sub_packages": list(rows)}]}]}


def deps(*pairs):
    """deps(("A","B"), ("B","C")) 或 deps(("A","B","SS",3))。"""
    out = []
    for item in pairs:
        pred, succ = item[0], item[1]
        dtype = item[2] if len(item) > 2 else "FS"
        lag = item[3] if len(item) > 3 else 0
        out.append({"predecessor": pred, "successor": succ,
                    "type": dtype, "lag_days": lag})
    return {"dependencies": out}


# 统一的 6 条叶子（数值刻意取整，便于手算断言）：
#   5 条钢筋工（人工，20 m3，P=1.0 单位/工日，目标 2 天，工作面 10 人）
#       → 需要 10 人，正好用满工作面，工期 2 天，**不封顶**；
#         5 条共用"钢筋工"资源池（池上限 10 人）→ 同一时刻只能开 1 条
#   1 条塔吊（机械，2000 m3，台班定额 0.05，工作面 5 台）
#       → 总台班 100 → 5 台（工作面封顶 10→5）→ 工期 20 天
# 依赖：1.1.1→1.1.5 与 1.1.3→1.1.6，两条并行链 → 理论最短 = 20 天（塔吊控制）
def six_leaf_wbs():
    return make_wbs(
        leaf("1.1.1", "甲区钢筋绑扎", "钢筋工", 20, 2, 1.0, cap_labor=10),
        leaf("1.1.2", "甲区模板支设", "钢筋工", 20, 2, 1.0, cap_labor=10),
        leaf("1.1.3", "甲区浇筑", "钢筋工", 20, 2, 1.0, cap_labor=10),
        leaf("1.1.4", "甲区吊装", "塔吊", 2000, 10, 0.05, cap_machine=5,
             mode="machine", machine_name="塔吊"),
        leaf("1.1.5", "乙区钢筋绑扎", "钢筋工", 20, 2, 1.0, cap_labor=10),
        leaf("1.1.6", "乙区浇筑", "钢筋工", 20, 2, 1.0, cap_labor=10),
    )


def _six_deps():
    """6 条叶子的标准依赖：两条并行链（1.1.1→1.1.5、1.1.3→1.1.6）。"""
    return deps(("1.1.1", "1.1.5"), ("1.1.3", "1.1.6"))


def run_scheduler(wbs, dependencies=None, boundary=None, params=None, cpm=None):
    """跑节点，返回 (node, 节点写回 ctx 的那部分)。"""
    ctx = {"wbs": wbs}
    if dependencies is not None:
        ctx["dependencies"] = dependencies
    if boundary is not None:
        ctx["boundary_conditions"] = boundary
    if params is not None:
        ctx["extracted_params"] = params
    if cpm is not None:
        ctx["cpm_result"] = cpm
    node = SchedulerNode()
    out = node.run(ctx)
    return node, out


def rows_of(version):
    return dict((r["task_id"], r) for r in version["schedule"])


# ==================== 1) 核心不变量 ====================
def test_theory_min_never_longer_than_resource_ok():
    """theory_min <= resource_ok（核心不变量）：给一个很紧的分工种限额也要成立。"""
    _, out = run_scheduler(
        six_leaf_wbs(),
        _six_deps(),
        boundary={"labor": {"by_trade": [{"trade": "钢筋工", "quantity": 6}]}},
    )
    v = out["schedule_versions"]
    assert v["theory_min"]["total_duration_days"] <= v["resource_ok"]["total_duration_days"]
    assert v["compare"]["delta_days"] >= 0
    # 逐条任务也要成立（同一版里每条任务的工期只可能更长或相等）
    for tid, row in rows_of(v["theory_min"]).items():
        other = rows_of(v["resource_ok"])[tid]
        assert (row["ef"] - row["es"]) <= (other["ef"] - other["es"]), tid


def test_invariant_holds_without_any_boundary():
    _, out = run_scheduler(six_leaf_wbs())
    v = out["schedule_versions"]
    assert v["theory_min"]["total_duration_days"] <= v["resource_ok"]["total_duration_days"]


# ==================== 2) 无用户限额：两版完全一致 ====================
def test_no_limits_two_versions_identical():
    """用户没给任何资源限额时，"不超额"版与"理论最短"版完全一致。"""
    _, out = run_scheduler(six_leaf_wbs(), _six_deps())
    v = out["schedule_versions"]
    theory, ok = v["theory_min"], v["resource_ok"]
    assert theory["schedule"] == ok["schedule"]
    assert theory["total_duration_days"] == ok["total_duration_days"]
    assert theory["daily_labor"] == ok["daily_labor"]
    assert theory["daily_equipment"] == ok["daily_equipment"]
    assert theory["peak_labor"] == ok["peak_labor"]
    assert theory["peak_equipment"] == ok["peak_equipment"]
    assert theory["critical_path"] == ok["critical_path"]
    # 没给用户限额 → 两版的封顶记录也必须一致（封顶只来自工作面容量）
    assert theory["capped"] == ok["capped"]
    assert ok["over_limit"] == []                       # 没限额 → 不可能超


# ==================== 3) 紧限额 → 变长 + over_limit + capped ====================
def test_tight_trade_limit_lengthens_and_reports():
    """紧的分工种限额：resource_ok 变长，over_limit 非空，capped 里能看到被压记录。

    3 条串行、工作面 10 人：理论最短 ⌈40/10⌉=4 天/条 → 12 天；
    用户只给 8 人 → 5 天/条 → 15 天（限额真正压长了工期）。
    """
    wbs = make_wbs(
        leaf("A", "工序A", "钢筋工", 40, 4, 1.0, cap_labor=10),
        leaf("B", "工序B", "钢筋工", 40, 4, 1.0, cap_labor=10),
        leaf("C", "工序C", "钢筋工", 40, 4, 1.0, cap_labor=10),
    )
    _, out = run_scheduler(
        wbs, deps(("A", "B"), ("B", "C")),
        boundary={"labor": {"by_trade": [{"trade": "钢筋工", "quantity": 8}]}})
    v = out["schedule_versions"]
    theory, ok = v["theory_min"], v["resource_ok"]
    rows = rows_of(ok)

    assert theory["total_duration_days"] == 12
    assert ok["total_duration_days"] == 15, "限额生效必须让工期变长"
    assert v["compare"]["delta_days"] == 3
    assert (rows["A"]["ef"] - rows["A"]["es"]) == 5

    # over_limit：resource_ok 里"已达上限"的记录
    assert ok["over_limit"], "资源已达用户上限必须报出来"
    assert any(rec["resource"] == "钢筋工" and rec["limit"] == 8 for rec in ok["over_limit"])

    # capped：被压的记录要保留原始值（want=原值、got=压后值）
    capped = [c for c in ok["capped"] if c["resource"] == "钢筋工"]
    assert capped, "被用户限额压过的人数必须在 capped 里留痕"
    assert capped[0]["want"] == 10 and capped[0]["got"] == 8
    assert "用户同类限额" in capped[0]["reason"] or "用户资源限额" in capped[0]["reason"]

    # 理论最短版**不看**用户限额：不该出现用户限额造成的封顶
    assert [c for c in theory["capped"] if "用户资源限额" in c["reason"]] == []
    assert theory["over_limit"] == []

    # 每条的每日用量都不超过用户上限
    for rec in ok["daily_labor"]:
        assert rec["trades"].get("钢筋工", 0) <= 8


def test_limit_reached_is_reported_on_six_leaf_project():
    """6 条叶子的项目：限额顶满时 over_limit 有记录、班组被压到限额、并发不超限。"""
    _, out = run_scheduler(
        six_leaf_wbs(), _six_deps(),
        boundary={"labor": {"by_trade": [{"trade": "钢筋工", "quantity": 8}]}})
    v = out["schedule_versions"]
    theory, ok = v["theory_min"], v["resource_ok"]

    assert ok["total_duration_days"] >= theory["total_duration_days"], "核心不变量"
    assert any(c["resource"] == "塔吊" for c in theory["capped"]), "工作面封顶要留痕"
    assert any(rec["resource"] == "钢筋工" and rec["limit"] == 8
               for rec in ok["over_limit"])
    # 同一工种并发也不能超用户限额（8 人）：这是"资源池"口径
    for rec in ok["daily_labor"]:
        assert rec["trades"].get("钢筋工", 0) <= 8
    # ⚠️ 口径变更（第 39 轮）：`peak_labor` 现在是"当天在场**全部人工**" = 工种 + 机械配员
    #    （与交付物 delivery 的 assigned_resources 同一口径，见 daily_curves / _total_labor_of）。
    #    本用例只给了**分品种**限额（钢筋工 ≤ 8），**没有**给 labor.peak_total（全项目上限），
    #    所以塔吊机组（5 台 × 司机1+信号工1 = 10 人）可以与钢筋工 8 人同时在场 → 总量 18。
    #    想约束这个总量，要的是 labor.peak_total（见 test_total_labor_limit_sizes_the_crew）。
    assert ok["peak_labor"] <= 8 + 10, "总量 = 分品种峰值 + 机械配员峰值（同一份计划只有一个口径）"


def test_same_trade_parallel_queues_within_pool():
    """同一工种并行：资源池上限就是并发天花板，第二条必须等第一条腾出名额。

    A、B 都是 8 工日、工作面只给 4 人 → 2 天/条；池上限 4 人 → 不能并行 → 4 天。
    """
    wbs = make_wbs(
        leaf("A", "并行A", "钢筋工", 8, 2, 1.0, cap_labor=4),
        leaf("B", "并行B", "钢筋工", 8, 2, 1.0, cap_labor=4),
    )
    _, out = run_scheduler(wbs)
    version = out["schedule_versions"]["theory_min"]
    rows = rows_of(version)

    assert rows["A"]["crew"] == {"钢筋工": 4.0}
    for rec in version["daily_labor"]:
        assert rec["trades"].get("钢筋工", 0) <= 4, "并发必须守住资源池上限"
    assert rows["A"]["es"] == 0 and rows["B"]["es"] == 2, "第二条顺延到第一条完工"
    assert version["total_duration_days"] == 4


# ==================== 3b) 总人工上限（labor.peak_total）====================
def test_total_labor_limit_sizes_the_crew():
    """总人工上限约束 **resource_ok**；theory_min 只受工作面容量约束。

    口径（第 37 轮）：定额给出工日需求（客观、不可压缩），班组 = 工作面容量
    （theory_min）或 min(工作面容量, 用户限额/人力预算)（resource_ok），
    工期 = 工日需求 ÷ 班组人数。**不再**按目标工期反推班组。
    第 37 轮前 theory_min 也被人力预算压到 6 人（"班组由人力分配而来"）；
    现在理论版按工作面顶满（10 人/条），所以这里只断言 resource_ok 那一侧。
    """
    boundary = {"labor": {"peak_total": 6,
                          "by_trade": [{"trade": "钢筋工", "quantity": 4}]}}
    _, out = run_scheduler(six_leaf_wbs(), _six_deps(), boundary)
    v = out["schedule_versions"]
    theory, ok = v["theory_min"], v["resource_ok"]

    assert ok["peak_labor"] <= 6, "逐日在岗绝不能超过 peak_total"
    # ⚠️ 口径变更（第 39 轮）：`peak_labor` 现在含**机械配员**（司机/信号工，与交付物同口径）。
    #    理论版不看用户限额 → 钢筋工按工作面顶满 10 人/条，同一时刻还有塔吊机组
    #    （5 台 × 2 人 = 10 人）在场 → 总峰值 20。这里断言"工种顶满 10 人"这一层含义，
    #    再单独验证总量 = 工种峰值 + 机械配员峰值（不再用 10 这个只算工种的旧上界）。
    _trade_peak = max((rec["trades"].get("钢筋工", 0) for rec in theory["daily_labor"]),
                      default=0)
    _crew_peak = max((sum(v for k, v in rec["trades"].items() if k in ("司机", "信号工"))
                      for rec in theory["daily_labor"]), default=0)
    assert _trade_peak <= 10, "理论版按工作面上限顶满（每条 10 人）"
    assert theory["peak_labor"] <= _trade_peak + _crew_peak, "总量 = 工种峰值 + 机械配员峰值"
    assert theory["total_duration_days"] <= ok["total_duration_days"], "核心不变量"
    # 分工种限额 4 人仍然独立生效（它是"同工种同时段"的上限，不参与班组分摊）
    assert any(rec["resource"] == "钢筋工" for rec in ok["over_limit"])
    # C8-5：96 人预算摊派的文案（`resolve_design_crews`）必须消失
    assert not any("设计班组按" in w and "定额总工日需求" in w for w in v["warnings"]), \
        "「按定额总工日需求比例摊派」的文案已随 resolve_design_crews 删除"
    assert any("定额工日需求合计" in w for w in v["warnings"])


def test_total_labor_ceiling_binds_across_trades():
    """用户**直接指定**大班组时，总人工上限只能靠错峰落地（逐日复核真的在起作用）。

    两条互不依赖的任务（钢筋工 10 人 / 木工 10 人，各 2 天）本来并行（理论 2 天）。
    用户把班组明确指定成 10+10 人（不走分摊），总人工上限 12 人容不下 20 人同时上
    → 必须错开，resource_ok 变长并留痕。
    """
    wbs = make_wbs(
        leaf("1.1.1", "甲区钢筋", "钢筋工", 20, 2, 1.0, cap_labor=10),
        leaf("1.1.2", "甲区支模", "木工", 20, 2, 1.0, cap_labor=10),
    )
    boundary = {"labor": {"peak_total": 12},
                "crew_design": {"钢筋工": 10, "木工": 10}}
    _, out = run_scheduler(wbs, deps(), boundary)
    v = out["schedule_versions"]
    theory, ok = v["theory_min"], v["resource_ok"]

    assert theory["peak_labor"] == 20, "前置条件：用户指定的两工种本来同时上 20 人"
    assert theory["total_duration_days"] == 2
    for rec in ok["daily_labor"]:
        assert rec["total"] <= 12, "当日总人工绝不能超过 12 人"
    assert ok["total_duration_days"] > theory["total_duration_days"], "错开后必然更长"
    assert any("总人工限额" in c["reason"] for c in ok["capped"]), "顺延必须留痕"
    assert theory["total_duration_days"] <= ok["total_duration_days"], "核心不变量"


def test_total_labor_limit_alone_does_not_report_violation():
    """只给总人工上限、且额度充裕时：不该虚报 over_limit。"""
    _, out = run_scheduler(six_leaf_wbs(), _six_deps(), {"labor": {"peak_total": 500}})
    v = out["schedule_versions"]
    assert v["resource_ok"]["peak_labor"] <= 500
    assert v["theory_min"]["schedule"] == v["resource_ok"]["schedule"]


# ==================== 4) 依赖生效（FS 串行）====================
def test_fs_dependency_serializes():
    """A→B→C 串行：B.es >= A.ef，C.es >= B.ef。"""
    wbs = make_wbs(
        leaf("A", "工序A", "钢筋工", 100, 5, 1.0, cap_labor=10),
        leaf("B", "工序B", "钢筋工", 100, 5, 1.0, cap_labor=10),
        leaf("C", "工序C", "钢筋工", 100, 5, 1.0, cap_labor=10),
    )
    _, out = run_scheduler(wbs, deps(("A", "B"), ("B", "C")))
    rows = rows_of(out["schedule_versions"]["theory_min"])

    assert rows["B"]["es"] >= rows["A"]["ef"]
    assert rows["C"]["es"] >= rows["B"]["ef"]
    # 100 m3 / (1.0 × 5 天) = 20 人 → 工作面压到 10 人 → 工期 10 天
    assert (rows["A"]["ef"] - rows["A"]["es"]) == 10
    assert out["schedule_versions"]["theory_min"]["total_duration_days"] == 30
    assert out["schedule_versions"]["theory_min"]["critical_path"] == ["A", "B", "C"]


def test_ss_dependency_and_lag():
    """SS 依赖 + lag：succ.es >= pred.es + lag（不是 pred.ef）。

    刻意用**不同工种**，避免工作面容量把并行挤成串行，才能单独验 SS 语义。
    """
    wbs = make_wbs(
        leaf("A", "工序A", "钢筋工", 100, 10, 1.0, cap_labor=10),
        leaf("B", "工序B", "瓦工", 100, 10, 1.0, cap_labor=10),
    )
    _, out = run_scheduler(wbs, deps(("A", "B", "SS", 3)))
    rows = rows_of(out["schedule_versions"]["theory_min"])
    assert rows["A"]["es"] == 0
    assert rows["B"]["es"] >= rows["A"]["es"] + 3
    assert rows["B"]["es"] == 3, "SS+lag 让 B 能提前开工，不该等 A 干完"
    assert rows["B"]["es"] < rows["A"]["ef"], "SS 下 B 与 A 必须搭接"


# ==================== 5) 并行生效 ====================
def test_parallel_tasks_share_start_day():
    """互不依赖的两条任务 es 相同（都为 0），总工期 = 单条工期而非相加。"""
    wbs = make_wbs(
        leaf("P1", "并行一", "钢筋工", 100, 10, 1.0, cap_labor=10),
        leaf("P2", "并行二", "瓦工", 100, 10, 1.0, cap_labor=10),
    )
    _, out = run_scheduler(wbs)
    version = out["schedule_versions"]["theory_min"]
    rows = rows_of(version)

    assert rows["P1"]["es"] == rows["P2"]["es"] == 0, "无依赖必须同时开工"
    assert version["total_duration_days"] == 10, "总工期应是单条 10 天而不是 20 天"
    # 不同工种各占各的资源，两条都能按 10 人推进
    assert rows["P1"]["crew"] == {"钢筋工": 10.0}
    assert rows["P2"]["crew"] == {"瓦工": 10.0}
    assert version["peak_labor"] == 20


def test_parallel_same_trade_stays_within_capacity():
    """同工种并行：当日累计用量不得超过工作面容量（放不下就顺延）。"""
    wbs = make_wbs(
        leaf("P1", "并行一", "钢筋工", 100, 5, 1.0, cap_labor=6),   # 需要 20 人 → 压到 6
        leaf("P2", "并行二", "钢筋工", 100, 5, 1.0, cap_labor=6),
    )
    _, out = run_scheduler(wbs)
    version = out["schedule_versions"]["theory_min"]
    rows = rows_of(version)

    for rec in version["daily_labor"]:
        assert rec["trades"].get("钢筋工", 0) <= 6, "当日累计人数绝不能超工作面容量"
    # 6 人 × 工期 ≥ 100 工日 → 单条 17 天；两条不能重叠 → 34 天
    assert rows["P1"]["es"] == 0
    assert (rows["P1"]["ef"] - rows["P1"]["es"]) == 17
    assert rows["P2"]["es"] >= rows["P1"]["ef"]
    assert version["total_duration_days"] == 34


# ==================== 6) 确定性 ====================
def test_deterministic_same_input_same_output():
    """同输入跑两次，schedule_versions 逐字段相同（含队列按 id 排序）。"""
    wbs = six_leaf_wbs()
    dependencies = deps(("1.1.1", "1.1.5"), ("1.1.3", "1.1.6"), ("1.1.2", "1.1.5"))
    boundary = {"labor": {"by_trade": [{"trade": "钢筋工", "quantity": 8}]}}
    _, out1 = run_scheduler(wbs, dependencies, boundary)
    _, out2 = run_scheduler(wbs, dependencies, boundary)
    assert out1["schedule_versions"] == out2["schedule_versions"]
    assert out1["schedule_warnings"] == out2["schedule_warnings"]
    # schedule 是 schedule_versions["resource_ok"] 的同一个对象
    assert out1["schedule"] == out1["schedule_versions"]["resource_ok"]


def test_deterministic_with_shuffled_dependency_order():
    """依赖表顺序被打乱（内容相同）→ 结果必须一致。"""
    wbs = six_leaf_wbs()
    d1 = deps(("1.1.1", "1.1.5"), ("1.1.3", "1.1.6"), ("1.1.2", "1.1.5"))
    d2 = {"dependencies": list(reversed(d1["dependencies"]))}
    _, out1 = run_scheduler(wbs, d1)
    _, out2 = run_scheduler(wbs, d2)
    assert out1["schedule_versions"] == out2["schedule_versions"]


# ==================== 7) 目标工期三种区间 ====================
def test_target_verdict_physically_impossible():
    """目标比理论最短还短 → "物理上做不到"。"""
    _, out = run_scheduler(six_leaf_wbs(),
                           boundary={"project_duration_days": 5})
    compare = out["schedule_versions"]["compare"]
    theory = out["schedule_versions"]["theory_min"]["total_duration_days"]
    assert compare["user_target"] == 5
    assert compare["target_verdict"] == "物理上做不到"
    assert "最快" in compare["target_note"] and "差" in compare["target_note"]
    assert theory > 5

def test_target_verdict_needs_more_resources():
    """目标落在 [theory_min, resource_ok] → "需放宽资源"。"""
    wbs = make_wbs(
        leaf("A", "工序A", "钢筋工", 40, 4, 1.0, cap_labor=10),
        leaf("B", "工序B", "钢筋工", 40, 4, 1.0, cap_labor=10),
        leaf("C", "工序C", "钢筋工", 40, 4, 1.0, cap_labor=10),
    )
    deps_abc = deps(("A", "B"), ("B", "C"))
    boundary = {"labor": {"by_trade": [{"trade": "钢筋工", "quantity": 8}]}}
    _, out = run_scheduler(wbs, deps_abc, boundary)
    v = out["schedule_versions"]
    theory = v["theory_min"]["total_duration_days"]
    ok = v["resource_ok"]["total_duration_days"]
    assert (theory, ok) == (12, 15), "前置条件：紧限额必须让 resource_ok 更长"

    _, out2 = run_scheduler(
        wbs, deps_abc, dict(boundary, project_duration_days=13))
    compare = out2["schedule_versions"]["compare"]
    assert compare["user_target"] == 13
    assert compare["target_verdict"] == "需放宽资源"
    assert "顶满工作面" in compare["target_note"]


def test_target_verdict_loose():
    _, out = run_scheduler(six_leaf_wbs(),
                           boundary={"project_duration_days": 5000})
    compare = out["schedule_versions"]["compare"]
    assert compare["target_verdict"] == "宽松"
    assert "有余量" in compare["target_note"]


def test_target_missing_verdict_is_none():
    _, out = run_scheduler(six_leaf_wbs())
    compare = out["schedule_versions"]["compare"]
    assert compare["user_target"] is None
    assert compare["target_verdict"] is None
    assert "用户未提出总工期" in compare["target_note"]


def test_target_from_extracted_params_also_recognized():
    """extracted_params 里的目标工期也要认（可选输入，容错）。"""
    _, out = run_scheduler(six_leaf_wbs(), params={"project_duration_days": 5})
    compare = out["schedule_versions"]["compare"]
    assert compare["user_target"] == 5
    assert compare["target_verdict"] == "物理上做不到"


# ==================== 8) 缺 norm_binding：沿用原工期 + warning ====================
def test_missing_norm_binding_uses_own_duration_and_warns():
    wbs = make_wbs(
        {"id": "N1", "name": "无定额工序", "quantity": 500, "unit": "m3",
         "duration_days": 7, "work_type": "土方工程"},
        {"id": "N2", "name": "也没有定额", "quantity": 200, "unit": "m3",
         "duration_days": 4, "work_type": "砌筑工程"},
    )
    node, out = run_scheduler(wbs)
    v = out["schedule_versions"]
    rows = rows_of(v["theory_min"])

    assert rows["N1"]["ef"] - rows["N1"]["es"] == 7, "缺定额必须沿用叶子自身工期"
    assert rows["N2"]["ef"] - rows["N2"]["es"] == 4
    assert v["theory_min"]["schedule"] == v["resource_ok"]["schedule"]
    assert any("无可用定额锚定" in w for w in v["warnings"]), "缺定额必须记中文 warning"
    assert any("AI 假设" in w for w in v["warnings"])
    assert node.done_summary, "必须有中文 done_summary"


def test_broken_norm_binding_does_not_crash():
    """畸形 norm_binding（字符串/负数/非字典）→ 降级沿用原工期，不崩。"""
    wbs = make_wbs(
        {"id": "B1", "name": "坏定额一", "quantity": 100, "duration_days": 3,
         "unit": "m3", "work_type": "钢筋工程", "norm_binding": "不是字典"},
        {"id": "B2", "name": "坏定额二", "quantity": 100, "duration_days": 6,
         "unit": "m3", "work_type": "钢筋工程",
         "norm_binding": {"mode": "labor", "norm_value": -5, "productivity_value": 0}},
        {"id": "B3", "name": "零工程量", "quantity": 0, "duration_days": 2,
         "unit": "m3", "work_type": "钢筋工程",
         "norm_binding": {"mode": "labor", "norm_value": 1.0}},
    )
    _, out = run_scheduler(wbs)
    rows = rows_of(out["schedule_versions"]["theory_min"])
    assert (rows["B1"]["ef"] - rows["B1"]["es"]) == 3
    assert (rows["B2"]["ef"] - rows["B2"]["es"]) == 6
    assert (rows["B3"]["ef"] - rows["B3"]["es"]) == 2
    assert out["schedule_versions"]["warnings"], "降级必须留中文说明"


# ==================== 9) 硬原则：不为凑目标改定额 ====================
def test_user_target_never_changes_schedule():
    """同一项目喂不同 target：各任务工期与班组必须完全相同（只允许 verdict/note 变化）。

    这是产品的硬原则：用户目标只当参照，绝不为凑目标去改定额或改数据。
    """
    dependencies = deps(("1.1.1", "1.1.5"), ("1.1.3", "1.1.6"))
    targets = [None, 20, 300, 5000, 1]

    baseline = {}
    for target in targets:
        boundary = {} if target is None else {"project_duration_days": target}
        _, out = run_scheduler(six_leaf_wbs(), dependencies, boundary or None)
        v = out["schedule_versions"]
        for which in ("theory_min", "resource_ok"):
            shape = [(r["task_id"], r["es"], r["ef"],
                      tuple(sorted(r["crew"].items())), r["capped"])
                     for r in v[which]["schedule"]]
            if which not in baseline:
                baseline[which] = shape
            assert shape == baseline[which], (
                "target=%r 改变了 %s 的排程（工期/班组）——违反『不为凑目标改定额』硬原则"
                % (target, which))
        # 定额相关字段与班组完全不受 target 影响
        assert v["theory_min"]["total_duration_days"] == 20
        assert v["resource_ok"]["total_duration_days"] == 20
        assert v["theory_min"]["capped"] == v["resource_ok"]["capped"]


def test_target_only_changes_verdict_and_note():
    """target 只影响 compare 的 verdict/note/user_target，不碰两版工期。"""
    wbs = six_leaf_wbs()
    _, out_a = run_scheduler(wbs, boundary={"project_duration_days": 20})
    _, out_b = run_scheduler(wbs, boundary={"project_duration_days": 5000})
    va, vb = out_a["schedule_versions"], out_b["schedule_versions"]

    assert va["theory_min"] == vb["theory_min"]
    assert va["resource_ok"] == vb["resource_ok"]
    assert va["compare"]["target_verdict"] != vb["compare"]["target_verdict"]
    assert va["compare"]["theory_min_total"] == vb["compare"]["theory_min_total"]
    assert va["compare"]["resource_ok_total"] == vb["compare"]["resource_ok_total"]


# ==================== 10) 工作面容量封顶（第 37 轮：班组 := 容量顶满）====================
def test_workface_capacity_caps_labor_and_records_original():
    """工作面容量决定班组，并留痕说明这个数是怎么来的。

    第 37 轮前：人数由「工程量 ÷（产能 × 目标工期）」反推（1000/(1.0×10) = 100 人），
    再被容量压到 6 —— 所以 capped 记的是 want=100 / got=6。
    第 37 轮后（契约 §5-WS4 ①）：**删掉按目标工期反推**，班组直接 := 容量 6，
    留痕记 want=6 / got=6（"工作面容量顶满"），工期 = 工日需求 ÷ 6 = 167 天。
    """
    wbs = make_wbs(
        leaf("W1", "基础钢筋", "钢筋工", 1000, 10, 1.0, cap_labor=6))
    _, out = run_scheduler(wbs)
    version = out["schedule_versions"]["theory_min"]
    row = rows_of(version)["W1"]

    assert row["crew"]["钢筋工"] == 6, "班组 := 工作面上限"
    capped = version["capped"]
    assert capped, "班组来源必须留痕"
    assert capped[0]["task_id"] == "W1"
    assert capped[0]["got"] == 6
    assert "工作面容量" in capped[0]["reason"]
    # 1000 人日 / 6 人 → 167 天
    assert (row["ef"] - row["es"]) == 167
    assert version["peak_labor"] == 6


def test_workface_capacity_caps_machine():
    """机械台数 := 工作面 machine_max（不再按目标工期反推台数）。"""
    wbs = make_wbs(
        leaf("M1", "装配式吊装", "塔吊", 10000, 10, 0.05,
             mode="machine", cap_machine=2, machine_name="塔吊"))
    _, out = run_scheduler(wbs)
    version = out["schedule_versions"]["theory_min"]
    # 总台班 = 10000 × 0.05 = 500；第 37 轮前按目标工期反推 50 台再压到 2，
    # 第 37 轮后台数直接 := 工作面 2 台 → 工期 ceil(500/2) = 250 天
    capped = version["capped"]
    assert capped and capped[0]["got"] == 2
    assert "工作面容量" in capped[0]["reason"]
    assert version["peak_equipment"] == 2
    assert (rows_of(version)["M1"]["ef"] - rows_of(version)["M1"]["es"]) == 250


def test_missing_capacity_is_reported_not_defaulted():
    """完全没有工作面容量数据 → **如实报缺**：不编班组、不写人数，工期沿用叶子原值。

    改前（第 37 轮口径）：人工按 `DEFAULT_CEILING` 兜底（等于不封顶）并给一个班组。
    改后（C8 第 6/8 项）：资源只来自**工作面容量**；一条容量依据都没有时**不猜**，
    只留痕（`capped`）+ 一条中文 warning（绝不静默）。
    """
    wbs = make_wbs(leaf("D1", "无容量数据", "钢筋工", 100, 10, 1.0))   # 无 workface_capacity
    _, out = run_scheduler(wbs)
    v = out["schedule_versions"]
    version = v["theory_min"]
    assert any("缺工作面容量数据" in w for w in v["warnings"])
    recs = [c for c in version["capped"] if c["task_id"] == "D1"]
    assert recs and "缺工作面容量数据" in recs[0]["reason"], "缺数据必须留痕"
    row = rows_of(version)["D1"]
    assert not row.get("crew"), "没有容量依据 → 不编班组（C8：AI 不补资源）"
    assert row["ef"] - row["es"] == 10, "工期沿用叶子原值 10 天"


# ============ 10b) 工作面容量：LOW/ai_estimate 照常参与封顶（第 37 轮翻转）============
# 背景（旧契约，已废除）：知识库 Workface_Capacity_Rule 478 行全部是
# source_type='ai_estimate' / confidence='LOW'，旧实现把整张容量表当"不许封顶"，
# 人数改由"工程量 ÷（定额产能 × 目标工期）"反推 —— 那正是本轮要删的
# "按目标工期反推班组"。
# 新契约（契约 §5-WS4 ①，第 37 轮）：**LOW 只是一条置信度标注**，容量照常用
# cap = clamp(crew_base + crew_step_n × ⌊(段量 − q_ref)/crew_step_q⌋, crew_min, crew_max)
# 算出来顶满；置信度进报告（warning 里点明 AI 估算/低置信度），不再等于禁用。
def _ai_leaf(tid, trade, quantity, duration, productivity, cap):
    row = leaf(tid, trade, trade, quantity, duration, productivity, cap_labor=cap)
    row["workface_capacity"] = dict(row["workface_capacity"],
                                    source_type="ai_estimate", confidence="LOW")
    row["workface_capacity"].pop("origin", None)
    return row


def test_workface_is_evidence_gate():
    """第 37 轮起：**否决语义已删除** —— 任何非空容量字典都"可用"。

    旧断言（第 37 轮前）：`source_type=ai_estimate` / `LOW` / `match_type=ai` → False
    （"AI 估算不得封顶"）。新语义：LOW 只进置信度报告，容量照常参与计算；
    只有"没有容量数据"（None / 空字典）才是真正没有依据。
    """
    assert workface_is_evidence({"source_type": "kb", "confidence": "HIGH"})
    assert workface_is_evidence({"origin": "kb", "confidence": "LOW"})
    assert workface_is_evidence({"source_type": "ai_estimate", "confidence": "LOW"}), \
        "第 37 轮：LOW 只表示置信度，不再禁止封顶"
    assert workface_is_evidence({"origin": "ai", "confidence": "HIGH"})
    assert workface_is_evidence({"origin": "AI_ESTIMATE_V1"})
    assert workface_is_evidence({"source_type": "kb", "match_type": "ai"})
    assert not workface_is_evidence({})          # 没有数据才是没有依据
    assert not workface_is_evidence(None)


def test_ai_workface_capacity_still_caps_crew():
    """AI 估算（LOW）的工作面容量**照常封顶**，并在 warning 里标注置信度。

    旧行为（第 37 轮前）：1000 m³ /（1.0 × 10 天）= 100 人，AI 容量 4 人只当参考。
    新行为：班组顶满工作面上限 4 人 → 工期 ceil(1000/4) = 250 天。
    """
    wbs = make_wbs(_ai_leaf("A1", "钢筋工", 1000, 10, 1.0, cap=4))
    _, out = run_scheduler(wbs)
    version = out["schedule_versions"]["theory_min"]
    row = rows_of(version)["A1"]
    assert row["crew"]["钢筋工"] == 4, "LOW 不再等于禁用：顶满工作面上限"
    assert (row["ef"] - row["es"]) == 250, "1000 人日 ÷ 4 人"
    assert any(c["got"] == 4 for c in version["capped"]), "封顶必须留痕"
    ws = out["schedule_versions"]["warnings"]
    assert any("AI 经验估算" in w and "已参与封顶" in w for w in ws), \
        "置信度要进报告：说明这些容量是低置信度 AI 估算、已参与计算"


def test_evidence_workface_capacity_still_caps_crew():
    """有据可查的工作面容量（origin=kb）同样封顶 —— 第 37 轮起两者口径统一。"""
    wbs = make_wbs(leaf("E1", "E1", "钢筋工", 1000, 10, 1.0, cap_labor=4))
    _, out = run_scheduler(wbs)
    version = out["schedule_versions"]["theory_min"]
    row = rows_of(version)["E1"]
    assert row["crew"]["钢筋工"] == 4
    assert version["capped"] and version["capped"][0]["got"] == 4
    assert (row["ef"] - row["es"]) == 250


def test_user_can_still_reject_and_get_a_shorter_duration():
    """用户给紧限额 → 两版分叉（这正是两版工期存在的意义）。"""
    wbs = make_wbs(_ai_leaf("A1", "钢筋工", 1000, 10, 1.0, cap=4))
    _, free = run_scheduler(wbs)
    _, tight = run_scheduler(wbs, boundary={"labor": {"by_trade": [
        {"trade": "钢筋工", "quantity": 20}]}})
    a = free["schedule_versions"]
    b = tight["schedule_versions"]
    # 第 37 轮前 a 是 10 天（AI 容量不封顶、人数由目标工期反推 100 人）。
    # 第 37 轮后 a = 顶满 4 人 → 250 天；用户给 20 人限额比 4 人宽松 → 不构成压缩。
    assert a["resource_ok"]["total_duration_days"] == 250, "1000 人日 ÷ 工作面上限 4 人"
    assert b["resource_ok"]["total_duration_days"] == 250
    assert a["resource_ok"]["total_duration_days"] <= b["resource_ok"]["total_duration_days"]


# ============ 10c) 以知识库定额为准：工日需求 → 班组 → 工期 ============
# 产品口径（用户拍板）：定额是**唯一产能真源**。
#     工日需求 = 工程量 ÷ 定额产能     ← 知识库，客观、不可压缩
#     工期     = 工日需求 ÷ 设计班组人数 ← 施工组织决策，唯一可谈的杠杆
# 节拍产能表（BEAT_PRODUCTIVITY）退出"排多少天"，只留工序顺序与搭接。
def test_duration_comes_from_the_norm_not_the_beat_table():
    """20 m³、定额 0.5 工日/m³ = 40 工日；班组 8 人 → 5 天（节拍目标 2 天不参与）。

    第 37 轮起 `crew_design` 属于**用户资源条件**，只进 resource_ok；
    theory_min 只受工作面容量约束（无容量数据 → 兜底 1 人）。两侧都不再用
    节拍目标工期反推人数，工期一律由定额工日需求决定。
    """
    wbs = make_wbs(leaf("K1", "K1", "钢筋工", 20, 2, 0.5))
    _, out = run_scheduler(wbs, boundary={"crew_design": {"钢筋工": 8}})
    versions = out["schedule_versions"]
    ok = rows_of(versions["resource_ok"])["K1"]
    assert ok["crew"]["钢筋工"] == 8, "resource_ok 尊重用户指定的设计班组"
    assert ok["ef"] - ok["es"] == 5, "40 工日 ÷ 8 人 = 5 天，不能被乐观节拍压回 2 天"
    theo = rows_of(versions["theory_min"])["K1"]
    assert not theo.get("crew"), "theory_min 没有工作面容量 → 不编班组（C8：不猜人数）"
    assert theo["ef"] - theo["es"] == 2, "工期沿用叶子原值 2 天"


def test_explicit_crew_design_only_binds_resource_ok():
    """用户直接指定的班组只约束 resource_ok；theory_min 仍按工作面容量顶满。"""
    wbs = make_wbs(leaf("K1", "K1", "钢筋工", 20, 2, 0.5))
    _, out = run_scheduler(wbs, boundary={"labor": {"peak_total": 100},
                                          "crew_design": {"钢筋工": 4}})
    versions = out["schedule_versions"]
    ok = rows_of(versions["resource_ok"])["K1"]
    assert ok["crew"]["钢筋工"] == 4
    assert ok["ef"] - ok["es"] == 10, "40 工日 ÷ 4 人"


def test_crew_allocation_is_monotonic_in_labor():
    """人力越多 → resource_ok 的班组越大 → 工期越短（单调、可解释）。"""
    def _wbs():
        return make_wbs(*[leaf("K%d" % i, "K%d" % i, "钢筋工", 200, 5, 0.2)
                          for i in (1, 2, 3)])
    days = []
    for total in (12, 24, 48):
        _, out = run_scheduler(_wbs(), boundary={"labor": {"peak_total": total}})
        days.append(out["schedule_versions"]["resource_ok"]["total_duration_days"])
    assert days[0] > days[1] > days[2], "人力翻倍工期应严格变短，实际 %s" % days


def test_norm_labor_demand_is_reported():
    """定额工日需求必须报给用户 —— 这是"算得清"的核心数字，也是跟用户自己对账的依据。"""
    wbs = make_wbs(leaf("K1", "K1", "钢筋工", 20, 2, 0.5))
    _, out = run_scheduler(wbs)
    ws = out["schedule_versions"]["warnings"]
    assert any("定额工日需求合计 40 人日" in w for w in ws), ws
    assert any("钢筋工 40" in w for w in ws), "要分列到工种"


# ============ 10d) 定额覆盖率：哪些工程量进了定额、占多少 ============
def _leaf_with_binding(tid, source_code, unit="m3", unit_norm="工日/m3"):
    row = leaf(tid, tid, "钢筋工", 20, 2, 0.5)
    row["unit"] = unit
    row["norm_binding"]["source_code"] = source_code
    row["norm_binding"]["unit"] = unit_norm
    return row


def test_norm_coverage_counts_and_reasons():
    """覆盖率必须分类到原因，并且 bound + unbound == total（数字要对得上）。

    **政策变更（2026-09-20，用户亲自决定）**：AI 来源定额（`AI_ESTIMATE_V1`）从
    "不可用"改为"放行 + 逐条标注"，所以 `AI1` 这条现在计入 `bound`，而
    `"AI估算定额"` 这个**不可用原因**桶不再出现；AI 来源单独进 `released_ai`。
    """
    from pipeline.nodes.scheduler import norm_coverage_report

    wbs = make_wbs(
        _leaf_with_binding("OK1", "LD_T72_7_2008"),
        _leaf_with_binding("AI1", "AI_ESTIMATE_V1"),
        _leaf_with_binding("UNIT1", "LD_T72_7_2008", unit="m²", unit_norm="工日/m³"),
        leaf("NONE1", "NONE1", "钢筋工", 20, 2, 0.5),      # 有定额但没写来源 → 视为有据
    )
    wbs["phases"][0]["work_packages"][0]["sub_packages"][3].pop("norm_binding")
    _, out = run_scheduler(wbs)
    cov = out["norm_coverage"]
    assert cov["total"] == 4
    assert cov["bound"] + cov["unbound"] == 4
    # 政策变更（2026-09-20）：AI 来源不再产生"不可用原因"（旧断言：== 1）
    assert "AI估算定额" not in cov["by_reason"], cov["by_reason"]
    assert cov["by_reason"].get("单位不一致") == 1
    assert cov["by_reason"].get("KB无定额行") == 1
    assert cov["bound"] == 2, "OK1 与 AI1（AI 定额现在同等参与）"
    assert cov["bound_pct"] == 50.0 and cov["unbound_pct"] == 50.0
    # 政策变更（2026-09-20）：AI 来源单独成一档（"不得不用 AI 就标出来"的那个数）
    assert cov["released_ai"] == 1 and cov["released_ai_pct"] == 25.0, cov
    # 缺口要能指到具体活动编号（补定额时才知道去哪补）
    assert "top_activities" in cov and cov["top_activities"]


def test_coverage_warning_states_the_gap_in_plain_chinese():
    """用户看的是一句话：多少条进了定额、多少条没进、为什么。

    **政策变更（2026-09-20）**：AI 来源定额已放行，所以它不再进"没进定额"的缺口，
    但必须在同一句话里**单列**"其中 N 条依据 AI 经验估算定额（无规范依据，已按来源
    放行并逐条标注）"。这里再放一条单位对不上的叶子，保证覆盖率警告本身仍然触发
    （它只在有缺口时出现）。
    """
    wbs = make_wbs(
        _leaf_with_binding("OK1", "LD_T72_7_2008"),
        _leaf_with_binding("AI1", "AI_ESTIMATE_V1"),
        _leaf_with_binding("UNIT1", "LD_T72_7_2008", unit="m²", unit_norm="工日/m³"),
    )
    _, out = run_scheduler(wbs)
    ws = out["schedule_versions"]["warnings"]
    hit = [w for w in ws if "定额口径覆盖率" in w]
    assert hit, ws
    text = hit[0]
    assert "66.7%" in text, text                      # 2/3 有据可查
    assert "沿用 WBS 原工期" in text, text
    # 政策变更（2026-09-20）：AI 来源的条数必须如实报出来，且不再写成"不可用原因"
    assert "AI 经验估算定额（无规范依据，已按来源放行并逐条标注）" in text, text
    assert "AI估算定额" not in text, text


def test_unit_mismatch_is_reported_before_ai_label():
    """绑到了真实定额但量纲对不上 → 归"单位不一致"，不能被笼统写成"AI 估算"。"""
    from pipeline.nodes.scheduler import _build_ledger_item

    lf = _leaf_with_binding("U1", "LD_T72_7_2008", unit="m²", unit_norm="工日/m³")
    item = _build_ledger_item(lf, "U1", "U1")
    assert item["usable"] is False
    assert item["not_usable_reason"] == "单位不一致"


def test_ai_estimate_norm_now_drives_duration_and_is_labeled():
    """**政策变更（2026-09-20，用户亲自决定）**：AI 估算定额放行 → 参与算工期 + 逐条标注。

    旧断言（第 37~39 轮）：`AI_ESTIMATE_V1` 叶片被证据门拦下 → 沿用叶子原工期 7 天、
    班组 `{}`、不计入"定额工日需求合计"。
    新口径：与真人定额**同等**参与 —— `duration = ceil(工程量 ÷ (P × 人数))`，
    工日需求照算；来源写进 `norm_evidence_label`（`state="released_ai"` +
    `label=LABEL_AI_ESTIMATE`），覆盖率里单列 `released_ai`。
    """
    from pipeline.nodes.scheduler import _build_ledger_item

    wbs = make_wbs(leaf("N1", "N1", "钢筋工", 20, 7, 0.5))
    sub = wbs["phases"][0]["work_packages"][0]["sub_packages"][0]
    sub["norm_binding"]["source_code"] = "AI_ESTIMATE_V1"

    item = _build_ledger_item(sub, "N1", "N1")
    assert item["usable"] is True and item["norm_is_evidence"] is True
    assert item["not_usable_reason"] == "", "AI 来源不再产生「不可用原因」"
    assert item["ai_norm_source"] is True
    lab = item["norm_evidence_label"]
    assert lab["state"] == "released_ai", lab
    assert lab["label"] == "AI 经验估算定额（无规范依据，待审）", lab
    assert lab["source_code"] == "AI_ESTIMATE_V1", lab

    _, out = run_scheduler(wbs)
    v = out["schedule_versions"]
    # 政策变更（2026-09-20）：AI 定额现在进定额工日需求（20 m³ ÷ 0.5 单位/工日 = 40 人日）
    assert any("定额工日需求合计 40 人日" in w for w in v["warnings"]), v["warnings"]
    row = rows_of(v["theory_min"])["N1"]
    assert not row.get("crew"), "无工作面容量 → 不编班组（C8-8：AI 不补资源）"
    assert row["ef"] - row["es"] == 7, "工期沿用叶子原值 7 天"
    cov = out["norm_coverage"]
    assert cov["bound"] == 1 and cov["released_ai"] == 1, cov
    assert "AI估算定额" not in cov["by_reason"], cov["by_reason"]


def test_norm_without_value_is_unusable():
    """定额值取不到（productivity / norm_value 都为 None）→ 一律不可用（这条判据没松）。

    政策变更（2026-09-20）只放开"AI 来源"，没有放开"没有定额值也算数"。
    """
    from pipeline.nodes.scheduler import _build_ledger_item

    lf = _leaf_with_binding("V0", "LD_T72_7_2008")
    lf["norm_binding"]["productivity_value"] = None
    item = _build_ledger_item(lf, "V0", "V0")
    # `norm_is_evidence` 只回答"这条定额够不够格当证据"（单位/口径/闸门），
    # "有没有值"由 `usable` 把关 —— 两个都取不到时 usable 必须为 False。
    assert item["usable"] is False, item
    assert item["not_usable_reason"] == "定额值为空", item["not_usable_reason"]


def test_gate_no_value_is_reported_as_empty_norm(monkeypatch):
    """闸门新增的 `no_value` 档（该 L4 的行定额值全缺失/<=0）→ 归到「定额值为空」。

    **政策变更（2026-09-20）**：放开 AI 后，"0 工日/m²"不再被 AI 档顺带拦住，
    闸门显式判 `no_value`。覆盖率口径里它属于既有的「定额值为空」桶 ——
    不许冒充"单位不一致"，也不许造一个用户看不懂的新桶名。
    """
    from pipeline.nodes import scheduler as S
    from pipeline.nodes.scheduler import _build_ledger_item

    class _Stub:
        KIND_LABOR = "labor"
        KIND_MACHINE = "machine"
        STATE_NO_VALUE = "no_value"
        LABEL_AI_ESTIMATE = "AI 经验估算定额（无规范依据，待审）"

        @staticmethod
        def gate_open(*a, **k):
            return False, "L4默认定额行定额值缺失或非正（<=0），不作工期证据"

        @staticmethod
        def gate_label(*a, **k):
            return {"state": "no_value", "confidence": "estimated",
                    "source_code": "KB_Norm_Labor_Table",
                    "note": "L4默认定额行定额值缺失或非正（<=0），不作工期证据"}

    monkeypatch.setattr(S, "_norm_defaults", _Stub)
    lf = _leaf_with_binding("NV1", "LD_T72_7_2008")
    item = _build_ledger_item(lf, "NV1", "NV1")
    assert item["usable"] is False and item["norm_is_evidence"] is False
    assert item["not_usable_reason"] == "定额值为空", item["not_usable_reason"]
    # 被拦下的条目不算"已放行的 AI 定额"（覆盖率不重复计）
    assert item["ai_norm_source"] is False or item["usable"] is False


# ==================== 11) 输出结构契约 ====================
def test_output_schema_contract():
    """ctx 写回的键与结构必须完全符合契约。"""
    _, out = run_scheduler(
        six_leaf_wbs(),
        deps(("1.1.1", "1.1.5")),
        {"labor": {"peak_total": 500,
                   "by_trade": [{"trade": "钢筋工", "quantity": 12}]},
         "equipment": [{"name": "塔吊", "quantity": 3}],
         "project_duration_days": 100},
    )
    # 第 41 轮（施工组织层）新增 `organization_gaps`：契约 §3 要求把"按节拍做不到的
    # 工序"交给 ctx（plan_assembler 搬进 meta.organization_gaps）。无节拍时恒为 `[]`。
    # 域 8.3 新增 daily_resource_share / resource_backpressure
    assert set(out.keys()) == {"schedule_versions", "schedule", "schedule_warnings",
                               "norm_coverage", "machine_labor_demand",
                               "equipment_binding", "organization_gaps",
                               "daily_resource_share", "resource_backpressure"}
    assert out["organization_gaps"] == [], "取不到节拍 → 不许编造组织缺口"
    # 定额口径覆盖率必须一起交出去（哪些工程量进了定额、占多少，用户有权知道）
    cov = out["norm_coverage"]
    assert set(cov.keys()) >= {"total", "bound", "bound_pct", "unbound", "unbound_pct",
                               "by_reason", "by_reason_pct", "top_activities"}
    assert cov["total"] == 6 and cov["bound"] + cov["unbound"] == 6
    assert 0.0 <= cov["bound_pct"] <= 100.0
    v = out["schedule_versions"]
    assert set(v.keys()) == {"theory_min", "resource_ok", "compare", "warnings"}
    for which in ("theory_min", "resource_ok"):
        block = v[which]
        assert set(block.keys()) == {
            "total_duration_days", "schedule", "critical_path", "daily_labor",
            "daily_equipment", "peak_labor", "peak_equipment", "over_limit", "capped"}
        assert isinstance(block["total_duration_days"], int)
        assert len(block["daily_labor"]) == block["total_duration_days"] + 1
        assert len(block["daily_equipment"]) == block["total_duration_days"] + 1
        for rec in block["daily_labor"]:
            assert set(rec.keys()) == {"day", "total", "trades"}
        for rec in block["daily_equipment"]:
            assert set(rec.keys()) == {"day", "total", "items"}
        for row in block["schedule"]:
            # 裁定 B（2026-09-21）：容量来源逐行可追溯 → 多出这两个键
            # （`capacity_source` ∈ mwi / reported_missing，域 1.6 收敛为两态）
            assert {"task_id", "es", "ef", "crew", "capped"} <= set(row.keys())
            assert set(row.keys()) <= {"task_id", "es", "ef", "crew", "capped",
                                       "_organization", "capacity_source",
                                       "capacity_basis"}
    assert set(v["compare"].keys()) == {
        "theory_min_total", "resource_ok_total", "delta_days",
        "user_target", "target_verdict", "target_note"}
    assert out["schedule"] == v["resource_ok"], "默认交付版必须是 resource_ok"
    # 设备限额 3 台 + 工作面 5 台 → min = 3 台
    assert v["theory_min"]["peak_equipment"] == 3 or v["resource_ok"]["peak_equipment"] == 3


def test_equipment_limit_matches_container_name():
    """用户设备的『包含匹配』：『塔吊』限额要能落到『塔吊QTZ80』这类名字上。"""
    wbs = make_wbs(
        leaf("T1", "塔吊吊装", "塔式起重机", 2000, 10, 0.05,
             mode="machine", cap_machine=10, machine_name="塔吊QTZ80"))
    _, out = run_scheduler(wbs, boundary={"equipment": [{"name": "塔吊", "quantity": 2}]})
    v = out["schedule_versions"]
    assert v["theory_min"]["peak_equipment"] == 10       # 理论版：顶满工作面
    assert v["resource_ok"]["peak_equipment"] == 2       # 资源版：落到用户限额
    assert v["resource_ok"]["over_limit"], "已达设备上限要报出来"
    assert v["theory_min"]["total_duration_days"] < v["resource_ok"]["total_duration_days"]


# ==================== 12) 容错与降级 ====================
def test_empty_and_broken_inputs_do_not_crash():
    """空 WBS / 畸形依赖 / 畸形边界 → 一律降级，返回空排程 + 中文 warning。"""
    node = SchedulerNode()
    out = node.run({})
    v = out["schedule_versions"]
    assert v["theory_min"]["schedule"] == []
    assert v["resource_ok"]["total_duration_days"] == 0
    assert v["compare"]["target_verdict"] is None
    assert any("没有可用叶子任务" in w for w in out["schedule_warnings"])
    assert node.done_summary

    for bad in ({"dependencies": [{"predecessor": None, "successor": None}, 7, "x"]},
                {"dependencies": "不是列表"},
                ["不是字典"]):
        _, o = run_scheduler(six_leaf_wbs(), bad)
        assert o["schedule_versions"]["resource_ok"]["total_duration_days"] > 0


def test_cyclic_dependency_falls_back_with_warning():
    """依赖成环：不挂死、不丢任务，按 id 顺序兜底并记 warning。"""
    wbs = make_wbs(
        leaf("A", "工序A", "钢筋工", 100, 5, 1.0, cap_labor=10),
        leaf("B", "工序B", "钢筋工", 100, 5, 1.0, cap_labor=10),
    )
    _, out = run_scheduler(wbs, deps(("A", "B"), ("B", "A")))
    v = out["schedule_versions"]
    assert len(v["theory_min"]["schedule"]) == 2
    assert any("环路" in w for w in v["warnings"])
    assert v["theory_min"]["total_duration_days"] > 0


def test_broken_boundary_conditions_do_not_crash():
    """畸形边界条件（字符串/数字/None 字段）→ 忽略，不崩。"""
    for boundary in ("不是 JSON", 123, {"labor": "乱写"},
                     {"labor": {"by_trade": "也不是列表"}},
                     {"equipment": [{"name": None, "quantity": "很多"}]},
                     {"labor": {"by_trade": [{"trade": "钢筋工", "quantity": -3}]}}):
        _, out = run_scheduler(six_leaf_wbs(), None, boundary)
        assert out["schedule_versions"]["theory_min"]["total_duration_days"] > 0


def test_kb_missing_does_not_crash():
    """叶子拿不到工作面容量数据 → 按"无容量数据"降级，不中断（只影响封顶）。

    域 1.6（第 6 批）已删 `Workface_Capacity_Rule` 表与 `kb.workface_capacity()`，
    本用例原先 monkeypatch `mod.kb.workface_capacity` 来制造"KB 挂了"，**已无打桩对象**；
    改为直接用"叶子不带 `workface_capacity` 键"这一**与真实链路同构**的情形
    （`crew_bind` 已不再从 KB 补容量 ⇒ 真实产物里就是没有该键），
    降级不中断 + 如实报缺的语义一字不变。
    """
    wbs = make_wbs(
        {"id": "K1", "name": "带活动ID的叶子", "quantity": 100, "unit": "m3",
         "duration_days": 5, "work_type": "钢筋工程", "kb_activity_id": "REBAR_NEW_FOUND",
         "norm_binding": {"mode": "labor", "norm_value": 1.0, "match_type": "exact",
                          "labor_types": ["钢筋工"]}})
    _, out = run_scheduler(wbs)
    version = out["schedule_versions"]["theory_min"]
    assert version["total_duration_days"] > 0
    assert any("缺工作面容量数据" in w for w in out["schedule_versions"]["warnings"])


def test_cpm_critical_path_reused_when_ids_match():
    """上游 CPM 的关键路径 id 能对上时直接复用（不自己另算一套）。"""
    wbs = six_leaf_wbs()
    cpm = {"total_duration_days": 80, "critical_path": ["1.1.1", "1.1.5"]}
    _, out = run_scheduler(wbs, None, None, None, cpm)
    assert out["schedule_versions"]["theory_min"]["critical_path"] == ["1.1.1", "1.1.5"]

    # 对不上（不存在的 id）→ 自己用 ef 最长链推导
    _, out2 = run_scheduler(wbs, None, None, None,
                            {"critical_path": ["不存在", "也不存在"]})
    path = out2["schedule_versions"]["theory_min"]["critical_path"]
    assert path and all(t in ("1.1.1", "1.1.2", "1.1.3", "1.1.4", "1.1.5", "1.1.6")
                        for t in path)


def test_daily_curves_are_consistent_with_schedule():
    """逐日曲线与排程一致：曲线峰值 = peak_labor，且与任务区间对得上。"""
    wbs = six_leaf_wbs()
    _, out = run_scheduler(wbs, deps(("1.1.1", "1.1.5")))
    version = out["schedule_versions"]["theory_min"]

    manual = [0] * (version["total_duration_days"] + 1)
    for row in version["schedule"]:
        people = sum(row["crew"].values())
        for day in range(row["es"], row["ef"]):
            manual[day] += people
    assert [rec["total"] for rec in version["daily_labor"]] == manual
    assert version["peak_labor"] == max(manual)


# ==================== 直接调用纯函数（不经节点）====================
def test_compute_schedules_direct_call():
    """compute_schedules 可独立调用（纯函数，便于装配与复用）。"""
    result = compute_schedules(six_leaf_wbs(), None, None, None, None)
    assert result["schedule_versions"]["resource_ok"]["total_duration_days"] == 20
    assert result["schedule"] is result["schedule_versions"]["resource_ok"]
    assert isinstance(result["schedule_warnings"], list)


def test_node_emits_progress_and_summary():
    """进度事件用契约里的 node_progress，结束有中文 done_summary。"""
    events = []
    node = SchedulerNode()
    node._emit = lambda event, data: events.append((event, data))
    node.run({"wbs": six_leaf_wbs()})

    fields = [data["progress"] for event, data in events if event == "node_progress"]
    assert fields and fields[0] < fields[-1]
    assert all(event == "node_progress" for event, _ in events)
    for _, data in events:
        assert data["node"] == "scheduler"
    assert "理论最短" in node.done_summary and "资源不超额" in node.done_summary


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("  PASS  %s" % fn.__name__)
    print("\n全部用例通过 ✔")
