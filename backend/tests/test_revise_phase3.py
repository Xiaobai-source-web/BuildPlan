# -*- coding: utf-8 -*-
"""Phase 3 能力扩展测试 —— 让「修改模块」真的能改东西（第 36 轮）

背景：用户实测「我想修改这个项目名为NUS大楼」→ 系统回「修改项目名称属于计划级别的
变更，目前我无法直接执行此操作」，并说「基本什么都改不了」。第 35/36 轮先修了入口与
改名，Phase 3 补上真正缺的那批能力：

  · 工序改名（name）
  · 增 / 删一条工序（add_task / remove_task）
  · 开工日期（start_date，整份日程平移，不重跑排程）
  · 总工期目标（target_duration，只记目标 + 如实报差距，**不假装能压缩工期**）
  · 一条都没生效时，必须回"做不到什么 + 能做的是…"（capability_hint）

本模块守住三件事：
  ① 新句式能被规则解析出来；② 旧句式不被新句式劫持（改错对象比改不动更糟）；
  ③ 新能力落地后**副作用边界**正确（改名不动工期、开工日期只动日历、
     总工期目标不动任何一条工期）。

运行：
  python -m pytest backend/tests/test_revise_phase3.py -q -p no:cacheprovider
"""

import copy
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline.nodes import revise as R
from pipeline.nodes.revise import (ReviseNode, build_revise_items, capability_hint)
from pipeline.plan_store import apply_patch, iter_leaves
from pipeline.recompute import recompute_after_revision


# ==================== 小计划（A → B → C 串行 + 日程 + 依赖）====================
def _leaf(tid, name, qty, days, crew="钢筋工", n=5):
    return {
        "id": tid, "name": name, "duration_days": days, "quantity": qty,
        "unit": "t", "work_type": "钢筋工程",
        "norm_binding": {
            "task_id": tid, "mode": "labor", "productivity_value": 1.0,
            "source_code": "LD_T72_7_2008", "match_type": "exact",
            "labor_types": [crew], "crew": {crew: n},
        },
    }


def _plan():
    return {
        "plan_id": "p3_test",
        "overview": {"project_name": "未命名施工项目", "total_duration_days": 9,
                     "planned_start_date": "2026-01-01",
                     "planned_end_date": "2026-01-10", "critical_path_length": 1},
        "meta": {"level": "L4",
                 "extracted_params": {"planned_start_date": "2026-01-01"}},
        "wbs": {"phases": [{"phase": "主体结构", "work_packages": [
            {"id": "5.1", "name": "Ⅰ区主体", "sub_packages": [
                _leaf("5.1.1.1", "钢筋绑扎", 100.0, 3),
                _leaf("5.1.1.2", "模板安装", 100.0, 3, crew="模板工", n=4),
                _leaf("5.1.1.3", "混凝土浇筑", 100.0, 3, crew="混凝土工", n=6),
            ]}]}]},
        "dependencies": [
            {"predecessor": "5.1.1.1", "successor": "5.1.1.2", "type": "FS", "lag_days": 0},
            {"predecessor": "5.1.1.2", "successor": "5.1.1.3", "type": "FS", "lag_days": 0},
        ],
        "all_tasks_schedule": [
            {"task_id": "5.1.1.1", "task_name": "钢筋绑扎", "start_date": "2026-01-01",
             "finish_date": "2026-01-03"},
            {"task_id": "5.1.1.2", "task_name": "模板安装", "start_date": "2026-01-04",
             "finish_date": "2026-01-06"},
            {"task_id": "5.1.1.3", "task_name": "混凝土浇筑", "start_date": "2026-01-07",
             "finish_date": "2026-01-09"},
        ],
        "critical_path_tasks": [
            {"task_id": "5.1.1.1", "task_name": "钢筋绑扎",
             "start_date": "2026-01-01", "finish_date": "2026-01-03"},
            {"task_id": "5.1.1.2", "task_name": "模板安装",
             "start_date": "2026-01-04", "finish_date": "2026-01-06"}],
        "resource_plan": {"total_manpower_days": 0.0, "peak_manpower": 0,
                          "equipment_peak": {}, "material_summary": []},
    }


