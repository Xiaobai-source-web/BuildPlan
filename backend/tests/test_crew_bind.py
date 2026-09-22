"""crew_bind（机械配员 / 工作面容量）+ resource 定额路径测试。

运行：python -m pytest backend/tests/test_crew_bind.py -v
      （也可直接 python 运行本文件）

只依赖随仓库附带的 BuildPlan_KB/kb.db，不联网、不调用 LLM。
覆盖：
1) 机械主导叶子 → 配员非空、crew_stats 统计正确
2) 无配员数据的机械 → crew 留空但给警告（不中断）
3) 域 1.6（第 6 批）已删 `Workface_Capacity_Rule` + `kb.workface_capacity()`：
   crew_bind **不再回查/写入** `leaf["workface_capacity"]`，
   `crew_stats` 只剩 `machines_total` / `machines_with_crew` 两键
4) composition 文本解析（"司机1名" → {"司机": 1}）
5) 遗留路径不变（无 norm_binding 的叶子输出 == 改动前基线）
6) 定额路径生效（labor：每日人数与手算一致）
7) 工作面容量封顶：**只认叶子自带** `workface_capacity`；无该键 → 不封顶
"""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline import kb
from pipeline.nodes import resource
from pipeline.nodes.crew_bind import CrewBindNode, parse_crew_composition


# ==================== 构造工具 ====================
def make_wbs(*leaves):
    """把若干叶子塞进一个最小的 wbs 结构。"""
    return {"phases": [{"phase": "测试相", "work_packages": [
        {"id": "P1", "name": "测试工作包", "sub_packages": list(leaves)}]}]}


def leaf_of(wbs):
    return wbs["phases"][0]["work_packages"][0]["sub_packages"][0]


def run_crew_bind(wbs):
    node = CrewBindNode()
    out = node.run({"wbs": wbs})
    return node, out


# KB 里确切存在的三个样本活动（数据随仓库附带，稳定）
ACT_WITH_CREW = "GD_A11_机械挖土方、淤泥流砂"   # 主控机械：履带式单斗液压挖掘机（司机1名）
ACT_NO_CREW = "GD_A13_旋挖成孔"                 # 主控机械：履带式旋挖钻机（配员表无此行）
ACT_LABOR = "REBAR_NEW_FOUND"                   # 基础钢筋（v2 标定：base=8 / q_ref=22 / step=10 / max=16）


# 域 1.6（第 6 批）：原先这里的 `formula_cap()` 辅助函数靠
# `scheduler.workface_limits_from_rule()` 从 KB 的 `Workface_Capacity_Rule`
# 按工程量反算上限。表与 `kb.workface_capacity()` 已删除，该函数只认**叶子自带**
# 的 `workface_capacity`，叶子没有该键就返回 `(None, None)` —— 辅助函数失去意义，
# 已随依赖它的用例一并删除。


# ==================== 1) 机械配员 ====================
def test_machine_leaf_gets_crew():
    """机械主导叶子 + KB 有配员的机械 → norm_binding["crew"] 非空且统计正确。"""
    wbs = make_wbs({
        "id": "T1", "name": "机械挖土方", "kb_activity_id": ACT_WITH_CREW,
        "duration_days": 25, "quantity": 96000, "unit": "m3", "work_type": "土方工程",
        "norm_binding": {"task_id": "T1", "mode": "machine", "norm_value": 0.012,
                         "quantity_basis": 1.0, "source_code": "GD_2018_A1_1",
                         "match_type": "exact"},
    })
    node, out = run_crew_bind(wbs)
    leaf = leaf_of(wbs)

    crew = leaf["norm_binding"]["crew"]
    assert crew, "机械主导叶子必须补上配员"
    assert crew.get("司机") == 1
    # 机械配员 vs 人工工种必须可区分（人工不重复计入机械配员）
    assert leaf["norm_binding"]["crew_kind"]["司机"] == "machine"
    assert leaf["norm_binding"]["machine_name"] == "履带式单斗液压挖掘机"

    src = leaf["crew_source"]
    assert src["origin"] == "kb"
    assert src["ref"] == "LD_T72"          # 来源：LD/T 72 配员表
    assert src["confidence"] == "HIGH"
    assert "司机" in src["note"]           # 配员原文保留

    assert out["crew_stats"]["machines_total"] >= 1
    assert out["crew_stats"]["machines_with_crew"] >= 1
    assert out["crew_warnings"] == []


