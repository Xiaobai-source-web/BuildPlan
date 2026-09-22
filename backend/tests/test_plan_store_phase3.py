# -*- coding: utf-8 -*-
"""plan_store 第 3 阶段：新增可改字段（name / start_date / target_duration /
add_task / remove_task）的专项单测。

运行（沙箱下必须带 tmpfix，否则 pytest 的临时目录会被拒）：
  python -m pytest backend/tests/test_plan_store_phase3.py -q -p no:cacheprovider -p _probe_tmp.tmpfix

为什么要这些用例（每条都对应一个"用户会当场发现"的故障）：
  - 改名只改树、不改 all_tasks_schedule / critical_path_tasks → 看板与 Word 里还是旧名；
  - 开工日期只写 overview、不平移逐条任务日期 → 总览说 3 月开工、甘特画在 1 月；
  - 目标总工期只记一句话、不落 boundary_conditions → 排程器读不到，用户白说；
  - 新增任务是"孤儿"（没有前后置）→ 排程把它排到一边，总工期不含它；
  - 删除任务直接摘掉前后两条边 → "A→tid→B" 断成互不相干，后续任务被重排到最前；
  - 以上任何一条若顺手改了入参 plan，修订链重放就会自我叠加，越改越乱。
"""

import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline.plan_store import (
    LEAF_FIELDS, META_FIELDS, PLAN_TARGET, STRUCT_FIELDS, SUPPORTED_FIELDS,
    apply_patch, find_leaf, iter_leaves,
)

START = "2026-01-01"


# ==================== 夹具 ====================
def _leaf(tid, name, **kw):
    leaf = {
        "id": tid,
        "name": name,
        "duration_days": 10,
        "quantity": 120.0,
        "unit": "吨",
        "work_type": "钢筋工程",
        "norm_binding": {"task_id": tid, "mode": "labor", "norm_value": 1.5,
                         "crew": {"钢筋工": 10}},
    }
    leaf.update(kw)
    return leaf


def _plan(head="4.1.1", start=START, end="2026-10-30", total=303):
    """一个工作包 4 个叶子（<head>.1→<head>.2→<head>.4，<head>.3 独立）。

    head 是叶子编号的前缀，同时也是工作包的 id：
      head="4.1.1"（默认）→ 工作包 4.1.1，叶子 4.1.1.1…4.1.1.4，自动编号 4.1.1.5
      head="4.1"          → 工作包少一级，叶子 4.1.1…4.1.4，自动编号 4.1.5
      两个层级都覆盖到：自动编号必须取**宿主工作包**的编号，而不是目标叶子的编号。

    带 all_tasks_schedule / critical_path_tasks 两张冗余任务表 —— 改名与删任务
    必须同步维护它们，缺了这两张表就测不出"改名在甘特上没生效"这类问题。
    """
    base = head + "."
    rows = [
        {"task_id": base + "1", "task_name": "Ⅰ区钢筋绑扎", "start_date": "2026-01-01",
         "finish_date": "2026-01-10"},
        {"task_id": base + "2", "task_name": "Ⅰ区混凝土浇筑", "start_date": "2026-01-11",
         "finish_date": "2026-01-18"},
        {"task_id": base + "3", "task_name": "Ⅰ区养护", "start_date": "2026-01-01",
         "finish_date": "2026-01-07"},
        {"task_id": base + "4", "task_name": "Ⅰ区验收", "start_date": "2026-01-19",
         "finish_date": "2026-01-21"},
    ]
    plan = {
        "plan_id": "phase3",
        "overview": {"project_name": "阶段三测试项目", "total_duration_days": total,
                     "planned_start_date": start, "planned_end_date": end,
                     "critical_path_length": 3},
        "wbs": {"phases": [{"phase": "主体结构", "work_packages": [
            {"id": head, "name": "Ⅰ区主体", "sub_packages": [
                _leaf(base + "1", "Ⅰ区钢筋绑扎"),
                _leaf(base + "2", "Ⅰ区混凝土浇筑", duration_days=8, quantity=200.0),
                _leaf(base + "3", "Ⅰ区养护", duration_days=7, quantity=1.0, unit="项"),
                _leaf(base + "4", "Ⅰ区验收", duration_days=3),
            ]}]}]},
        "dependencies": [
            {"predecessor": base + "1", "successor": base + "2", "type": "FS", "lag_days": 0},
            {"predecessor": base + "2", "successor": base + "4", "type": "FS", "lag_days": 0},
        ],
        "cpm_result": {"total_duration_days": total, "critical_path": [base + "1"],
                       "schedule": []},
        "all_tasks_schedule": [dict(r) for r in rows],
        "critical_path_tasks": [dict(rows[0]), {"task_id": base + "98",
                                                "task_name": "不在本计划里"}],
        "meta": {"audit_status": "未审计", "plan_level": "L4"},
    }
    return plan


def _patch(target, field, value):
    return {"target": target, "field": field, "value": value, "scope": "auto",
            "reason": "阶段三测试"}


def _snapshot(plan):
    """深拷贝后的 JSON 文本：用来断言纯函数没改入参。"""
    return json.dumps(plan, ensure_ascii=False, sort_keys=True)


def _ids(plan):
    return [x["id"] for x in iter_leaves(plan)]


def _deps(plan):
    return sorted((str(d.get("predecessor")), str(d.get("successor")))
                  for d in plan["dependencies"])


def _row(plan, tid, key="task_name"):
    for r in plan["all_tasks_schedule"]:
        if r.get("task_id") == tid:
            return r.get(key)
    return None


# ==================== 0. 字段集合 ====================
def test_supported_fields_cover_all_phase3_capabilities():
    for f in ("quantity", "duration", "norm", "crew", "name", "level", "cost",
              "segment", "plan_title", "start_date", "target_duration",
              "add_task", "remove_task"):
        assert f in SUPPORTED_FIELDS, f
    assert LEAF_FIELDS == ("quantity", "duration", "norm", "crew", "name")
    assert META_FIELDS == ("level", "cost", "segment", "plan_title",
                           "start_date", "target_duration")
    assert STRUCT_FIELDS == ("add_task", "remove_task")
    assert SUPPORTED_FIELDS == LEAF_FIELDS + META_FIELDS + STRUCT_FIELDS
    assert PLAN_TARGET == "plan"