ITEMS = build_revise_items(_plan())


def run(text, plan=None):
    """走真实 ReviseNode + 真实重算回调，返回 (revision, 改后的计划)。"""
    plan = copy.deepcopy(plan if plan is not None else _plan())
    node = ReviseNode(store=None, recompute=recompute_after_revision)
    res = node.run({"plan_json": plan, "user_instruction": text, "plan_id": "p3"})
    return res.get("revision") or {}, res.get("plan_json") or plan


def durations(plan):
    return {str(l.get("id")): l.get("duration_days") for l in iter_leaves(plan)}


# ==================== ① 规则解析：新句式 ====================
@pytest.mark.parametrize("text,field,target", [
    # 工序改名
    ("把 5.1.1.1 的名字改成 地下室防水", "name", "5.1.1.1"),
    ("5.1.1.2 改名为 模板加固", "name", "5.1.1.2"),
    ("把 5.1.1.3 的名称改为 混凝土二次浇筑", "name", "5.1.1.3"),
    # 开工日期
    ("把开工日期改到 2026-07-01", "start_date", "plan"),
    ("开工日期设为 2026-7-1", "start_date", "plan"),
    ("开工日期调整到 2027-03-15", "start_date", "plan"),
    # 总工期目标
    ("总工期改成 306 天", "target_duration", "plan"),
    ("把总工期压到 300 天", "target_duration", "plan"),
    ("总工期缩短到三十天", "target_duration", "plan"),
    # 增工序（不带位置 / 带位置）
    ("增加一个工序：地下室防水", "add_task", "plan"),
    ("新增工序：屋面保温", "add_task", "plan"),
    ("再加一道工序 基坑支护", "add_task", "plan"),
    ("在 5.1.1.1 后面增加一个工序：地下室防水", "add_task", "5.1.1.1"),
    # 删工序
    ("删除 5.1.1.1 这条工序", "remove_task", "5.1.1.1"),
    ("把 5.1.1.2 删掉", "remove_task", "5.1.1.2"),
    ("去掉 5.1.1.3", "remove_task", "5.1.1.3"),
])
def test_新句式都能被规则解析(text, field, target):
    patches = R._rule_patches(text, ITEMS)
    assert patches, "一句都没解析出来：%s" % text
    got = patches[0]
    assert got.get("field") == field, "字段判错：%r → %r" % (text, got)
    assert str(got.get("target")) == target, "目标判错：%r → %r" % (text, got)
    ok, why, _ = R._validate(_plan(), ITEMS, got)
    assert ok, "解析出来了却过不了校验：%s（%s）" % (text, why)


@pytest.mark.parametrize("text,forbidden", [
    # 改工程量不能被当成"新增工序"
    ("把 5.1.1.1 的工程量增加到 1200", ("add_task", "start_date", "target_duration")),
    # 普通工序字段不能被当成计划级
    ("把 5.1.1.1 的工期改成 20", ("add_task", "start_date", "target_duration", "name")),
    ("把 5.1.1.1 的工程量改成 1200", ("add_task", "start_date", "target_duration", "name")),
    # 计划改名不能被工序改名抢走
    ("把项目名称改为NUS大楼", ("name", "add_task")),
    ("我想修改这个项目名为NUS大楼", ("name", "add_task")),
    # 工序改名不能被计划改名抢走（名词组同形，这是最危险的一条）
    ("把 5.1.1.1 的名字改成 地下室防水", ("plan_title",)),
    ("5.1.1.2 改名为 模板加固", ("plan_title",)),
])
def test_旧句式不被新句式劫持(text, forbidden):
    fields = set(p.get("field") for p in R._rule_patches(text, ITEMS))
    hit = fields & set(forbidden)
    assert not hit, "%r 被误判成 %s（实际 %s）" % (text, "/".join(sorted(hit)), fields)