def test_leaf_without_norm_binding_not_polluted():
    """只有 kb_activity_id、没有 norm_binding 的叶子：补配员，但绝不新建 norm_binding。

    （一旦新建 norm_binding，resource.py 就会切到定额路径，可能弄坏遗留输出。）
    域 1.6（第 6 批）：crew_bind **不再回查/写入**工作面容量，故这里断言叶子
    **没有** `workface_capacity` 键（旧断言 `is not None` 说的是已退役的 KB 补齐）。
    """
    wbs = make_wbs({
        "id": "T2", "name": "机械挖土方", "kb_activity_id": ACT_WITH_CREW,
        "duration_days": 25, "quantity": 96000, "unit": "m3", "work_type": "土方工程",
    })
    _, out = run_crew_bind(wbs)
    leaf = leaf_of(wbs)
    assert "norm_binding" not in leaf
    assert leaf["machine_crew"].get("司机") == 1
    assert "workface_capacity" not in leaf, \
        "域 1.6：crew_bind 已不再回查 KB 补工作面容量"
    assert out["crew_stats"]["machines_with_crew"] == 1


# ==================== 2) 无配员数据：警告而不中断 ====================
def test_machine_without_crew_data_warns():
    """配员表没覆盖的机械 → crew 留空 + crew_warnings 有记录，节点不抛异常。"""
    assert kb.crew_for_machine("履带式旋挖钻机") is None, "前置条件：该机械应无配员数据"

    wbs = make_wbs({
        "id": "T3", "name": "旋挖成孔", "kb_activity_id": ACT_NO_CREW,
        "duration_days": 10, "quantity": 500, "unit": "m3", "work_type": "桩基工程",
        "norm_binding": {"task_id": "T3", "mode": "machine", "norm_value": 0.5,
                         "source_code": "GD_2018_A3_1", "match_type": "default"},
    })
    _, out = run_crew_bind(wbs)          # 不中断
    leaf = leaf_of(wbs)

    assert leaf["norm_binding"]["crew"].get("司机") is None   # 没瞎编司机
    assert leaf["machine_crew"] == {}
    assert out["crew_warnings"], "无配员数据必须记警告，不能静默"
    warn = out["crew_warnings"][0]
    assert warn["machine"] == "履带式旋挖钻机"
    assert "无配员数据" in warn["reason"]
    assert out["crew_stats"]["machines_total"] == 1
    assert out["crew_stats"]["machines_with_crew"] == 0
    # 域 1.6：不再回查 KB 补工作面容量 → 叶子不带该键（降级但不停）
    assert "workface_capacity" not in leaf


# ==================== 3) 工作面容量：域 1.6 后只剩「叶子自带」 ====================
# 迁移测试说明（第 6 批）：本节原有 2 条用例
#   test_workface_capacity_confidence_kept（crew_bind 从 KB 补容量并原样带 confidence）
#   test_workface_missing_is_counted（KB 查不到容量 → crew_stats.workface_missing 计数）
# 断的都是已退役的「crew_bind 回查 KB 补容量」能力。第 6 批删表后 crew_bind 不再写
# `leaf["workface_capacity"]`，`crew_stats` 也去掉了两个 workface 键 → **整条删除**。
# 封顶能力本身没退役（只是上限只来自叶子自带），由第 7 节用例覆盖。


def test_unlinked_leaf_untouched():
    """完全没锚定 KB 的老叶子：crew_bind 一个字段都不补。"""
    wbs = make_wbs({
        "id": "T6", "name": "底板混凝土", "duration_days": 15,
        "quantity": 15600, "unit": "m³", "work_type": "混凝土工程",
    })
    before = dict(leaf_of(wbs))
    _, out = run_crew_bind(wbs)
    assert leaf_of(wbs) == before
    # 域 1.6：`crew_stats` 只剩 machines_* 两键（workface_* 已随 KB 回查退役）
    assert out["crew_stats"] == {"machines_total": 0, "machines_with_crew": 0}