# ==================== 1. name：改名 ====================
def test_rename_updates_leaf_and_denormalised_tables():
    plan = _plan()
    before = _snapshot(plan)
    new_plan, changed, out = apply_patch(
        plan, _patch("4.1.1.1", "name", " Ⅰ区钢筋绑扎（夜间） "))

    assert out["applied"] is True
    assert "warning" not in out
    assert changed == ["4.1.1.1"]
    assert find_leaf(new_plan, "4.1.1.1")["name"] == "Ⅰ区钢筋绑扎（夜间）"   # 首尾空白去掉
    # 两张冗余任务表同步（改名不重排，所以必须一起改）
    for key in ("all_tasks_schedule", "critical_path_tasks"):
        rows = [r for r in new_plan[key] if r.get("task_id") == "4.1.1.1"]
        assert rows, key
        for r in rows:
            assert r["task_name"] == "Ⅰ区钢筋绑扎（夜间）", key
    # 别的任务不受影响
    assert _row(new_plan, "4.1.1.2") == "Ⅰ区混凝土浇筑"
    assert new_plan["critical_path_tasks"][1]["task_name"] == "不在本计划里"
    # 纯函数：入参一字未改
    assert _snapshot(plan) == before


def test_rename_rejects_empty_and_too_long():
    plan = _plan()
    before = _snapshot(plan)

    for bad in ("", "   ", "\t\n", None):
        _p, changed, out = apply_patch(plan, _patch("4.1.1.1", "name", bad))
        assert out["applied"] is False, bad
        assert changed == []
        assert "不能为空" in out["warning"]

    _p, changed, out = apply_patch(plan, _patch("4.1.1.1", "name", "长" * 41))
    assert out["applied"] is False
    assert "40" in out["warning"] and "过长" in out["warning"]
    assert changed == []

    # 40 字正好可以（边界不能被误伤）
    ok, changed, out = apply_patch(plan, _patch("4.1.1.1", "name", "长" * 40))
    assert out["applied"] is True and changed == ["4.1.1.1"]
    assert find_leaf(ok, "4.1.1.1")["name"] == "长" * 40

    # 打不中的 target 走原有拒绝路径
    _p2, changed2, out2 = apply_patch(plan, _patch("9.9.9.9", "name", "新名字"))
    assert out2["applied"] is False and changed2 == []
    assert "找不到" in out2["warning"]
    assert _snapshot(plan) == before


def test_rename_tolerates_plan_without_schedule_tables():
    """老计划可能压根没有这两张表：不能因为找不到就抛异常。"""
    plan = _plan()
    plan.pop("all_tasks_schedule")
    plan["critical_path_tasks"] = "损坏的值"
    new_plan, changed, out = apply_patch(plan, _patch("4.1.1.3", "name", "养护（改）"))
    assert out["applied"] is True and changed == ["4.1.1.3"]
    assert find_leaf(new_plan, "4.1.1.3")["name"] == "养护（改）"


# ==================== 2. start_date：开工日期 ====================
def test_start_date_shifts_schedule_and_end_date():
    plan = _plan()
    before = _snapshot(plan)
    new_plan, changed, out = apply_patch(plan, _patch("plan", "start_date", "2026-01-15"))

    assert out["applied"] is True
    assert changed == ["plan"]
    assert new_plan["overview"]["planned_start_date"] == "2026-01-15"
    # 竣工日期 = 新开工 + (总工期 - 1) 天 —— **闭区间**口径（首尾两天都算）。
    # 冻结断言原来写 +303（半新半旧：日期按闭区间用、竣工却按 +total 算）。
    # 标本本身就是闭区间：START=2026-01-01、total=303、planned_end_date=2026-10-30
    # = START + 302。改成 +302 后与标本、与逐条 finish_date = 开工+(ef-1) 全部对齐。
    assert new_plan["overview"]["planned_end_date"] == "2026-11-13"
    # meta 镜像一份（参数面板读 meta）
    assert new_plan["meta"]["start_date"] == "2026-01-15"
    # 每一行的 start / finish 都平移 14 天
    assert _row(new_plan, "4.1.1.1", "start_date") == "2026-01-15"
    assert _row(new_plan, "4.1.1.1", "finish_date") == "2026-01-24"
    assert _row(new_plan, "4.1.1.2", "start_date") == "2026-01-25"
    assert _row(new_plan, "4.1.1.4", "finish_date") == "2026-02-04"
    assert len(new_plan["all_tasks_schedule"]) == 4
    assert _snapshot(plan) == before


def test_start_date_backwards_and_empty_target_accepted():
    plan = _plan()
    new_plan, changed, out = apply_patch(plan, _patch("", "start_date", "2025-12-25"))
    assert out["applied"] is True
    assert changed == ["plan"]                       # 空 target 也认，落到 plan
    assert new_plan["overview"]["planned_start_date"] == "2025-12-25"
    assert _row(new_plan, "4.1.1.1", "start_date") == "2025-12-25"      # -7 天
    assert _row(new_plan, "4.1.1.1", "finish_date") == "2026-01-03"
    # 闭区间：竣工日 = 新开工 + (总工期 - 1) = 2025-12-25 + 302（原来写 +303，多一天）
    assert new_plan["overview"]["planned_end_date"] == "2026-10-23"

    # 同一天：不重算竣工日期，但也不该出错
    same, _c, out2 = apply_patch(plan, _patch("plan", "start_date", START))
    assert out2["applied"] is True
    assert same["overview"]["planned_end_date"] == "2026-10-30"