# ==================== ② 清单：计划项 + 逐项字段 ====================
def test_清单第一条是计划项且只列计划级字段():
    items = build_revise_items(_plan())
    assert items[0]["target"] == "plan"
    for f in ("plan_title", "start_date", "target_duration", "add_task"):
        assert f in items[0]["fields"]
    for f in ("quantity", "duration", "crew"):
        assert f not in items[0]["fields"], "计划项不该列工序级字段"


def test_工序项列了改名与删除但不列计划级字段():
    items = build_revise_items(_plan())[1:]
    assert items, "应该有工序项"
    for it in items:
        for f in ("name", "remove_task", "quantity", "duration", "norm", "crew"):
            assert f in it["fields"], "%s 缺字段 %s" % (it["target"], f)
        for f in ("plan_title", "start_date", "target_duration", "add_task"):
            assert f not in it["fields"], "%s 不该有 %s" % (it["target"], f)


# ==================== ③ 校验：新字段的准入 ====================
def _patch(field, value, target="plan"):
    return {"target": target, "field": field, "value": value}


@pytest.mark.parametrize("value,ok", [
    ("2026-07-01", True), ("2026/7/1", True), ("2026-7-1", True),
    ("2026-13-01", False), ("下个月", False), ("", False), ("20260701", False),
])
def test_开工日期的格式校验(value, ok):
    got, why, norm = R._validate(_plan(), ITEMS, _patch("start_date", value))
    assert got is ok, "开工日期 %r 判成了 %s（%s）" % (value, got, why)
    if ok:
        assert norm["value"] == "2026-07-01" or norm["value"].startswith("2026-")


@pytest.mark.parametrize("value,ok", [(306, True), ("306", True), (1, True),
                                      (0, False), (-5, False), ("很久", False), (None, False)])
def test_总工期目标的取值校验(value, ok):
    got, _why, _norm = R._validate(_plan(), ITEMS, _patch("target_duration", value))
    assert got is ok


@pytest.mark.parametrize("value,ok", [
    ({"name": "地下室防水"}, True), ("地下室防水", True),
    ({}, False), ({"name": ""}, False), ({"name": "x" * 41}, False), (None, False),
])
def test_新增工序的名字校验(value, ok):
    got, _why, _norm = R._validate(_plan(), ITEMS, _patch("add_task", value))
    assert got is ok


def test_新增工序的位置必须是清单里的工序():
    """位置写错不许静默退回"塞进第一个工作包" —— 用户说了插在哪，就得插在那。"""
    ok, why, _ = R._validate(_plan(), ITEMS, _patch("add_task", {"name": "地下室防水"},
                                                    target="9.9.9"))
    assert not ok and "9.9.9" in why
    ok2, _w, norm2 = R._validate(_plan(), ITEMS, _patch("add_task", {"name": "地下室防水"},
                                                        target="5.1.1.1"))
    assert ok2 and norm2["target"] == "5.1.1.1"


@pytest.mark.parametrize("value,ok", [("地下室防水", True), ("", False),
                                      ("x" * 41, False), (None, False)])
def test_工序改名的取值校验(value, ok):
    got, _why, _norm = R._validate(_plan(), ITEMS, _patch("name", value, target="5.1.1.1"))
    assert got is ok