# ==================== 4) 配员文本解析 ====================
@pytest.mark.parametrize("text,expected", [
    ("司机1名", {"司机": 1}),
    ("操作工1名", {"操作工": 1}),
    ("振捣工1人", {"振捣工": 1}),
    ("泵工1人+辅助1人", {"泵工": 1, "辅助": 1}),
    ("司机1名+信号工2名", {"司机": 1, "信号工": 2}),
    ("信号工1名、司机1名", {"信号工": 1, "司机": 1}),
    ("", {}),
    (None, {}),
    ("若干人", {}),        # 解析不出来 → 空（调用方须保留原文）
    ("人", {}),
])
def test_parse_crew_composition(text, expected):
    assert parse_crew_composition(text) == expected


def test_parse_crew_composition_matches_kb_rows():
    """真实 KB 配员行的 composition 都能解析出人数。"""
    for machine in ("履带式单斗液压挖掘机", "自卸汽车", "履带式推土机"):
        row = kb.crew_for_machine(machine)
        assert row is not None, machine
        crew = parse_crew_composition(row["composition"])
        assert crew, "%s 的 composition=%r 解析失败" % (machine, row["composition"])
        assert sum(crew.values()) >= 1


# ==================== 5) 遗留路径不变 ====================
# 基线：改动前（旧硬编码产能表路径）compute_flat 的真实输出，逐字段抄录。
LEGACY_WBS = {"phases": [{"phase": "地下结构", "work_packages": [
    {"id": "2.2", "name": "底板与桩基", "sub_packages": [
        {"id": "2.2.1", "name": "底板混凝土", "duration_days": 15,
         "quantity": 15600, "unit": "m³", "work_type": "混凝土工程"},
        {"id": "2.2.2", "name": "底板钢筋", "duration_days": 20,
         "quantity": 800, "unit": "吨", "work_type": "钢筋工程"}]}]}]}

LEGACY_EXPECTED = {"resource_demand": {"tasks": [
    {"_matched_keyword": "底板混凝土", "planned_duration_days": 15,
     "quantity": 15600.0, "task_id": "2.2.1", "task_name": "底板混凝土",
     "泵车_per_day": 13, "泵车_total_days": 195,
     "混凝土工_per_day": 52, "混凝土工_total_days": 780},
    {"_matched_keyword": "底板钢筋", "planned_duration_days": 20,
     "quantity": 800.0, "task_id": "2.2.2", "task_name": "底板钢筋",
     "钢筋工_per_day": 27, "钢筋工_total_days": 540},
]}}


def test_legacy_path_unchanged():
    """无 norm_binding 的叶子 → 输出与改动前逐字段一致（含不新增任何顶层键）。"""
    got = resource.compute_flat(LEGACY_WBS, {"total_concrete": 15600, "total_rebar": 800}, None)
    assert got == LEGACY_EXPECTED
    assert "resource_provenance" not in got
    assert "_norm_path_count" not in got["resource_demand"]


def test_legacy_path_unchanged_when_binding_unusable():
    """有 norm_binding 但定额不可用（norm_value=0/None）→ 仍然走遗留路径。"""
    wbs = make_wbs({
        "id": "2.2.1", "name": "底板混凝土", "duration_days": 15,
        "quantity": 15600, "unit": "m³", "work_type": "混凝土工程",
        "norm_binding": {"task_id": "2.2.1", "mode": "labor", "norm_value": 0,
                         "source_code": "", "match_type": "ai"},
    })
    got = resource.compute_flat(wbs, None, None)
    task = got["resource_demand"]["tasks"][0]
    assert task["混凝土工_per_day"] == 52 and task["泵车_per_day"] == 13
    assert "_norm_applied" not in task
    assert "resource_provenance" not in got
    assert not resource.norm_binding_usable({"mode": "labor", "norm_value": 0})