def test_start_date_without_old_start_does_not_shift_and_explains():
    plan = _plan()
    plan["overview"].pop("planned_start_date")
    before = _snapshot(plan)
    new_plan, changed, out = apply_patch(plan, _patch("plan", "start_date", "2026-03-01"))

    assert out["applied"] is True                    # 记下用户的话，不算失败
    assert changed == ["plan"]
    assert new_plan["overview"]["planned_start_date"] == "2026-03-01"
    assert new_plan["meta"]["start_date"] == "2026-03-01"
    assert "note" in out and out["note"]
    assert "warning" not in out                      # 不能把"改了但没法平移"报成失败
    # 算不出差值就不猜：逐条任务日期原样保留
    assert _row(new_plan, "4.1.1.1", "start_date") == "2026-01-01"
    assert _row(new_plan, "4.1.1.1", "finish_date") == "2026-01-10"
    assert new_plan["overview"]["planned_end_date"] == "2026-10-30"
    assert _snapshot(plan) == before

    # 旧日期是垃圾字符串，同样只记不改
    plan2 = _plan()
    plan2["overview"]["planned_start_date"] = "开工日待定"
    new2, _c2, out2 = apply_patch(plan2, _patch("plan", "start_date", "2026-03-01"))
    assert out2["applied"] is True and out2.get("note")
    assert _row(new2, "4.1.1.1", "start_date") == "2026-01-01"


def test_start_date_rejects_malformed_dates():
    plan = _plan()
    before = _snapshot(plan)
    for bad in ("2026/01/15", "2026-13-01", "2026-02-30", "26-01-15", "下周三",
                "2026-01-15T00:00:00", "", None, 20260115):
        _p, changed, out = apply_patch(plan, _patch("plan", "start_date", bad))
        assert out["applied"] is False, bad
        assert changed == []
        assert "YYYY-MM-DD" in out["warning"]
    assert _snapshot(plan) == before                 # 拒绝时也不能改入参


def test_start_date_without_parseable_total_does_not_crash():
    """total_duration_days 缺失/非法时：只平移，不编造竣工日期。"""
    plan = _plan()
    plan["overview"].pop("total_duration_days")
    before = _snapshot(plan)
    new_plan, _c, out = apply_patch(plan, _patch("plan", "start_date", "2026-01-11"))
    assert out["applied"] is True
    assert new_plan["overview"]["planned_start_date"] == "2026-01-11"
    # 没有总工期 → 老竣工日期照着同样的 +10 天平移，不留在原地自相矛盾
    assert new_plan["overview"]["planned_end_date"] == "2026-11-09"
    assert _row(new_plan, "4.1.1.1", "start_date") == "2026-01-11"   # 平移照旧
    assert _snapshot(plan) == before

    # 总工期与老竣工日期都拿不到 → 不凭空编一个竣工日期出来
    plan2 = _plan()
    plan2["overview"].pop("total_duration_days")
    plan2["overview"].pop("planned_end_date")
    new2, _c2, out2 = apply_patch(plan2, _patch("plan", "start_date", "2026-01-11"))
    assert out2["applied"] is True
    assert "planned_end_date" not in new2["overview"]
    assert _row(new2, "4.1.1.1", "start_date") == "2026-01-11"

    # 任务表本身是坏值：不炸，总览照改
    plan3 = _plan()
    plan3["all_tasks_schedule"] = "坏值"
    new3, _c3, out3 = apply_patch(plan3, _patch("plan", "start_date", "2026-01-11"))
    assert out3["applied"] is True
    assert new3["overview"]["planned_start_date"] == "2026-01-11"


# ==================== 3. target_duration：目标总工期 ====================
def test_target_duration_records_meta_and_boundary_conditions():
    plan = _plan()
    before = _snapshot(plan)
    new_plan, changed, out = apply_patch(plan, _patch("plan", "target_duration", 300))

    assert out["applied"] is True
    assert changed == ["plan"]
    assert new_plan["meta"]["target_duration_days"] == 300
    assert new_plan["meta"]["boundary_conditions"]["project_duration_days"] == 300
    # 只记意图：不重排、不动树、不动任务表
    assert new_plan["wbs"] == plan["wbs"]
    assert new_plan["all_tasks_schedule"] == plan["all_tasks_schedule"]
    assert [x["duration_days"] for x in iter_leaves(new_plan)] == [10, 8, 7, 3]
    assert _snapshot(plan) == before


def test_target_duration_creates_and_merges_boundary_conditions():
    plan = _plan()
    new_plan, _c, out = apply_patch(plan, _patch("", "target_duration", 250))
    assert out["applied"] is True
    assert new_plan["meta"]["target_duration_days"] == 250
    assert new_plan["meta"]["boundary_conditions"]["project_duration_days"] == 250

    # 已有 boundary_conditions：合并，不覆盖别的人工/材料边界
    plan2 = _plan()
    plan2["meta"]["boundary_conditions"] = {"labor": {"available": 40}}
    new2, _c2, _o2 = apply_patch(plan2, _patch("plan", "target_duration", 180))
    assert new2["meta"]["boundary_conditions"]["project_duration_days"] == 180
    assert new2["meta"]["boundary_conditions"]["labor"] == {"available": 40}
    assert "planned_end_date" not in new2["meta"]

    # boundary_conditions 是垃圾值 → 换成一个可用字典
    plan3 = _plan()
    plan3["meta"]["boundary_conditions"] = "坏值"
    new3, _c3, out3 = apply_patch(plan3, _patch("plan", "target_duration", 210))
    assert out3["applied"] is True
    assert new3["meta"]["boundary_conditions"] == {"project_duration_days": 210}