# ==================== ④ 端到端：副作用边界 ====================
def test_工序改名只改名字_一条工期都不许动():
    before = durations(_plan())
    rev, after = run("把 5.1.1.1 的名字改成 地下室防水")
    assert [p["field"] for p in rev["applied"]] == ["name"]
    assert durations(after) == before, "改名把工期改了：%s" % durations(after)
    names = [l.get("name") for l in iter_leaves(after) if str(l.get("id")) == "5.1.1.1"]
    assert names == ["地下室防水"]
    # 日程表里的冗余名字也要同步，否则看板上还是旧名字
    sched = {str(r.get("task_id")): r.get("task_name")
             for r in (after.get("all_tasks_schedule") or [])}
    assert sched.get("5.1.1.1") == "地下室防水"
    assert after["critical_path_tasks"][0]["task_name"] == "地下室防水"


def test_改名的影响范围不许说是下游一大片():
    """改名不可能影响别的任务；旧实现会跟着依赖闭包报"受影响 199 项"。"""
    rev, _ = run("把 5.1.1.1 的名字改成 地下室防水")
    assert rev["affected"] == ["5.1.1.1"], rev["affected"]


def test_开工日期整体平移_工期不变():
    before = durations(_plan())
    rev, after = run("把开工日期改到 2026-02-01")
    assert [p["field"] for p in rev["applied"]] == ["start_date"]
    assert after["overview"]["planned_start_date"] == "2026-02-01"
    assert durations(after) == before
    rows = {str(r.get("task_id")): r for r in (after.get("all_tasks_schedule") or [])}
    # 2026-01-01 → 2026-02-01 是 +31 天
    assert rows["5.1.1.1"]["start_date"] == "2026-02-01"
    assert rows["5.1.1.2"]["start_date"] == "2026-02-04"
    assert rows["5.1.1.3"]["finish_date"] == "2026-02-09"
    # 提示不能自相矛盾：日期确实动了，就不许说"日期保持原样"
    joined = " ".join(rev["warnings"])
    assert "保持原样" not in joined or "开工日期" not in joined
    assert "平移" in joined


def test_总工期目标只记目标_不重排_并如实报差距():
    before = durations(_plan())
    rev, after = run("总工期改成 3 天")
    assert [p["field"] for p in rev["applied"]] == ["target_duration"]
    assert after["meta"]["target_duration_days"] == 3
    assert after["meta"]["boundary_conditions"]["project_duration_days"] == 3
    assert durations(after) == before, "记个总工期目标竟然把工期改了"
    joined = " ".join(rev["warnings"])
    assert "差" in joined and "压缩" in joined, joined


def test_新增工序_跟着目标串进依赖链并自动编号():
    rev, after = run("在 5.1.1.1 后面增加一个工序：地下室防水")
    assert [p["field"] for p in rev["applied"]] == ["add_task"]
    ids = [str(l.get("id")) for l in iter_leaves(after)]
    assert "5.1.1.4" in ids, "自动编号不对：%s" % ids
    # 物理位置紧跟目标
    subs = after["wbs"]["phases"][0]["work_packages"][0]["sub_packages"]
    order = [str(s.get("id")) for s in subs]
    assert order.index("5.1.1.4") == order.index("5.1.1.1") + 1, order
    deps = [(str(d.get("predecessor")), str(d.get("successor")))
            for d in (after.get("dependencies") or [])]
    assert ("5.1.1.1", "5.1.1.4") in deps, "没串前驱：%s" % deps
    assert ("5.1.1.4", "5.1.1.2") in deps, "老后继没挂到新任务下：%s" % deps


def test_删除工序_断开的依赖要接回而不是留一个洞():
    rev, after = run("删除 5.1.1.2 这条工序")
    assert [p["field"] for p in rev["applied"]] == ["remove_task"]
    ids = [str(l.get("id")) for l in iter_leaves(after)]
    assert "5.1.1.2" not in ids
    deps = [(str(d.get("predecessor")), str(d.get("successor")))
            for d in (after.get("dependencies") or [])]
    assert not any("5.1.1.2" in pair for pair in deps), "残留依赖：%s" % deps
    assert ("5.1.1.1", "5.1.1.3") in deps, "没把链接回：%s" % deps
    assert not [r for r in (after.get("all_tasks_schedule") or [])
                if str(r.get("task_id")) == "5.1.1.2"]