# ==================== 6) 定额路径生效（labor） ====================
def test_norm_labor_path_computes_real_norm():
    """labor 定额：P=1/norm_value=0.25 t/工日，100 t / 10 天 → 40 人/天。"""
    wbs = make_wbs({
        "id": "N1", "name": "基础钢筋", "duration_days": 10,
        "quantity": 100, "unit": "t", "work_type": "钢筋工程",
        "norm_binding": {"task_id": "N1", "mode": "labor", "norm_value": 4.0,
                         "quantity_basis": 1.0, "source_code": "LD_T72_7_2008",
                         "match_type": "exact", "labor_types": ["钢筋工"],
                         "crew": {"钢筋工": 1}, "crew_kind": {"钢筋工": "labor"}},
    })
    flat = resource.compute_flat(wbs, None, None)
    task = flat["resource_demand"]["tasks"][0]

    # 手算：每人每天产量 P = 1/4 = 0.25 t；每日工程量 10 t；⌈10/0.25⌉ = 40 人/天
    assert task["钢筋工_per_day"] == 40
    assert task["钢筋工_total_days"] == 400          # 40 人 × 10 天
    assert task["_resource_source"]["钢筋工"] == {"origin": "kb", "ref": "LD_T72_7_2008"}
    assert task["_norm_applied"]["mode"] == "labor"
    assert flat["resource_provenance"]["N1"]["钢筋工"]["ref"] == "LD_T72_7_2008"
    assert flat["resource_demand"]["_norm_path_count"] == 1
    # 走定额路径时不再用硬编码产能表兜底
    assert "普工_per_day" not in task


def test_norm_labor_path_prefers_productivity_value():
    """有 productivity_value 时优先用它：P=0.5 → ⌈10/0.5⌉ = 20 人/天。"""
    wbs = make_wbs({
        "id": "N2", "name": "基础钢筋", "duration_days": 10,
        "quantity": 100, "unit": "t", "work_type": "钢筋工程",
        "norm_binding": {"task_id": "N2", "mode": "labor", "norm_value": 4.0,
                         "productivity_value": 0.5, "source_code": "LD_T72_7_2008",
                         "match_type": "exact", "labor_types": ["钢筋工"]},
    })
    flat = resource.compute_flat(wbs, None, None)
    assert flat["resource_demand"]["tasks"][0]["钢筋工_per_day"] == 20


def test_unmarked_crew_roles_classified_safely():
    """没打 crew_kind 标记时的兜底分类：

    - 工种名（钢筋工，产能表里的工人）→ 人工，不能覆盖按定额算出的人工数；
    - 司机 / 泵工 这类 → 机械配员，照常计入 resources。
    """
    wbs = make_wbs({
        "id": "N4", "name": "基础钢筋", "duration_days": 10,
        "quantity": 100, "unit": "t", "work_type": "钢筋工程",
        # 注意：没有 crew_kind，crew 里是人工工种（modify 流程可能这么写）
        "norm_binding": {"task_id": "N4", "mode": "labor", "norm_value": 4.0,
                         "source_code": "LD_T72_7_2008", "match_type": "exact",
                         "crew": {"钢筋工": 12}},
    })
    task = resource.compute_flat(wbs, None, None)["resource_demand"]["tasks"][0]
    assert task["钢筋工_per_day"] == 40        # 按定额算，不能被 crew=12 覆盖

    wbs2 = make_wbs({
        "id": "N5", "name": "机械挖土方", "duration_days": 20,
        "quantity": 1000, "unit": "m3", "work_type": "土方工程",
        "norm_binding": {"task_id": "N5", "mode": "machine", "norm_value": 0.012,
                         "source_code": "GD_2018_A1_1", "match_type": "exact",
                         "machine_name": "履带式单斗液压挖掘机",
                         "crew": {"司机": 2}},          # 无标记 → 按机械配员
    })
    task2 = resource.compute_flat(wbs2, None, None)["resource_demand"]["tasks"][0]
    assert task2["履带式单斗液压挖掘机_per_day"] == 1
    assert task2["司机_per_day"] == 2                  # 1 台 × 司机2名


