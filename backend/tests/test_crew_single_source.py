# -*- coding: utf-8 -*-
"""D1 回归：计划里的班组只能有**一个**真源。

背景（真实缺陷，已修）：
  `crew_bind` 只填**机械配员**，人工班组是**故意留空**的（见 `test_norm_bind.py` 里
  `b["crew"] == {}` 的断言，注释写着「配员由别的节点填，这里必须留空」），等实际资源
  算出来后再回填。但初次排程这条路漏了回填，于是计划里长期并存两个互相矛盾的班组：

    · `norm_binding.crew`   —— `/sources` 读它
    · `assigned_resources`  —— 交付物（Word / 看板）读它

  潭村 12 栋实测：415 条叶子里有 **377 条（90.8%）** 对不上，
  例如 `4.1.1.1` 绑定写「钢筋工 ×1」而实际是「钢筋工 ×14」。

修法：
  1. `resource.py::compute_norm_resources` 把已算好的人工班组以 `_crew` 透出
     —— **不含机械本身**（机械不是人，不能写进班组字段）；
  2. `plan_assembler.build_parts` 把它回写进 `leaf.norm_binding["crew"]`。

运行：python -m pytest backend/tests/test_crew_single_source.py -q
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from pipeline.nodes.plan_assembler import build_meta, build_parts  # noqa: E402
from pipeline.nodes.resource import compute_norm_resources  # noqa: E402


# ── 第一层：resource 节点透出的人工班组 ──────────────────────
LABOR_BINDING = {
    "mode": "labor", "norm_value": 0.1, "productivity_value": 10.0,
    "quantity_basis": 1.0, "source_code": "TEST_001", "match_type": "default",
    "labor_types": ["钢筋工"], "crew": {},
}

MACHINE_BINDING = {
    "mode": "machine", "norm_value": 1.26,
    "productivity_value": 0.7936507936507936,
    "quantity_basis": 10.0, "source_code": "TEST_M_001", "match_type": "exact",
    "machine_name": "混凝土输送泵车",
    "crew": {"泵工": 1}, "crew_kind": {"泵工": "machine"},
}


class TestResourceEmitsLaborCrew:
    """本函数返回的是**扁平键**（`钢筋工_per_day`），嵌套在 to_nested_resources。"""

    def test_人工任务透出纯人工班组(self):
        d = compute_norm_resources({"id": "T1", "name": "钢筋绑扎"},
                                   dict(LABOR_BINDING), 100.0, 2)
        assert d is not None
        # 工日需求 = (100 t ÷ 2 天) ÷ 10 t/工日 = 5 人/天
        assert d["钢筋工_per_day"] == 5
        assert d["钢筋工_total_days"] == 10.0
        assert d["_crew"] == {"钢筋工": 5}, d["_crew"]

    def test_机械任务透出的人工班组不含机械(self):
        d = compute_norm_resources({"id": "T2", "name": "混凝土浇筑"},
                                   dict(MACHINE_BINDING), 427.0, 2)
        assert d is not None
        # 总台班 = 427 ÷ 10 × 1.26 = 53.8；2 天干完 → 27 台
        assert d["混凝土输送泵车_per_day"] == 27
        assert "混凝土输送泵车" not in d["_crew"], \
            "机械**不能**出现在人工班组字段里"
        # 配员随台数走：27 台 × 1 名泵工
        assert d["_crew"] == {"泵工": 27}, d["_crew"]
        assert d["泵工_per_day"] == 27

    def test_没有可用定额时不产出班组(self):
        assert compute_norm_resources({"id": "T3", "name": "未知"}, {}, 10.0, 2) is None
        assert compute_norm_resources({"id": "T4", "name": "未知"},
                                      dict(LABOR_BINDING), 0.0, 2) is None


# ── 第二层：plan_assembler 把班组回写进叶子 ──────────────────
def _leaf(tid, name, dur=2, crew=None):
    leaf = {"id": tid, "name": name, "quantity": 100.0, "unit": "t",
            "duration_days": dur}
    if crew is not None:
        leaf["norm_binding"] = {"crew": crew}
    return leaf


def _ctx(leaves, rd_tasks):
    return {
        "wbs": {"phases": [{"phase": "主体", "work_packages": [
            {"id": "1.1", "name": "主体", "sub_packages": leaves}]}]},
        "cpm_result": {
            "total_duration_days": 10,
            "critical_path": [leaves[0]["id"]],
            "schedule": [{"task_id": lf["id"], "es": 0, "ef": lf["duration_days"]}
                         for lf in leaves],
        },
        "resource_demand": {"tasks": rd_tasks},
        "extracted_params": {"planned_start_date": "2026-03-01"},
        "boundary_conditions": {},
    }


def _row(tid, name, resources, crew=None, mode="labor"):
    row = {"task_id": tid, "task_name": name, "resources": resources,
           "_norm_applied": {"mode": mode}}
    if crew is not None:
        row["_crew"] = crew
    return row


class TestAssemblerWritesCrewBack:
    def test_人工班组回写进norm_binding(self):
        leaf = _leaf("1.1.1", "钢筋绑扎", crew={"钢筋工": 1})   # 旧的占位值
        row = _row("1.1.1", "钢筋绑扎",
                   {"钢筋工": {"per_day": 14, "total_days": 120.0}},
                   crew={"钢筋工": 14})
        parts = build_parts(_ctx([leaf], [row]))
        assert leaf["norm_binding"]["crew"] == {"钢筋工": 14}, \
            "必须用实际班组覆盖占位值"
        assert leaf["norm_binding"].get("crew_source"), "必须留下回写来源"
        # 与交付物读的 assigned_resources 一致 —— 这就是"单一真源"
        task = parts["all_tasks_schedule"][0]
        assert task["assigned_resources"] == {"钢筋工": 14}

    def test_机械不写进人工班组(self):
        leaf = _leaf("1.1.2", "混凝土浇筑", crew={"泵工": 1})
        row = _row("1.1.2", "混凝土浇筑",
                   {"混凝土输送泵车": {"per_day": 1, "total_days": 53.8},
                    "泵工": {"per_day": 1, "total_days": 53.8}},
                   crew={"泵工": 1}, mode="machine")
        build_parts(_ctx([leaf], [row]))
        assert "混凝土输送泵车" not in leaf["norm_binding"]["crew"]
        assert leaf["norm_binding"]["crew"] == {"泵工": 1}

    def test_缺少_crew_时不覆盖已有值(self):
        """resource_demand 没透出 _crew（遗留路径）时，不能把已有班组清掉。"""
        leaf = _leaf("1.1.3", "临时设施", crew={"普工": 3})
        row = _row("1.1.3", "临时设施",
                   {"普工": {"per_day": 1, "total_days": 4.0}})
        build_parts(_ctx([leaf], [row]))
        assert leaf["norm_binding"]["crew"] == {"普工": 3}, \
            "没有新数据时不许动已有班组"

    def test_回写不依赖是否有排程行(self):
        """班组来自 resource_demand，与本条任务排没排上无关，不能因此漏写。"""
        leaf = _leaf("9.9.9", "未排上的任务")
        row = _row("9.9.9", "未排上的任务",
                   {"瓦工": {"per_day": 7, "total_days": 14.0}},
                   crew={"瓦工": 7})
        ctx = _ctx([leaf], [row])
        ctx["cpm_result"]["schedule"] = []       # 故意不给排程行
        build_parts(ctx)
        assert leaf["norm_binding"]["crew"] == {"瓦工": 7}

    def test_非正数与非整数班组的处理(self):
        leaf = _leaf("1.1.4", "混合班组")
        row = _row("1.1.4", "混合班组",
                   {"钢筋工": {"per_day": 6, "total_days": 12.0}},
                   crew={"钢筋工": 6, "瓦工": 0, "普工": -1, "抹灰工": 2.5})
        build_parts(_ctx([leaf], [row]))
        assert leaf["norm_binding"]["crew"] == {"钢筋工": 6, "抹灰工": 2.5}, \
            "非正数必须丢弃，非整数保留原值"

    def test_班组是_assigned_resources_的子集(self):
        """单一真源的核心不变量：班组 ⊆ 交付物读到的资源。"""
        leaves = [_leaf("1.1.1", "钢筋绑扎", crew={"钢筋工": 1}),
                  _leaf("1.1.2", "混凝土浇筑", crew={"泵工": 1})]
        rows = [_row("1.1.1", "钢筋绑扎",
                     {"钢筋工": {"per_day": 14, "total_days": 120.0}},
                     crew={"钢筋工": 14}),
                _row("1.1.2", "混凝土浇筑",
                     {"混凝土输送泵车": {"per_day": 1, "total_days": 53.8},
                      "泵工": {"per_day": 1, "total_days": 53.8}},
                     crew={"泵工": 1}, mode="machine")]
        parts = build_parts(_ctx(leaves, rows))
        by_id = {t["task_id"]: t for t in parts["all_tasks_schedule"]}
        for lf in leaves:
            crew = set(lf["norm_binding"]["crew"])
            res = set(by_id[lf["id"]]["assigned_resources"])
            assert crew <= res, "%s：班组 %s 不是资源 %s 的子集" % (lf["id"], crew, res)


# ── 第三层：resource_plan 的口径（机械配员 vs 设备；人工需求并入）──────
class TestResourcePlanCaliber:
    """机械配员是**人**不是设备；已算好的人工需求必须进 `resource_plan`。

    背景（用户实测反馈，真实计划 plan_run_1789818211）：
      · `equipment_peak` 里混进了 `泵工17 / 辅助17 / 操作工15 / 司机1` —— 看板
        「设备资源荷载」显示的是人；
      · 而 `meta.machine_labor_demand` 里的「混凝土工 2633.6 工日 / 40 个任务」
        没有进 `resource_plan` → 交付物只拿得到 plan，看板上「混凝土工」彻底消失。
    口径（硬约束）：`total_manpower_days` = **工种人工**工日账本，保持原样；
    机械配员只进人数峰值 + 单列 `machine_crew_peak`，不进 `equipment_peak`。
    """

    MACHINE_RES = {"混凝土输送泵车": {"per_day": 17, "total_days": 180.9},
                   "泵工": {"per_day": 17, "total_days": 180.9},
                   "辅助": {"per_day": 17, "total_days": 180.9}}

    def _machine_ctx(self):
        return _ctx([_leaf("1.1.2", "混凝土浇筑")],
                    [_row("1.1.2", "混凝土浇筑", self.MACHINE_RES,
                          crew={"泵工": 17}, mode="machine")])

    def test_机械配员不进设备峰值(self):
        rp = build_parts(self._machine_ctx())["resource_plan"]
        assert rp["equipment_peak"] == {"混凝土输送泵车": 17}, \
            "设备表里只能有真设备"
        for name in ("泵工", "辅助", "操作工", "司机"):
            assert name not in rp["equipment_peak"], "%s 是人，不是设备" % name

    def test_机械配员单列人数峰值_且不计入工种人工工日(self):
        rp = build_parts(self._machine_ctx())["resource_plan"]
        assert rp["machine_crew_peak"] == {"泵工": 17, "辅助": 17}
        # 配员是"每日在场的人" → 计入人数峰值（17 + 17）
        assert rp["peak_manpower"] == 34
        # 但 **不是** 工种人工：total_manpower_days 的口径与数值不变
        assert rp["total_manpower_days"] == 0.0

    def test_已算好的人工需求并进resource_plan(self):
        ctx = self._machine_ctx()
        ctx["machine_labor_demand"] = {
            "demand": {"混凝土工": 2633.601925925928},
            "detail": {"混凝土工": {"days": 2633.601925925928, "tasks": 40,
                                    "activity": "CONC_NEW_FOUND"}},
        }
        rp = build_parts(ctx)["resource_plan"]
        assert rp["labor_demand"] == {"混凝土工": 2633.601925925928}
        assert rp["labor_demand_detail"]["混凝土工"]["tasks"] == 40
        # resource_plan 与 meta 读的是**同一个 ctx**，不许两处分叉
        assert build_meta(ctx)["machine_labor_demand"]["demand"] == rp["labor_demand"]

    def test_没有人工需求时字段存在且为空(self):
        ctx = _ctx([_leaf("1.1.3", "临时设施")],
                   [_row("1.1.3", "临时设施",
                         {"普工": {"per_day": 3, "total_days": 6.0}},
                         crew={"普工": 3})])
        rp = build_parts(ctx)["resource_plan"]
        assert rp["labor_demand"] == {}
        assert rp["labor_demand_detail"] == {}
        assert rp["machine_crew_peak"] == {}
        assert rp["equipment_peak"] == {}


REAL_PLAN = BACKEND / "plans" / "plan_run_1789818211.json"


@pytest.mark.skipif(not REAL_PLAN.exists(),
                    reason="plans/ 是运行产物，真实计划不在仓库里")
def test_真实计划_设备表没有人且人工需求已并入():
    """端到端复核（真实 plan）：设备表剔除 4 类配员，混凝土工需求进 resource_plan。

    这里不写死 2633.6 这个数：计划被重跑后数值会变，判据是**与 meta 同源**。
    """
    plan = json.loads(REAL_PLAN.read_text(encoding="utf-8"))
    meta = plan.get("meta") or {}
    recorded = plan.get("resource_plan") or {}
    ctx = {
        "wbs": plan.get("wbs") or {},
        "cpm_result": dict(plan.get("cpm_result") or {}),
        "resource_demand": plan.get("resource_demand") or {},
        "extracted_params": meta.get("extracted_params") or {},
        "boundary_conditions": meta.get("boundary_conditions") or {},
        "machine_labor_demand": meta.get("machine_labor_demand") or {},
    }
    rp = build_parts(ctx)["resource_plan"]
    for name in ("泵工", "辅助", "操作工", "司机"):
        assert name not in rp["equipment_peak"], "%s 是人，不是设备" % name
    assert set(rp["machine_crew_peak"]) == {"泵工", "辅助", "操作工", "司机"}
    assert all(v > 0 for v in rp["machine_crew_peak"].values())
    demand = rp["labor_demand"]
    assert demand, "已算好的人工需求不能丢"
    assert demand["混凝土工"] == pytest.approx(
        meta["machine_labor_demand"]["demand"]["混凝土工"])
    # 既有口径没被这次修正改变（工日账本）
    assert rp["total_manpower_days"] == recorded["total_manpower_days"]
    # E1（用户 2026-09-21 裁定）：这份旧计划的 `boundary_conditions.labor.peak_total=120`
    # 没有 `_source`（= 当年由 `boundary` 节点按"18 层住宅常见做法"补的，病根 3），
    # **不再被采纳** —— 峰值人数改为曲线/逐任务实算，两个申报键也整体消失。
    assert "declared_peak_manpower" not in rp, rp
    assert "declared_peak_manpower_source" not in rp, rp
    assert rp["peak_manpower_source"] == "resource_curve", (
        "旧计划没标来源 → 不许默认当用户给的，回落资源曲线口径")
    assert rp["peak_manpower"] == rp["curve_peak_manpower"]
    assert rp["peak_manpower"] != 120, "模型补的 120 不许再顶掉实算峰值"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_")]
    for cls in (TestResourceEmitsLaborCrew, TestAssemblerWritesCrewBack,
                TestResourcePlanCaliber):
        for name in sorted(dir(cls)):
            if name.startswith("test_"):
                getattr(cls(), name)()
                print("ok  %s.%s" % (cls.__name__, name))
    print("全部通过")