# ==================== ⑤ 能力边界：必须说出来 ====================
@pytest.mark.parametrize("text", [
    "把层数改成 5 层", "栋数改成 3 栋", "建筑面积改成 20000 平方米",
    "把塔吊增加到 2 台", "调整一下紧前关系", "把机械投入加大",
])
def test_做不到的意图要给能力说明而不是沉默(text):
    hint = capability_hint(text)
    assert hint, "「%s」没给出任何能力说明" % text
    assert "我能直接改的是" in hint
    rev, _after = run(text)
    assert not rev["applied"]
    assert rev.get("hint"), "revision 里没有 hint，终端就没得显示"
    assert rev["hint"] in rev["summary"], "摘要里也应该带上，终端两条路都要能看到"


def test_能改的句子不许冒出做不到的说明():
    rev, _ = run("把 5.1.1.1 的工期改成 5 天")
    assert rev["applied"], rev
    assert not rev.get("hint")


# ==================== ⑥ 回归：不许把 add_task 的字典当班组去卡上限 ====================
def test_工作面容量校验不许碰新增工序的字典值():
    warnings = []
    spec = {"name": "地下室防水", "crew": {"钢筋工": 999}}
    out = R._clamp_crew(_plan(), _patch("add_task", spec, target="5.1.1.1"), warnings)
    assert out["value"] == spec, "add_task 的值被当成班组人数改了：%r" % out["value"]
    assert not warnings


# ==================== ⑧ 回归：无位置新增绝不许把同一条任务放两处 ====================
def test_无位置新增只加一条_且编号全局唯一():
    """真实计划上抓到的严重缺陷：无位置新增会**追加进第一个工作包**之后又走一遍
    "造一个『修改新增』阶段"的老兜底，把**同一个 dict 对象**放进两处 ——
    实测 209 → 211 条、`1.1.3` 出现两次（两个位置指向同一个 dict）。
    排程与依赖表按编号认任务，会把它们当成同一条：用户改一条等于改两条。
    """
    base = _plan()
    n0 = len(list(iter_leaves(base)))
    out, _changed, patch = apply_patch(base, {"target": "plan", "field": "add_task",
                                              "value": {"name": "地下室防水"}})
    assert patch.get("applied")
    ids = [str(l.get("id")) for l in iter_leaves(out)]
    assert len(ids) == n0 + 1, "叶子数应为 %d，实际 %d（新增被放了两次）" % (n0 + 1, len(ids))
    assert len(set(ids)) == len(ids), "出现了重复编号：%s" % ids
    # 同一个 dict 不许被引用两次
    objs = [id(l) for l in iter_leaves(out)]
    assert len(set(objs)) == len(objs), "同一个任务对象被放进了多处"
    # 没有凭空多出一个"修改新增"阶段
    assert "修改新增" not in [str(p.get("phase")) for p in out["wbs"]["phases"]] or \
        len(out["wbs"]["phases"]) == len(base["wbs"]["phases"]) + 1, "凭空多了阶段"
    assert len(out["wbs"]["phases"]) == len(base["wbs"]["phases"]), "不该新建阶段"


@pytest.mark.parametrize("target", ["5.1.1.1", "5.1.1.2", "5.1.1.3", "plan", "9.9.9"])
def test_各种新增目标都不产生重复编号(target):
    out, _changed, patch = apply_patch(_plan(), {"target": target, "field": "add_task",
                                                 "value": {"name": "新工序"}})
    assert patch.get("applied"), patch.get("warning")
    ids = [str(l.get("id")) for l in iter_leaves(out)]
    assert len(set(ids)) == len(ids), "重复编号：%s" % [x for x in ids if ids.count(x) > 1]