def test_target_duration_syncs_source_when_boundary_has_provenance():
    """第 40 轮：总工期补丁是**用户明确改写**，来源必须同步成 user。

    否则按新约定读 `_source` 的下游（`scheduler.parse_boundary_limits` /
    `resource.parse_boundary_conditions`）会把它当"模型按常见做法补的"而**忽略** ——
    用户那句"总工期压到 300 天"就静默失效了（只加标注不同步标注 = 指令被规则吃掉）。
    """
    plan = _plan()
    plan["meta"]["boundary_conditions"] = {
        "project_duration_days": 420,
        "_source": {"project_duration_days": "user", "labor.peak_total": "model"},
    }
    new_plan, _c, out = apply_patch(plan, _patch("plan", "target_duration", 300))

    assert out["applied"] is True
    bc = new_plan["meta"]["boundary_conditions"]
    assert bc["project_duration_days"] == 300
    assert bc["_source"]["project_duration_days"] == "user"
    # 相邻键的来源不许被顺手改写
    assert bc["_source"]["labor.peak_total"] == "model"


def test_target_duration_without_source_does_not_invent_provenance():
    """老计划（boundary 里没有 `_source`）不许凭空造标注。

    无标注时下游按"旧行为"照常采纳这个值，所以功能不受影响；但凭空写一个只有单键的
    `_source` 会让"其余键到底有没有标注"看起来像已知信息，这是编数据。
    """
    plan = _plan()
    plan["meta"]["boundary_conditions"] = {"labor": {"available": 40}}
    new_plan, _c, _o = apply_patch(plan, _patch("plan", "target_duration", 300))

    assert new_plan["meta"]["boundary_conditions"]["project_duration_days"] == 300
    assert "_source" not in new_plan["meta"]["boundary_conditions"]


def test_target_duration_rejects_non_positive_and_non_numeric():
    plan = _plan()
    before = _snapshot(plan)
    for bad in (0, -5, 0.4, "abc", None, True, float("nan"), float("inf"), [300]):
        _p, changed, out = apply_patch(plan, _patch("plan", "target_duration", bad))
        assert out["applied"] is False, bad
        assert changed == []
        assert "目标总工期" in out["warning"]
    # 数字字符串与小数照收（向下取整成整数天）
    ok, changed, _o = apply_patch(plan, _patch("plan", "target_duration", "120"))
    assert changed == ["plan"] and ok["meta"]["target_duration_days"] == 120
    ok2, _c2, _o2 = apply_patch(plan, _patch("plan", "target_duration", 90.6))
    assert ok2["meta"]["target_duration_days"] == 91
    assert _snapshot(plan) == before


# ==================== 4. add_task：新增任务 ====================
def test_add_task_auto_id_from_host_work_package_and_insert_after_target():
    plan = _plan()          # 工作包 4.1.1；同级叶子 4.1.1.1 / 4.1.1.2 / 4.1.1.3 / 4.1.1.4
    before = _snapshot(plan)
    new_plan, changed, out = apply_patch(
        plan, _patch("4.1.1.1", "add_task",
                     {"name": "Ⅰ区夜间浇筑", "duration_days": 2, "quantity": 30,
                      "unit": "m³", "work_type": "混凝土工程", "crew": {"混凝土工": 6}}))

    assert out["applied"] is True
    # 编号派生自宿主工作包 4.1.1，同级最大后缀 4 → 下一个 4.1.1.5
    assert out["target"] == "4.1.1.5"
    assert changed == ["4.1.1.5"]
    assert "note" in out and "4.1.1.5" in out["note"] and "4.1.1.1" in out["note"]
    assert "warning" not in out
    # 紧跟在目标叶子后面（同一个 sub_packages 列表）
    ids = _ids(new_plan)
    assert ids.index("4.1.1.5") == ids.index("4.1.1.1") + 1
    leaf = find_leaf(new_plan, "4.1.1.5")
    assert leaf["name"] == "Ⅰ区夜间浇筑"
    assert leaf["duration_days"] == 2
    assert leaf["quantity"] == 30
    assert leaf["unit"] == "m³"
    assert leaf["work_type"] == "混凝土工程"
    assert leaf["source"] == "revise"
    assert leaf["norm_binding"]["crew"] == {"混凝土工": 6}
    # 纯函数：入参没被塞进新任务
    assert "4.1.1.5" not in _ids(plan)
    assert _snapshot(plan) == before


def test_add_task_auto_id_deep_work_package():
    """契约里的例子：同级已有 4.1.1.1/4.1.1.2 → 新任务 4.1.1.3。

    另外覆盖"宿主工作包比目标叶子少一级"（工作包 4.1、叶子 4.1.1.1）：前缀取同级
    任务的父编号 4.1.1，得到 4.1.1.2，而不是 4.1.5（那是另一个工作包的编号，
    用户按编号找不到刚加的任务），也不是 4.1.1.1.5 那种多长一截的编号。
    """
    plan = _plan()                                    # 工作包 4.1.1
    subs = plan["wbs"]["phases"][0]["work_packages"][0]["sub_packages"]
    plan["wbs"]["phases"][0]["work_packages"][0]["sub_packages"] = subs[:2]   # 只留 .1 .2
    plan["dependencies"] = [
        {"predecessor": "4.1.1.1", "successor": "4.1.1.2", "type": "FS", "lag_days": 0}]
    plan["all_tasks_schedule"] = plan["all_tasks_schedule"][:2]
    plan["critical_path_tasks"] = plan["critical_path_tasks"][:1]
    assert _ids(plan) == ["4.1.1.1", "4.1.1.2"]
    new_plan, changed, out = apply_patch(
        plan, _patch("4.1.1.1", "add_task", {"name": "补位任务"}))
    assert out["applied"] is True
    assert changed == ["4.1.1.3"]                     # 同级最大后缀 2 → 下一个 3
    assert _ids(new_plan) == ["4.1.1.1", "4.1.1.3", "4.1.1.2"]

    # 工作包比目标叶子少一级（真实计划就是这个形状：wp 4.1，子任务 4.1.1.1）：
    # 前面还有 real-plan 专测；这里只确认编号仍跟着"目标那一组"走，不跳到 4.1.5
    shallow = _plan("4.1")
    assert _ids(shallow) == ["4.1.1", "4.1.2", "4.1.3", "4.1.4"]
    new2, changed2, out2 = apply_patch(
        shallow, _patch("4.1.1", "add_task", {"name": "浅层新增"}))
    assert out2["applied"] is True
    assert changed2 == ["4.1.5"]                      # 同级工作包编号往后顺延
    assert find_leaf(new2, "4.1.5") is not None
    assert find_leaf(new2, changed2[0])["name"] == "浅层新增"