# 迁移测试说明（第 6 批）：原 `test_crew_bind_survives_kb_exception` 打桩
# `crew_bind.kb.workface_capacity` 抛异常，断"记警告 + 继续处理后面的叶子 +
# workface_known==0"。域 1.6 已删 `Workface_Capacity_Rule` 与
# `kb.workface_capacity()`，crew_bind 不再回查容量、也不再产出 workface 警告
# → 打桩目标与断言语义**都不存在**，**整条用例删除**。
# （配员侧的异常韧性仍由第 2 节用例覆盖。）


def test_broken_binding_falls_back_to_legacy():
    """norm_binding 数值畸形（推不出定额）→ 视为不可用，走遗留路径。"""
    wbs = make_wbs({
        "id": "B1", "name": "底板混凝土", "duration_days": 15,
        "quantity": 15600, "unit": "m³", "work_type": "混凝土工程",
        "norm_binding": {"task_id": "B1", "mode": "labor", "norm_value": "abc",
                         "crew": "司机1名", "crew_kind": 7},      # 类型全错
    })
    flat = resource.compute_flat(wbs, None, None)
    task = flat["resource_demand"]["tasks"][0]
    assert task["混凝土工_per_day"] == 52          # 遗留产能表口径
    assert task["泵车_per_day"] == 13
    assert "_norm_applied" not in task
    assert "resource_provenance" not in flat
    assert not resource.norm_binding_usable({"mode": "labor", "norm_value": "abc"})


def test_norm_path_exception_falls_back_to_legacy(monkeypatch):
    """定额路径内部抛异常 → 退回遗留路径，绝不中断。"""
    def boom(task, binding, quantity, planned_days):
        raise RuntimeError("定额路径炸了")

    monkeypatch.setattr(resource, "compute_norm_resources", boom)
    wbs = make_wbs({
        "id": "B2", "name": "底板混凝土", "duration_days": 15,
        "quantity": 15600, "unit": "m³", "work_type": "混凝土工程",
        "norm_binding": {"task_id": "B2", "mode": "labor", "norm_value": 4.0,
                         "source_code": "LD_T72_7_2008", "match_type": "exact"},
    })
    task = resource.compute_flat(wbs, None, None)["resource_demand"]["tasks"][0]
    assert task["混凝土工_per_day"] == 52
    assert task["泵车_per_day"] == 13


def test_wrong_typed_crew_does_not_crash():
    """crew 写成了字符串 → 拆不出机械配员，但整条计算不崩。"""
    wbs = make_wbs({
        "id": "B3", "name": "基础钢筋", "duration_days": 10,
        "quantity": 100, "unit": "t", "work_type": "钢筋工程",
        "norm_binding": {"task_id": "B3", "mode": "labor", "norm_value": 4.0,
                         "source_code": "LD_T72_7_2008", "match_type": "exact",
                         "crew": "司机1名"},
    })
    task = resource.compute_flat(wbs, None, None)["resource_demand"]["tasks"][0]
    assert task["普工_per_day"] == 40             # 无工种信息 → 兜底普工，人数仍按定额
    assert "司机_per_day" not in task