def test_删掉一条再补一条也不产生重复编号():
    p1, _c, r1 = apply_patch(_plan(), {"target": "5.1.1.2", "field": "remove_task",
                                       "value": None})
    assert r1.get("applied")
    p2, _c2, r2 = apply_patch(p1, {"target": "5.1.1.2", "field": "add_task",
                                   "value": {"name": "补回来"}})
    assert r2.get("applied")
    ids = [str(l.get("id")) for l in iter_leaves(p2)]
    assert len(set(ids)) == len(ids), "重复编号：%s" % [x for x in ids if ids.count(x) > 1]


# ==================== ⑩ 对抗性验证揪出来的坑（逐条回归）====================
def _plan_with_milestones_and_crit():
    p = _plan()
    p["key_milestones"] = [{"name": "开工", "date": "2026-01-01", "task_id": "5.1.1.1"}]
    return p


def test_开工日期平移必须带上关键路径与里程碑():
    """同一个计划里三处日期必须一起走。

    早年只平了 `all_tasks_schedule`，于是任务表已经到 2 月了、关键路径表与里程碑
    还写着 1 月 —— 看板与 Word 会显示互相矛盾的日期。
    """
    p = _plan_with_milestones_and_crit()
    out, _c, patch = apply_patch(p, {"target": "plan", "field": "start_date",
                                     "value": "2026-02-01"})
    assert patch.get("applied")
    rows = {str(r["task_id"]): r for r in out["all_tasks_schedule"]}
    crit = {str(r["task_id"]): r for r in out["critical_path_tasks"]}
    assert rows["5.1.1.1"]["start_date"] == "2026-02-01"
    assert crit["5.1.1.1"]["start_date"] == rows["5.1.1.1"]["start_date"], \
        "关键路径表的日期没跟着平移：%s vs %s" % (crit["5.1.1.1"], rows["5.1.1.1"])
    assert crit["5.1.1.1"]["finish_date"] == rows["5.1.1.1"]["finish_date"]
    assert out["key_milestones"][0]["date"] == "2026-02-01", out["key_milestones"]


@pytest.mark.parametrize("value", ["9999-12-31", "0001-01-01"])
def test_极端开工日期不许抛异常且日程必须自洽(value):
    """`apply_patch` 的底线是"任何输入都不抛异常"，但**仅仅不抛**还不够（第 36 轮）。

    早先的实现吞掉日期越界、原样留下旧值，于是返回的日程里 `finish_date` 早于
    `start_date`、竣工早于开工 —— 而 `applied=True` 且没有任何提示。所以这里断言的
    是**自洽性**：要么整条被拒，要么每一条 `start <= finish`、`开工 <= 竣工`。
    """
    out, _c, patch = apply_patch(_plan_with_milestones_and_crit(),
                                 {"target": "plan", "field": "start_date", "value": value})
    assert isinstance(out, dict)
    if not patch.get("applied"):
        assert patch.get("warning"), "拒了就得说明为什么"
        return
    ov = out["overview"]
    assert ov["planned_start_date"] <= ov["planned_end_date"], \
        "竣工早于开工：%s > %s" % (ov["planned_start_date"], ov["planned_end_date"])
    for row in (out.get("all_tasks_schedule") or []):
        if row.get("start_date") and row.get("finish_date"):
            assert row["start_date"] <= row["finish_date"], \
                "任务 %s 的 finish 早于 start：%s" % (row.get("task_id"), row)
            assert row["start_date"] >= ov["planned_start_date"], \
                "任务 %s 排到了开工日期之前：%s" % (row.get("task_id"), row)


def test_日期上限之外的平移要整条拒绝而不是只平一半():
    rev, after = run("把开工日期改到 9999-12-31")
    assert not rev["applied"], "越界的开工日期不该生效"
    assert rev["rejected"], "必须如实拦下"
    assert "上限" in " ".join(str(r.get("reason") or "") for r in rev["rejected"])
    assert after["overview"]["planned_start_date"] == "2026-01-01", "被拒了就不许动计划"