def test_add_task_真实计划的编号形状_工作包比叶子少一级():
    """真实计划：工作包 "4.1" 下面挂着 "4.1.1.1"…"4.1.4.3" 若干组。

    在 4.1.1.1 后面新增，必须得到 4.1.1.4（同一组的下一个），
    绝不能是 4.1.5 —— 那在编号体系里是另一个工作包，用户根本找不到这条新任务。
    """
    plan = _plan("4.1")
    wp = plan["wbs"]["phases"][0]["work_packages"][0]
    wp["sub_packages"] = [
        _leaf("4.1.1.1", "a"), _leaf("4.1.1.2", "b"), _leaf("4.1.1.3", "c"),
        _leaf("4.1.2.1", "d"), _leaf("4.1.2.2", "e"),
        _leaf("4.1.3.1", "f"),
    ]
    before = _ids(plan)
    new_plan, changed, out = apply_patch(
        plan, _patch("4.1.1.1", "add_task", {"name": "新增钢筋复核"}))
    assert out["applied"] is True
    assert changed == ["4.1.1.4"], changed
    assert _ids(new_plan).index("4.1.1.4") == _ids(new_plan).index("4.1.1.1") + 1
    assert set(before) | {"4.1.1.4"} == set(_ids(new_plan))
    # 同组的下一个：4.1.1.3 已存在，不能覆盖，也不能跳到别组
    assert find_leaf(new_plan, "4.1.1.3")["name"] == "c"
    assert find_leaf(new_plan, "4.1.2.1")["name"] == "d"


def test_add_task_wires_fs_edges_and_retargets_successors():
    """新任务必须是"有逻辑关系"的：X → 新 → X 的老后继。"""
    plan = _plan()                                    # 4.1.1.1 → 4.1.1.2 → 4.1.1.4
    new_plan, changed, out = apply_patch(
        plan, _patch("4.1.1.1", "add_task", {"name": "插入任务", "duration_days": 1}))
    new_id = changed[0]
    assert new_id == "4.1.1.5"

    deps = _deps(new_plan)
    assert ("4.1.1.1", new_id) in deps                       # 新增 X → 新
    assert (new_id, "4.1.1.2") in deps                       # X 的老后继改挂到新任务下
    assert ("4.1.1.1", "4.1.1.2") not in deps                # 老边被替换，不留双份
    assert ("4.1.1.2", "4.1.1.4") in deps                    # 与 X 无关的边不动
    assert len([d for d in new_plan["dependencies"]
                if str(d["predecessor"]) == new_id
                and str(d["successor"]) == "4.1.1.2"]) == 1
    # 新任务不再孤立
    assert any(str(d["successor"]) == new_id for d in new_plan["dependencies"])
    # 原来是 2 条边：X 的老后继被改挂（还是 1 条）+ 新增 X → 新 = 3 条
    assert len(new_plan["dependencies"]) == 3


def test_add_task_appends_without_target_and_with_unknown_target():
    plan = _plan()
    before = _snapshot(plan)
    new_plan, changed, out = apply_patch(plan, _patch("", "add_task", {"name": "收尾清理"}))
    new_id = changed[0]
    assert out["applied"] is True
    assert new_id == "4.1.1.5"                        # 追加到第一个工作包 4.1.1 下
    assert _ids(new_plan)[-1] == new_id
    assert find_leaf(new_plan, new_id)["name"] == "收尾清理"
    # 落点不确定时必须如实说明（放进的是第一个工作包，并告知下次怎么定位）
    assert out.get("note")
    assert "4.1.1" in out["note"] and "第一个工作包" in out["note"]
    assert "没有逻辑关系" in out["note"] or "未串前后置" in out["note"]
    # 追加不串逻辑关系
    assert new_plan["dependencies"] == plan["dependencies"]
    assert _snapshot(plan) == before

    # target 不存在 → 也走追加，不抛异常、不拒绝
    new2, changed2, out2 = apply_patch(plan, _patch("9.9.9.9", "add_task", {"name": "又一个"}))
    assert out2["applied"] is True
    assert _ids(new2)[-1] == changed2[0]
    assert find_leaf(new2, changed2[0])["name"] == "又一个"
    assert out2.get("note")
    assert new2["dependencies"] == plan["dependencies"]


def test_add_task_accepts_plain_string_and_avoids_id_collision():
    # value 直接是任务名（口语最常见的形态）
    new_plan, changed, out = apply_patch(_plan(), _patch("4.1.1.1", "add_task", "临时支撑"))
    assert out["applied"] is True
    assert find_leaf(new_plan, changed[0])["name"] == "临时支撑"
    assert find_leaf(new_plan, changed[0])["duration_days"] == 1      # 默认 1 天

    # 显式给的 id 与既有任务撞号 → 顺延出一个不冲突的编号
    plan = _plan()
    with_extra = _ids(plan)
    new2, changed2, out2 = apply_patch(
        plan, _patch("4.1.1.1", "add_task", {"id": "4.1.1.2", "name": "撞号任务"}))
    assert out2["applied"] is True
    assert changed2[0] != "4.1.1.2"
    assert changed2[0] not in with_extra
    assert changed2[0] == "4.1.1.5"
    assert find_leaf(new2, "4.1.1.2")["name"] == "Ⅰ区混凝土浇筑"     # 老任务没被覆盖
    assert find_leaf(new2, changed2[0])["name"] == "撞号任务"
    assert len(_ids(new2)) == len(with_extra) + 1
    assert len(_ids(new2)) == len(set(_ids(new2)))                    # 编号仍唯一
    assert "重复" in out2["note"]