def test_norm_machine_path_counts_crew_as_resources():
    """machine 定额：台数 + 机械配员都进 resources，来源逐项标注。"""
    wbs = make_wbs({
        "id": "N3", "name": "机械挖土方", "kb_activity_id": ACT_WITH_CREW,
        "duration_days": 20, "quantity": 1000, "unit": "m3", "work_type": "土方工程",
        "crew_source": {"origin": "kb", "ref": "LD_T72", "confidence": "HIGH",
                        "note": "司机1名"},
        "norm_binding": {"task_id": "N3", "mode": "machine", "norm_value": 0.012,
                         "quantity_basis": 1.0, "source_code": "GD_2018_A1_1",
                         "match_type": "exact", "machine_name": "履带式单斗液压挖掘机",
                         "crew": {"司机": 1, "普工": 1},
                         "crew_kind": {"司机": "machine", "普工": "labor"}},
    })
    flat = resource.compute_flat(wbs, None, None)
    task = flat["resource_demand"]["tasks"][0]

    # 总台班 = 1000/1 × 0.012 = 12；⌈12/20⌉ = 1 台/天
    assert task["履带式单斗液压挖掘机_per_day"] == 1
    assert task["履带式单斗液压挖掘机_total_days"] == 12.0
    # 配员随台数走：1 台 × 司机1名
    assert task["司机_per_day"] == 1
    assert task["_resource_source"]["司机"] == {"origin": "kb", "ref": "LD_T72"}
    assert task["_resource_source"]["履带式单斗液压挖掘机"]["origin"] == "kb"
    # 人工工种没被当成机械配员重复计入
    assert "普工_per_day" not in task
    # 台数没超工作面（该活动 max_machine=2）→ 不该有封顶记录
    assert "_workface_capped" not in task


def test_resource_node_writes_provenance_to_ctx():
    """ResourceNode 必须把 resource_provenance 合并回 ctx；遗留场景不带该键。"""
    node = resource.ResourceNode()
    lab = make_wbs({
        "id": "RN", "name": "基础钢筋", "duration_days": 10,
        "quantity": 100, "unit": "t", "work_type": "钢筋工程",
        "norm_binding": {"task_id": "RN", "mode": "labor", "norm_value": 4.0,
                         "source_code": "LD_T72_7_2008", "match_type": "exact",
                         "labor_types": ["钢筋工"]},
    })
    out = node.run({"wbs": lab})
    assert out["resource_provenance"]["RN"]["钢筋工"]["origin"] == "kb"
    assert out["resource_demand"]["tasks"][0]["resources"]["钢筋工"]["per_day"] == 40

    out2 = node.run({"wbs": LEGACY_WBS})
    assert "resource_provenance" not in out2


# ==================== 7) 工作面容量封顶 ====================
def test_workface_capacity_caps_labor():
    """叶子自带 `max_labor=14`：40 人/天必须被压到 14，且写明原因。

    域 1.6（第 6 批）已删 `Workface_Capacity_Rule` + `kb.workface_capacity()`：
    封顶上限只来自**叶子自带**的 `workface_capacity`，不再按 KB 标定公式重算
    （旧口径此处按公式得 15 人，本用例已随该口径改写）。
    """
    wbs = make_wbs({
        "id": "C1", "name": "基础钢筋", "kb_activity_id": ACT_LABOR,
        "duration_days": 10, "quantity": 100, "unit": "t", "work_type": "钢筋工程",
        "workface_capacity": {"max_labor": 14, "max_machine": None,
                              "unit_basis": "每施工段", "origin": "kb",
                              "confidence": "LOW", "note": "数据为 AI 估算，置信度低"},
        "norm_binding": {"task_id": "C1", "mode": "labor", "norm_value": 4.0,
                         "source_code": "LD_T72_7_2008", "match_type": "exact",
                         "labor_types": ["钢筋工"]},
    })
    flat = resource.compute_flat(wbs, None, None)
    task = flat["resource_demand"]["tasks"][0]

    assert task["钢筋工_per_day"] == 14                     # 40 → 14（叶子自带上限）
    capped = task.get("_workface_capped")
    assert capped, "封顶必须留痕（_workface_capped），不能静默"
    assert capped[0]["original_per_day"] == 40
    assert capped[0]["capped_per_day"] == 14
    assert capped[0]["kind"] == "labor"
    assert "14" in capped[0]["reason"]
    assert task["钢筋工_total_days"] == 400                 # 总工日不变，缺的是工期