@pytest.mark.parametrize("total", [10 ** 9, -10 ** 9, 10 ** 6])
def test_总工期是天文数字时开工日期也不许抛异常(total):
    p = _plan_with_milestones_and_crit()
    p["overview"]["total_duration_days"] = total
    out, _c, patch = apply_patch(p, {"target": "plan", "field": "start_date",
                                     "value": "2026-02-01"})
    assert isinstance(out, dict) and patch.get("applied") is not None


def test_新增工序的显式编号撞上工作包必须被拒():
    """工作包有了子工序后自己就不再是叶子，只跟叶子比会漏掉这种撞号。"""
    # 夹具里 "5.1" 是工作包编号
    ok, why, _ = R._validate(_plan(), ITEMS,
                            _patch("add_task", {"id": "5.1", "name": "撞工作包"},
                                   target="5.1.1.1"))
    assert not ok, "撞上工作包编号竟然放行了"
    assert "5.1" in why and "占用" in why, why
    # 撞已有叶子同样要拒
    ok2, _w2, _ = R._validate(_plan(), ITEMS,
                              _patch("add_task", {"id": "5.1.1.2", "name": "撞叶子"},
                                     target="5.1.1.1"))
    assert not ok2
    # 没被占用的编号照旧放行
    ok3, _w3, _n3 = R._validate(_plan(), ITEMS,
                                _patch("add_task", {"id": "5.1.1.9", "name": "新"},
                                       target="5.1.1.1"))
    assert ok3


def test_改单条工序的开工日期必须被拦下而不是平移整份计划():
    """「把 5.1.1.1 的开工日期改到 X」是在改**一条工序**，而支持的是整份计划的开工日期。
    默默把整个计划平移过去是"改错对象"，比改不动更糟 —— 必须如实拦下。
    """
    rev, after = run("把 5.1.1.1 的开工日期改到 2026-07-01")
    assert not rev["applied"], rev["applied"]
    assert rev["rejected"], "应当有一条被拦下的说明"
    reason = " ".join(str(r.get("reason") or "") for r in rev["rejected"])
    assert "开工日期" in reason and "单条" in reason, reason
    # 整份日程一条都不许动
    rows = {str(r["task_id"]): r for r in (after.get("all_tasks_schedule") or [])}
    assert rows["5.1.1.1"]["start_date"] == "2026-01-01", rows["5.1.1.1"]
    assert after["overview"]["planned_start_date"] == "2026-01-01"


def test_无点号编号的改名要落到工序上而不是项目名():
    """计划里若有顶级编号（"5"），工序改名的前两个模式都要求点号会漏掉，
    漏掉的后果是掉进计划改名 —— 用户想改一条工序，项目名被改了。"""
    patches = R._rule_patches("把 5 的名字改成 地下室防水", ITEMS)
    assert patches, "没解析出来"
    assert patches[0]["field"] == "name", patches[0]
    assert str(patches[0]["target"]) == "5", patches[0]


def test_第一个工作包本身就是任务时无位置新增也要真的进计划():
    """实测过的静默丢数据：`_iter_places` 对"没有 sub_packages 的工作包"给的是
    临时列表，append 进去会被丢掉 —— 新增任务哪儿都不在，patch 却报成功。"""
    p = {"overview": {"total_duration_days": 10}, "meta": {},
         "wbs": {"phases": [{"phase": "临建", "work_packages": [
             {"id": "1.1", "name": "场地平整", "duration_days": 3},
             {"id": "1.2", "name": "临时道路", "duration_days": 3}]}]},
         "dependencies": [{"predecessor": "1.1", "successor": "1.2", "type": "FS"}]}
    ids0 = [str(l.get("id")) for l in iter_leaves(p)]
    out, changed, patch = apply_patch(p, {"target": "plan", "field": "add_task",
                                          "value": {"name": "收尾清理"}})
    assert patch.get("applied")
    ids = [str(l.get("id")) for l in iter_leaves(out)]
    assert changed and str(changed[0]) in ids, \
        "报成功但计划里没有它：changed=%s ids=%s" % (changed, ids)
    assert len(ids) == len(ids0) + 1, ids
    assert len(set(ids)) == len(ids), "重复编号：%s" % ids