def test_add_task_auto_id_skips_ids_used_elsewhere_in_plan():
    """跨工作包撞号也要避开：编号在整份计划里必须唯一。"""
    plan = _plan()
    plan["wbs"]["phases"].append({"phase": "二次结构", "work_packages": [
        {"id": "5.1", "name": "Ⅱ区", "sub_packages": [_leaf("4.1.1.5", "别处的 4.1.1.5")]}]})
    before_ids = _ids(plan)
    new_plan, changed, out = apply_patch(
        plan, _patch("4.1.1.2", "add_task", {"name": "新增任务"}))
    assert out["applied"] is True
    assert changed[0] not in before_ids
    assert changed[0] == "4.1.1.6"                    # 4.1.1.5 被别处占了，跳到 6
    ids = _ids(new_plan)
    assert len(ids) == len(before_ids) + 1
    assert len(ids) == len(set(ids))                  # 整份计划编号唯一


def test_add_task_to_empty_plan_creates_fallback_phase():
    """空计划也得能加：退回老的兜底 —— 造"修改新增"阶段装它。"""
    new_plan, changed, out = apply_patch({}, _patch("", "add_task", {"name": "第一条"}))
    assert out["applied"] is True
    assert changed == ["新增.1"]
    assert new_plan["wbs"]["phases"][0]["phase"] == "修改新增"
    assert find_leaf(new_plan, "新增.1")["name"] == "第一条"
    assert "note" in out


def test_add_task_目标是没有子项的二级工作包时不许静默丢失():
    """二级工作包本身就是一条任务（没有 sub_packages）时，新任务必须真的进计划。

    旧实现往 _iter_places 造的临时列表里 insert —— 那条新任务哪儿都不在，patch 却
    报 applied=True，重算也就当它不存在。这是静默丢数据，最坏的一种失败。
    正确做法：往同一个阶段的工作包列表里插一个**同级工作包**，且不能把工作包塞进
    自己的 sub_packages（那会让同一任务在 JSON 里出现两次）。
    """
    plan = {
        "overview": {"project_name": "二级工作包计划", "total_duration_days": 10,
                     "planned_start_date": "2026-01-01",
                     "planned_end_date": "2026-01-11"},
        "wbs": {"phases": [{"phase": "临建", "work_packages": [
            {"id": "1.1", "name": "场地平整", "quantity": 1, "unit": "项",
             "duration_days": 3},
            {"id": "1.2", "name": "临时道路", "quantity": 1, "unit": "项",
             "duration_days": 3},
        ]}]},
        "dependencies": [{"predecessor": "1.1", "successor": "1.2",
                          "type": "FS", "lag_days": 0}],
        "all_tasks_schedule": [{"task_id": "1.1", "task_name": "场地平整",
                                "start_date": "2026-01-01", "finish_date": "2026-01-03"}],
        "critical_path_tasks": [{"task_id": "1.1", "task_name": "场地平整"}],
        "meta": {},
    }
    before = _snapshot(plan)
    new_plan, changed, out = apply_patch(
        plan, _patch("1.1", "add_task", {"name": "地下室防水", "duration_days": 4}))

    assert out["applied"] is True
    new_id = changed[0]
    assert new_id == "1.3"                            # 同级工作包 1.1 / 1.2 → 下一个 1.3

    # ① 真的在计划里（能从 iter_leaves 找到），且只有一份
    assert find_leaf(new_plan, new_id) is not None, "新任务没进计划（静默丢失）"
    ids = _ids(new_plan)
    assert ids.count(new_id) == 1
    assert find_leaf(new_plan, new_id)["name"] == "地下室防水"
    assert find_leaf(new_plan, new_id)["duration_days"] == 4

    # ② 就插在目标工作包后面，并且在同一个阶段的工作包列表里
    wps = new_plan["wbs"]["phases"][0]["work_packages"]
    wp_ids = [str(w.get("id")) for w in wps]
    assert wp_ids.index(new_id) == wp_ids.index("1.1") + 1, wp_ids
    # ③ 不许把工作包塞进自己的 sub_packages（同一任务出现两次）
    for w in wps:
        subs = w.get("sub_packages")
        assert not subs or w.get("id") not in [s.get("id") for s in subs], w

    # ④ 依赖照常串：1.1 → 新 → 老的 1.2
    deps = _deps(new_plan)
    assert ("1.1", new_id) in deps
    assert (new_id, "1.2") in deps
    assert ("1.1", "1.2") not in deps
    assert "warning" not in out
    assert _snapshot(plan) == before