def test_workface_capacity_caps_machine():
    """max_machine 封顶机械台数。"""
    wbs = make_wbs({
        "id": "C2", "name": "机械挖土方", "duration_days": 25,
        "quantity": 96000, "unit": "m3", "work_type": "土方工程",
        "workface_capacity": {"max_labor": 6, "max_machine": 2,
                              "unit_basis": "每施工段", "origin": "kb",
                              "confidence": "LOW", "note": ""},
        "crew_source": {"origin": "kb", "ref": "LD_T72", "confidence": "HIGH", "note": ""},
        "norm_binding": {"task_id": "C2", "mode": "machine", "norm_value": 0.012,
                         "source_code": "GD_2018_A1_1", "match_type": "exact",
                         "machine_name": "履带式单斗液压挖掘机",
                         "crew": {"司机": 1}, "crew_kind": {"司机": "machine"}},
    })
    flat = resource.compute_flat(wbs, None, None)
    task = flat["resource_demand"]["tasks"][0]

    assert task["履带式单斗液压挖掘机_per_day"] == 2        # 47 台 → 2 台
    assert task["司机_per_day"] == 2                       # 配员随封顶后的台数走
    assert task["_workface_capped"][0]["kind"] == "machine"
    assert task["_workface_capped"][0]["original_per_day"] == 47


# 迁移测试说明（第 6 批）：原 `test_workface_capacity_from_kb_when_leaf_lacks_field`
# 断"叶子没带 `workface_capacity` 时 resource 回查 KB 容量并照常封顶（40 → 15）"。
# 域 1.6 删表后 `resource._resolve_workface()` **只吃叶子自带键**，没有键就是
# 不封顶（40 人/天原样保留）——该用例测的"KB 回查 + 公式上限"能力经用户裁定退役，
# 无法改写成对新行为的对照（新行为由 `test_workface_capacity_caps_labor` 覆盖），
# **整条用例删除**。


def test_有据可查的工作面容量仍然封顶():
    """对照：容量标注为有据可查（非 AI）时，封顶必须照常生效。

    没有这条，上面那条测试就可能掩盖"把封顶功能整个改没了"的回归。
    """
    wbs = make_wbs({
        "id": "C4", "name": "基础钢筋", "kb_activity_id": ACT_LABOR,
        "duration_days": 10, "quantity": 100, "unit": "t", "work_type": "钢筋工程",
        "workface_capacity": {"max_labor": 14, "unit_basis": "每施工段",
                              "source_type": "labor_standard", "confidence": "HIGH"},
        "norm_binding": {"task_id": "C4", "mode": "labor", "norm_value": 4.0,
                         "quantity_basis": 1.0, "source_code": "LD_T72_7_2008",
                         "match_type": "exact", "labor_types": ["钢筋工"]},
    })
    cap = 14                        # 域 1.6：上限只来自叶子自带的 max_labor
    flat = resource.compute_flat(wbs, None, None)
    task = flat["resource_demand"]["tasks"][0]
    assert task["钢筋工_per_day"] == cap, task["钢筋工_per_day"]
    assert task["_workface_capped"][0]["original_per_day"] == 40
    assert task["_workface_capped"][0]["unit_basis"] == "每施工段"
    # 文案不得再声称"工期已延长"（本节点只调投入量，不改计划工期）
    reason = task["_workface_capped"][0]["reason"]
    assert "不改计划工期" in reason, reason


# ==================== 端到端：crew_bind → resource ====================
def test_crew_bind_to_resource_end_to_end():
    """crew_bind 跑完后，resource 定额路径能直接吃下叶子上的数据。"""
    wbs = make_wbs({
        "id": "E1", "name": "基础钢筋", "kb_activity_id": ACT_LABOR,
        "duration_days": 10, "quantity": 100, "unit": "t", "work_type": "钢筋工程",
        "norm_binding": {"task_id": "E1", "mode": "labor", "norm_value": 4.0,
                         "quantity_basis": 1.0, "source_code": "LD_T72_7_2008",
                         "match_type": "exact"},
    })
    node = CrewBindNode()
    ctx = {"wbs": wbs}
    ctx.update(node.run(ctx))
    out = ctx
    assert out["crew_stats"]["machines_total"] == 0
    assert out["crew_stats"]["machines_with_crew"] == 0

    flat = resource.compute_flat(ctx["wbs"], None, None)
    task = flat["resource_demand"]["tasks"][0]

    # 域 1.6（第 6 批）：crew_bind 已不再从 KB 补工作面容量 → 叶子没有该键，
    # resource 也就无上限可封，定额照实算到 40 人/天（100 t ÷ 0.25 t/工日 ÷ 10 天）。
    assert task["钢筋工_per_day"] == 40
    assert "_workface_capped" not in task
    assert "_resource_source" in task