def test_插入编号要跟着锚点同级而不是跳到别组():
    p = _plan()
    subs = p["wbs"]["phases"][0]["work_packages"][0]["sub_packages"]
    subs.append({"id": "5.1.2", "name": "另一组", "duration_days": 1})
    out, changed, patch = apply_patch(p, {"target": "5.1.2", "field": "add_task",
                                          "value": {"name": "新"}})
    assert patch.get("applied")
    assert str(changed[0]) == "5.1.3", \
        "插在 5.1.2 之后应得到同级的 5.1.3，实际 %s" % changed[0]


# ==================== ⑨ 修订链：新增工序必须可重放（/undo /goto 靠它）====================
def test_新增工序落进修订链后能重建成同一个计划():
    """`rebuild()` = 基线 + 按序重放 patch。新增工序的 patch 若没记下"插在哪条后面"，
    重放时会静默落到第一个工作包，历史版本与当时的真实计划就对不上了。

    刻意不用 pytest 的 `tmp_path`：本机沙箱会拒绝 `%TEMP%` 下的 pytest 临时目录，
    这种测试一律用仓库内的普通目录（与 test_revise_api.py 同样的做法）。
    """
    import os
    import shutil

    from pipeline.plan_store import PlanStore, apply_patch

    # 目录名带 pid：与 `test_revise_api.py` 用同样的做法，两个 pytest 进程同时跑
    # 也不会互相删对方的档案目录。
    root = Path(BACKEND) / "_test_tmp" / ("p3_rebuild_p%d" % os.getpid())
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        store = PlanStore(root=str(root))
        base = _plan()
        store.save_baseline("p3chain", base)

        # 第 1 轮：在 5.1.1.1 后面新增一道工序
        p1, _c, rec1 = apply_patch(base, {"target": "5.1.1.1", "field": "add_task",
                                          "value": {"name": "地下室防水"}})
        assert rec1.get("applied")
        store.append_revision("p3chain", rec1, "在 5.1.1.1 后面增加一个工序：地下室防水",
                              ["5.1.1.1"], "新增")
        # 第 2 轮：再改一条工期（让链条不止一步）
        p2, _c2, rec2 = apply_patch(p1, {"target": "5.1.1.3", "field": "duration",
                                         "value": 8})
        assert rec2.get("applied")
        store.append_revision("p3chain", rec2, "把 5.1.1.3 的工期改成 8", ["5.1.1.3"], "改工期")

        # 重放到第 1 轮 → 必须等于当时的 p1
        got1 = store.rebuild("p3chain", upto=1)
        assert got1 is not None
        assert [str(l.get("id")) for l in iter_leaves(got1)] == \
               [str(l.get("id")) for l in iter_leaves(p1)], "重建出来的任务清单和当时不一致"
        # 重放全部 → 必须等于 p2
        got2 = store.rebuild("p3chain")
        newid = str(rec1.get("target"))
        assert newid in [str(l.get("id")) for l in iter_leaves(got2)]
        subs = got2["wbs"]["phases"][0]["work_packages"][0]["sub_packages"]
        order = [str(s.get("id")) for s in subs]
        assert order.index(newid) == order.index("5.1.1.1") + 1, \
            "重建后新增任务没有紧跟目标（说明 patch 重放丢了插入位置）：%s" % order
        durations2 = {str(l.get("id")): l.get("duration_days") for l in iter_leaves(got2)}
        assert durations2["5.1.1.3"] == 8, durations2
    finally:
        shutil.rmtree(root, ignore_errors=True)