def test_add_task_的patch可以重放():
    """修订链靠"重放 patch"重建历史版本，add_task 的 patch 必须可重放。

    这条守两件事：
      ① `insert_after` 记下插入位置 —— 否则重放时找不到"插在谁后面"，会静默改成
         塞进第一个工作包，重建出来的版本和当时写盘的那份不是同一个计划；
      ② `value.id` 把生成的编号写死 —— 否则重放时重新推导，编号一变后面所有指着
         它的 patch 全部打空。
    """
    # --- 定位插入（插在 4.1.1.1 后面）---
    base = _plan()
    recorded = apply_patch(base, _patch("4.1.1.1", "add_task", {"name": "地下室防水"}))[2]
    assert recorded["applied"] is True
    assert recorded["insert_after"] == "4.1.1.1"       # 插入位置留在 patch 里
    assert recorded["target"] == recorded["value"]["id"]   # 上层要靠 target 算影响范围
    assert recorded["value"]["id"] == "4.1.1.5"

    first = apply_patch(base, recorded)[0]             # 第一次应用
    replay = apply_patch(base, recorded)[0]            # 同一份记录再应用一次（重放）
    assert _snapshot(first) == _snapshot(replay)
    # 新任务真的插在目标后面，而不是被塞进第一个工作包
    subs = first["wbs"]["phases"][0]["work_packages"][0]["sub_packages"]
    order = [str(s.get("id")) for s in subs]
    assert order.index("4.1.1.5") == order.index("4.1.1.1") + 1, order

    # 模拟 rebuild()：基线 + 按序重放记录下来的 patch
    chain_base = _plan()
    live, _c0, out0 = apply_patch(chain_base, _patch("4.1.1.1", "add_task",
                                                     {"name": "地下室防水"}))
    again, _c1, _out1 = apply_patch(chain_base, out0)
    assert _snapshot(live) == _snapshot(again), "重放出来的计划与首跑不一致"

    # --- 没指定位置（insert_after 记为 "plan"）---
    base2 = _plan()
    rec2 = apply_patch(base2, _patch("plan", "add_task", {"name": "收尾清理"}))[2]
    assert rec2["insert_after"] == "plan"
    new_id2 = rec2["value"]["id"]
    assert new_id2 == "4.1.1.5"
    first2, _c2, _o2 = apply_patch(base2, rec2)
    replay2, _c3, _o3 = apply_patch(base2, rec2)
    assert _snapshot(first2) == _snapshot(replay2)
    assert find_leaf(first2, new_id2) is not None
    # 没指定位置 → 落在第一个工作包末尾（而不是插到队首）
    subs2 = first2["wbs"]["phases"][0]["work_packages"][0]["sub_packages"]
    assert [str(s.get("id")) for s in subs2][-1] == new_id2
    assert rec2["value"]["name"] == "收尾清理"


def test_add_task_目标是没有子项的二级工作包时编号撞号要顺延():
    """工作包本身就是任务时，指定了已被占用的编号也要往后顺延，且不跨阶段。"""
    plan = {
        "wbs": {"phases": [{"phase": "临建", "work_packages": [
            {"id": "5.1", "name": "场地平整", "quantity": 1},
            {"id": "5.2", "name": "临时道路", "quantity": 1},
            {"id": "5.3", "name": "临时用电", "quantity": 1},
        ]}]},
        "dependencies": [{"predecessor": "5.1", "successor": "5.2",
                          "type": "FS", "lag_days": 0}],
        "meta": {},
    }
    before = _snapshot(plan)
    new_plan, changed, out = apply_patch(
        plan, _patch("5.1", "add_task", {"id": "5.2", "name": "地下室防水"}))
    assert out["applied"] is True
    # 5.2 已被占用 → 顺延到 5.3 之后的 5.4，绝不能变成 5.2 的上级 5 → "6"
    new_id = changed[0]
    assert new_id not in ("5.2",), new_id
    assert new_id.startswith("5."), new_id
    wps = new_plan["wbs"]["phases"][0]["work_packages"]
    assert [str(w.get("id")) for w in wps] == ["5.1", new_id, "5.2", "5.3"]
    assert find_leaf(new_plan, "5.2")["name"] == "临时道路"      # 没被覆盖
    assert _ids(new_plan).count("5.2") == 1
    assert find_leaf(new_plan, new_id)["name"] == "地下室防水"
    assert _snapshot(plan) == before


def test_add_task_defaults_and_min_one_day():
    plan = _plan()
    new_plan, changed, _o = apply_patch(plan, _patch("4.1.1.1", "add_task", {}))
    leaf = find_leaf(new_plan, changed[0])
    assert leaf["name"] == changed[0]                 # 没给名字就用编号
    assert leaf["quantity"] == 0
    assert leaf["unit"] == ""
    assert leaf["duration_days"] == 1
    assert leaf["work_type"] == ""
    assert "norm_binding" not in leaf

    # 0 / 负数工期被抬到 1 天，小数向上取整
    for raw, expect in ((0, 1), (-3, 1), (0.2, 1), (2.5, 3)):
        out_plan, ch, _o2 = apply_patch(
            plan, _patch("4.1.1.1", "add_task", {"duration_days": raw}))
        assert find_leaf(out_plan, ch[0])["duration_days"] == expect, raw

    # crew 非法值 → 0 人，不抛异常
    out3, ch3, _o3 = apply_patch(
        plan, _patch("4.1.1.1", "add_task", {"crew": {"木工": "很多人"}}))
    assert find_leaf(out3, ch3[0])["norm_binding"]["crew"] == {"木工": 0}


# ==================== 5. remove_task：删除任务 ====================
def test_remove_task_drops_leaf_deps_and_schedule_rows():
    plan = _plan()
    before = _snapshot(plan)
    new_plan, changed, out = apply_patch(plan, _patch("4.1.1.2", "remove_task", None))

    assert out["applied"] is True
    assert changed == ["4.1.1.2"]
    assert "4.1.1.2" not in _ids(new_plan)
    # 相关依赖全删干净，并补回 4.1.1.1 → 4.1.1.4
    for d in new_plan["dependencies"]:
        assert str(d["predecessor"]) != "4.1.1.2"
        assert str(d["successor"]) != "4.1.1.2"
    assert _deps(new_plan) == [("4.1.1.1", "4.1.1.4")]
    assert "note" in out and "4.1.1.1→4.1.1.4" in out["note"]
    # 冗余任务表同步删行
    assert "4.1.1.2" not in [r["task_id"] for r in new_plan["all_tasks_schedule"]]
    assert "4.1.1.2" not in [r["task_id"] for r in new_plan["critical_path_tasks"]]
    assert len(new_plan["all_tasks_schedule"]) == 3
    # 别的行原样保留
    assert _row(new_plan, "4.1.1.4", "task_name") == "Ⅰ区验收"
    assert new_plan["critical_path_tasks"][1]["task_id"] == "4.1.1.98"
    assert _snapshot(plan) == before