# ==================== 政策变更（2026-09-20）：AI 估算定额的消费层 ====================
def test_ai_estimate_norm_now_computes_resources_with_source():
    """**政策变更（用户 2026-09-20 亲自决定）**：AI 经验估算定额现在照算班组 + 逐条标注。

    旧口径（第 37~39 轮）：`resource._norm_evidence_reason()` 命中
    `origin=ai` / `match_type=ai` / `source_code=AI_*` 就返回「AI 估算定额（只作参考，
    不用来算班组）」，调用处 `continue` —— **整条任务不给班组**（`_norm_flagged`）。
    新口径：AI 来源不再拦（只剩"单位不可换算"与"定额口径不符"两条），但资源行必须
    留下可追溯的来源：`_resource_source[工种]` 的 `origin="ai_estimate"` + `ref` 为
    来源代号，`_norm_applied` 保留 `source_code` 并给出 `ai_estimate` / `source_label`
    （交付物层的依据列据此标注，**不需要新增键**）。
    """
    wbs = make_wbs({
        "id": "A1", "name": "楼地面找平", "kb_activity_id": "",
        "duration_days": 10, "quantity": 100, "unit": "m²", "work_type": "装饰工程",
        "norm_binding": {"task_id": "A1", "mode": "labor", "productivity_value": 12.5,
                         "quantity_basis": 1.0, "source_code": "AI_ESTIMATE_V1",
                         "match_type": "exact", "unit": "工日/m²",
                         "labor_types": ["抹灰工"]},
    })
    flat = resource.compute_flat(wbs, None, None)
    task = flat["resource_demand"]["tasks"][0]
    assert "_norm_flagged" not in task and "_warning" not in task, \
        "政策变更 2026-09-20：AI 来源不再被拦下，必须真的算出班组"
    assert task["抹灰工_per_day"] >= 1, task
    src = task["_resource_source"]["抹灰工"]
    assert src["origin"] == "ai_estimate", src      # AI 来源单列（旧口径写 "kb"）
    assert src["ref"] == "AI_ESTIMATE_V1", src
    applied = task["_norm_applied"]
    assert applied["source_code"] == "AI_ESTIMATE_V1", applied
    assert applied["ai_estimate"] is True, applied
    assert applied["source_label"] == "AI 经验估算定额（无规范依据，待审）", applied


def test_ai_estimate_norm_with_unusable_unit_is_still_blocked():
    """对照：放开 AI 来源**不等于**放开单位判据 —— 单位不可换算仍然不给班组。

    没有这条，上面那条测试就可能掩盖"把证据门整个拆掉"的回归。
    """
    wbs = make_wbs({
        "id": "A2", "name": "楼地面找平", "kb_activity_id": "",
        "duration_days": 10, "quantity": 100, "unit": "m²", "work_type": "装饰工程",
        "norm_binding": {"task_id": "A2", "mode": "labor", "productivity_value": 12.5,
                         "source_code": "AI_ESTIMATE_V1", "match_type": "exact",
                         "unit": "工日/m³", "labor_types": ["抹灰工"]},
    })
    flat = resource.compute_flat(wbs, None, None)
    task = flat["resource_demand"]["tasks"][0]
    assert task.get("_norm_flagged"), task
    assert "不可换算" in task["_norm_flagged"], task["_norm_flagged"]
    assert not task.get("resources"), "被拦下的任务不许给出班组（这条判据没松）"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        try:
            fn()
        except TypeError:                     # 参数化用例：手工展开
            for text, expected in [("司机1名", {"司机": 1}), ("泵工1人+辅助1人", {"泵工": 1, "辅助": 1}),
                                   ("", {}), (None, {})]:
                fn(text, expected)
        print("  PASS  %s" % fn.__name__)
    print("\n全部用例通过 ✔")