def test_remove_task_bridge_deduped_when_pair_already_exists():
    """p → s 已经存在时不要再补一条重复依赖。"""
    plan = _plan()
    plan["dependencies"].append({"predecessor": "4.1.1.1", "successor": "4.1.1.4",
                                 "type": "FS", "lag_days": 0})
    new_plan, changed, out = apply_patch(plan, _patch("4.1.1.2", "remove_task", None))
    assert out["applied"] is True
    pair = [d for d in new_plan["dependencies"]
            if str(d["predecessor"]) == "4.1.1.1" and str(d["successor"]) == "4.1.1.4"]
    assert len(pair) == 1
    # 除了原有的 4.1.1.1→4.1.1.4，不该冒出别的边
    assert _deps(new_plan) == [("4.1.1.1", "4.1.1.4")]


def test_remove_task_without_edges_and_without_schedule_tables():
    # 没有前后置的孤立任务：能删，无需补链，note 说明清楚
    plan = _plan()
    new_plan, changed, out = apply_patch(plan, _patch("4.1.1.3", "remove_task", None))
    assert out["applied"] is True and changed == ["4.1.1.3"]
    assert "4.1.1.3" not in _ids(new_plan)
    assert "note" in out
    assert _deps(new_plan) == [("4.1.1.1", "4.1.1.2"), ("4.1.1.2", "4.1.1.4")]

    # dependencies / 任务表缺失或坏掉也不能炸
    plan2 = _plan()
    plan2.pop("dependencies")
    plan2["all_tasks_schedule"] = "坏值"
    plan2.pop("critical_path_tasks")
    new2, changed2, out2 = apply_patch(plan2, _patch("4.1.1.1", "remove_task", None))
    assert out2["applied"] is True and changed2 == ["4.1.1.1"]
    assert "4.1.1.1" not in _ids(new2)
    assert "dependencies" not in new2                # 本来就没有，不凭空造一个


def test_remove_task_requires_existing_target():
    plan = _plan()
    before = _snapshot(plan)
    for target in ("9.9.9.9", "", None, PLAN_TARGET):
        out_plan, changed, out = apply_patch(plan, _patch(target, "remove_task", None))
        assert out["applied"] is False, target
        assert changed == []
        assert "找不到" in out["warning"]
        assert _ids(out_plan) == _ids(plan)           # 拒绝时计划不变
    assert _snapshot(plan) == before


# ==================== 6. 组合与重放 ====================
def test_phase3_patches_replay_deterministically():
    """整条链重放（改开工 → 改名 → 删任务 → 加任务）结果稳定且幂等重放一致。"""
    plan = _plan()
    patches = [
        _patch("plan", "start_date", "2026-02-01"),
        _patch("plan", "target_duration", 280),
        _patch("4.1.1.3", "name", "养护（保温）"),
        _patch("4.1.1.2", "remove_task", None),
        _patch("4.1.1.1", "add_task", {"name": "新增钢筋复核", "duration_days": 1}),
    ]
    first = plan
    for p in patches:
        first, _changed, out = apply_patch(first, p)
        assert out["applied"] is True, (p, out)

    second = plan
    for p in patches:
        second, _changed, _out = apply_patch(second, p)
    assert _snapshot(first) == _snapshot(second)

    assert first["overview"]["planned_start_date"] == "2026-02-01"
    assert first["meta"]["target_duration_days"] == 280
    assert first["meta"]["boundary_conditions"]["project_duration_days"] == 280
    assert "4.1.1.2" not in _ids(first)
    assert find_leaf(first, "4.1.1.3")["name"] == "养护（保温）"
    # 逐条任务表里也没有被删掉的那一行，且新开工日期已经落进每一行
    assert "4.1.1.2" not in [r["task_id"] for r in first["all_tasks_schedule"]]
    assert all(r["start_date"] >= "2026-02-01" for r in first["all_tasks_schedule"])


def test_every_new_branch_is_pure():
    """逐个新分支跑一遍：任何一次都不能改动传入的 plan。"""
    plan = _plan()
    before = _snapshot(plan)
    cases = [
        _patch("4.1.1.1", "name", "改名"),
        _patch("plan", "start_date", "2026-03-01"),
        _patch("plan", "target_duration", 300),
        _patch("4.1.1.1", "add_task", {"name": "新增", "duration_days": 2}),
        _patch("4.1.1.2", "remove_task", None),
        _patch("4.1.1.1", "add_task", {"id": "4.1.1.1", "name": "撞号"}),
        _patch("plan", "start_date", "不是日期"),
        _patch("4.1.1.1", "name", ""),
        _patch("plan", "target_duration", -1),
        _patch("9.9.9.9", "remove_task", None),
    ]
    for p in cases:
        out_plan, _changed, _out = apply_patch(plan, p)
        assert out_plan is not plan, p
        assert _snapshot(plan) == before, p


def test_patch_dict_not_mutated_and_note_only_on_success():
    plan = _plan()
    for field, value in (("name", "新名"), ("start_date", "2026-02-01"),
                         ("target_duration", 200)):
        p = _patch("plan" if field != "name" else "4.1.1.1", field, value)
        snapshot = json.dumps(p, ensure_ascii=False, sort_keys=True)
        _plan_out, _changed, out = apply_patch(plan, p)
        assert json.dumps(p, ensure_ascii=False, sort_keys=True) == snapshot
        assert "applied" not in p                    # 原 patch 不被就地改
        assert out["applied"] is True
        assert "warning" not in out
    # 失败时不能带 note（note 只表示"成功了但有话要说"）
    for bad in (_patch("plan", "start_date", "坏日期"), _patch("plan", "target_duration", 0),
                _patch("4.1.1.1", "name", ""), _patch("9.9.9.9", "remove_task", None)):
        _o, _c, out = apply_patch(plan, bad)
        assert out["applied"] is False, bad
        assert out.get("warning")
        assert "note" not in out, bad
    # 成功的删任务带 note，但绝不能带 warning（否则上层会当成"没删掉"）
    _o2, _c2, out2 = apply_patch(plan, _patch("4.1.1.2", "remove_task", None))
    assert out2["applied"] is True and out2.get("note")
    assert "warning" not in out2


if __name__ == "__main__":                            # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
