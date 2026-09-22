# -*- coding: utf-8 -*-
"""WS6 施工组织层 → 资源层：**本工种的班组以组织层为唯一真源**。

对应用户/父代理实测的缺陷（`resource.py` 侧）：
  · `5.1.1.1` 钢筋绑扎 = 2 个作业面 × 19 人 = **38 人**（组织层），资源行却按
    `Workface_Capacity_Rule.max_labor`（口径是**每施工段**）压成 **8 人/天**，
    并自述"仍缺 168 工日" —— 2 个面同时干，工日一点没缺，这句是假话；
  · 全量 156 条带 `_organization` 的任务里 106 条资源行与组织层对不上。

**第 45 轮口径变更（用户裁定，依据 `devtools/_dev-notes/终版修改_接口冻结.md` §6）**：
"每面人数上限"由两套（资源层按段公式 ∩ 组织层工种曲线）改为**单源** ——
**认大的那版 = 组织层工种曲线**（内墙抹灰每面 30 人）。因此：
  · 有组织层结果 → `_organization_crew.cap_per_face == org.crew_per_face`、
    `cap_total == crew_total`、`resource_cap_below_org is False`（资源层不再产出
    第二个更小的上限，`_workface_note` 只写一套一致说明）；
  · **无组织层结果** → 才退回工作面容量兜底。⚠️ 第 6 批（域 1.6）已删
    `Workface_Capacity_Rule`：兜底不再回查 KB，只认**叶子自带**的
    `workface_capacity`（`cap3` / `nocap` 桩用例即打在这个函数上）；
    原先"退回 `Workface_Capacity_Rule` 按段公式"的那条真库用例已随之删除。
  本节测试原先写死了"两套上限都要留档"（旧设计），随单源裁定一并改写。

本文件钉住四件事：
  ① 有 `_organization.crew_total` → 本工种 `per_day == crew_total`（不再套单面上限）；
  ② 留痕：`_resource_source[工种].origin == "org_layer"` + `_organization_crew` **单源**上限；
  ③ 无 `_organization` 的任务**逐位不变**（旧路径与封顶行为原样保留）；
  ④ 机械与机械配员路径一律不动。

运行：python -m pytest backend/tests/test_org_res_crew.py -v
"""

import copy
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline.nodes import resource as R          # noqa: E402


def _binding(crew=None):
    return {"mode": "labor", "norm_value": 4.43, "productivity_value": 0.225734,
            "unit": "工日/t", "source_code": "LD_T72_7_2008", "match_type": "kb",
            "usable": True, "norm_is_evidence": True,
            "crew": dict(crew or {"钢筋工": 7}),
            "crew_kind": {"钢筋工": "labor"}, "labor_types": ["钢筋工"],
            "provenance": {"origin": "kb", "ref": "LD_T72_7_2008"}}


def _leaf(qty=200.0, days=7, max_labor=3):
    return {"id": "5.1.1.1", "name": "1-1层 钢筋绑扎", "quantity": qty, "unit": "t",
            "duration_days": days,
            "workface_capacity": {"max_labor": max_labor, "max_machine": None,
                                  "unit_basis": "每施工段",
                                  "source_type": "ai_estimate", "confidence": "LOW"},
            "norm_binding": _binding()}


ORG = {"cadence_days": 7.0, "n_faces": 2, "crew_per_face": 19, "crew_total": 38,
       "source": "cadence", "duration_days": 7, "c_max": 20}


@pytest.fixture
def cap3(monkeypatch):
    """把"单面上限"钉成 3 人，与 KB 标定行无关（测试要与库解耦）。"""
    monkeypatch.setattr(R, "_workface_caps",
                        lambda wf, **kw: (3, None, "（测试桩：单面 3 人）"))


@pytest.fixture
def nocap(monkeypatch):
    monkeypatch.setattr(R, "_workface_caps", lambda wf, **kw: (None, None, ""))


# ---------------- ① 组织层接管班组 ----------------

def test_组织层按跨作业面总班组定人数(cap3):
    leaf = _leaf()
    d = R.compute_norm_resources(leaf, leaf["norm_binding"], 200.0, 7, org=dict(ORG))
    assert d["钢筋工_per_day"] == 38, "本工种班组 = 组织层 crew_total（2 面 × 19 人）"
    assert d["_crew"]["钢筋工"] == 38
    assert not d.get("_workface_capped"), "组织层已校验每面人数，资源层不该再封顶"
    assert "仍缺" not in json.dumps(d.get("_workface_capped") or [], ensure_ascii=False)
    assert "仍缺" not in (d.get("_workface_note") or "")


def test_组织层留痕写清来源与单源上限(cap3):
    """§6 单源：每面人数上限只认组织层工种曲线（用户裁定：认大的那版）。

    旧断言写死"两套上限都要留档"（`cap_per_face == 3 and cap_total == 6`、
    `resource_cap_below_org is True`），那是**旧设计**。依据
    `devtools/_dev-notes/终版修改_接口冻结.md` §6 与用户裁定，改为单源：
    资源层不再对同一工序产出第二个更小的上限（抹灰 20 vs 组织层 30 打架就是这么来的）。
    """
    leaf = _leaf()
    d = R.compute_norm_resources(leaf, leaf["norm_binding"], 200.0, 7, org=dict(ORG))
    src = d["_resource_source"]["钢筋工"]
    assert src["origin"] == "org_layer", "来源必须是组织层，不是定额（否则班组来历就没了）"
    assert "施工组织层" in src["ref"] and "2 面" in src["ref"] and "19 人/面" in src["ref"]
    oc = d["_organization_crew"]
    assert oc["crew_total"] == 38 and oc["n_faces"] == 2 and oc["crew_per_face"] == 19
    # 单源：每面上限 = 组织层 crew_per_face（不是资源层公式算出的 3）
    assert oc["cap_per_face"] == ORG["crew_per_face"] == 19, \
        "§6 单源：cap_per_face 必须等于 org.crew_per_face（认大的那版）"
    assert oc["cap_total"] == 38, "总数口径也随单源，与 crew_total 一致"
    assert oc["resource_cap_below_org"] is False, \
        "资源层不再产出第二个更小的上限 → 这个键正常恒为 False（§6）"
    assert oc["capped"] is False, "没有真封顶，就不许写封顶"
    assert not d.get("_workface_capped"), "资源层不许再按单面口径压组织层的班组"
    # 一套一致说明（旧文案"以组织层为准、资源层不再封顶"自相矛盾，已删）
    note = d.get("_workface_note") or ""
    assert "组织层" in note and "为准" in note and "19 人/面" in note and "38 人" in note, note
    assert "不再封顶" not in note and "两套" not in note, \
        "单源后不许再出现「两套上限/资源层不再封顶」这种自相矛盾的文案：%s" % note


def test_工日需求按定额不变_只改班组(cap3):
    """规则 4：`total_days`（工日需求）保持原样，只有 `per_day`（班组）改。"""
    leaf = _leaf()
    old = R.compute_norm_resources(leaf, leaf["norm_binding"], 200.0, 7)
    new = R.compute_norm_resources(leaf, leaf["norm_binding"], 200.0, 7, org=dict(ORG))
    assert new["钢筋工_total_days"] == old["钢筋工_total_days"], "工日需求不许被改动"
    assert new["钢筋工_per_day"] != old["钢筋工_per_day"] == 3, "旧口径的单面封顶仍在（无组织层时）"


# ---------------- ② 无组织层 → 逐位不变 ----------------

def test_无组织层与旧调用逐位一致(cap3):
    leaf = _leaf()
    a = R.compute_norm_resources(leaf, leaf["norm_binding"], 200.0, 7)
    b = R.compute_norm_resources(leaf, leaf["norm_binding"], 200.0, 7, org=None)
    c = R.compute_norm_resources(leaf, leaf["norm_binding"], 200.0, 7, org={})
    assert a == b == c, "`org` 缺省/为空必须与旧调用完全一致"
    assert a["钢筋工_per_day"] == 3
    rec = a["_workface_capped"][0]
    assert rec["unit_basis"] == "每施工段" and "仍缺" in rec["reason"], "旧封顶文案原样保留"


def test_组织层缺crew_total时退回旧口径(cap3):
    leaf = _leaf()
    d = R.compute_norm_resources(leaf, leaf["norm_binding"], 200.0, 7,
                                 org={"n_faces": 2, "source": "cadence"})
    assert d["钢筋工_per_day"] == 3, "拿不到班组数 → 逐位走旧路径"
    assert d.get("_workface_capped") and "_organization_crew" not in d
    assert d["_resource_source"]["钢筋工"]["origin"] != "org_layer"


# 迁移测试说明（第 6 批）：原 `test_无组织层时按Workface_Capacity_Rule兜底` 的
# 前提是 `R.kb.workface_capacity("REBAR_NEW_SLAB")` 能拿到标定行。域 1.6 已删
# `Workface_Capacity_Rule` 表与 `kb.workface_capacity()`，调用直接 `AttributeError`；
# "无组织层时退回 Workface_Capacity_Rule 按段公式"这条兜底能力经用户裁定退役
# （容量只认叶子自带键，本用例叶子 `workface_capacity=None` → 不封顶），
# 无法改写成对新行为的同义断言（新行为见 `test_已有按段上限...` 一类用例），
# **整条用例删除**。


def test_组织层cap_source跟随WS4标记():
    """§6：WS4 的 `crew_source` / `crew_source_ref` / `curve_ceiling` 有就原样带出。

    `crew_source` 是 WS4 定的"这个每面人数从哪来"（`user_limit` / `org_curve` /
    `crew_preferred` / `c_min`）；资源层只是**消费**它，不许自己另立一套判据。
    """
    leaf = _leaf()
    org = dict(ORG)
    org.update({"crew_source": "org_curve", "curve_ceiling": 30,
                "crew_source_ref": "曲线：max(crew_max=15, min(40, ceil(12×2.5)))"})
    d = R.compute_norm_resources(leaf, leaf["norm_binding"], 200.0, 7, org=org)
    oc = d["_organization_crew"]
    assert oc["cap_source"] == "org_curve"
    assert oc["curve_ceiling"] == 30, "WS4 的曲线单面上限原样带出（可对账）"
    assert "曲线" in oc["crew_source_ref"]
    note = d.get("_workface_note") or ""
    assert "org_curve" in note and "曲线" in note, note
    # WS4 还没写这几个键（老计划）时：不加键、不编值，cap_source 如实写 org_layer
    d2 = R.compute_norm_resources(leaf, leaf["norm_binding"], 200.0, 7, org=dict(ORG))
    assert d2["_organization_crew"]["cap_source"] == "org_layer"
    assert "curve_ceiling" not in d2["_organization_crew"]
    assert "crew_source_ref" not in d2["_organization_crew"]


def test_机械路径不受组织层影响(cap3):
    """规则 4：机械与机械配员一律不动（组织层只接管人工班组）。"""
    leaf = {"id": "3.2.1", "name": "基坑土方开挖", "quantity": 5000.0, "unit": "m³",
            "duration_days": 5, "kb_activity_id": None,
            "workface_capacity": {"max_labor": None, "max_machine": None},
            "norm_binding": {"mode": "machine", "norm_value": 0.53,
                             "unit": "台班/100m³", "source_code": "GD_2018_A1_1",
                             "match_type": "kb", "usable": True, "norm_is_evidence": True,
                             "machine_name": "履带式单斗液压挖掘机",
                             "crew": {"司机": 1}, "crew_kind": {"司机": "machine"}}}
    b = leaf["norm_binding"]
    a = R.compute_norm_resources(leaf, b, 5000.0, 5)
    c = R.compute_norm_resources(leaf, b, 5000.0, 5, org=dict(ORG))
    assert a == c, "机械任务的资源行必须逐位相同"


# ---------------- ③ compute_flat：不削峰 + 参数透传恒等 ----------------

def _wbs(leaf=None):
    return {"phases": [{"phase": "主体", "work_packages": [
        {"name": "钢筋", "sub_packages": [leaf or _leaf()]}]}]}


def _boundary(trade="钢筋工", limit=25):
    return {"labor": {"by_trade": [{"trade": trade, "quantity": limit, "unit": "人"}]}}


def test_组织层任务不再被申报工种上限二次削峰(nocap):
    leaf = _leaf()
    rd = R.compute_flat(_wbs(leaf), None, _boundary(), schedule_days={"5.1.1.1": 7},
                        schedule_org={"5.1.1.1": dict(ORG)})
    t = {x["task_id"]: x for x in rd["resource_demand"]["tasks"]}["5.1.1.1"]
    assert t["钢筋工_per_day"] == 38, "组织层是唯一真源：不许按总数再削一刀"
    assert t["_resource_source"]["钢筋工"]["origin"] == "org_layer"
    sk = t.get("_peak_shaving_skipped")
    assert sk, "跳过削峰必须留痕（绝不静默）"
    assert sk["declared_trade_limit"] == 25 and sk["crew_total"] == 38
    assert "组织层" in sk["reason"] and "每面" in sk["reason"]


def test_无组织层时削峰照旧(nocap):
    """同一条任务、同一份申报限额，没有组织层就还是老行为（削到 25）。"""
    leaf = _leaf()
    rd = R.compute_flat(_wbs(leaf), None, _boundary(), schedule_days={"5.1.1.1": 7})
    t = {x["task_id"]: x for x in rd["resource_demand"]["tasks"]}["5.1.1.1"]
    assert t["钢筋工_per_day"] == 25, "无组织层 → 削峰行为不变"
    assert t.get("_adjusted") and "_peak_shaving_skipped" not in t


def test_schedule_org缺省与空字典结果恒等(cap3):
    a = R.compute_flat(_wbs(), None, None, schedule_days={"5.1.1.1": 7})
    b = R.compute_flat(_wbs(), None, None, schedule_days={"5.1.1.1": 7}, schedule_org={})
    c = R.compute_flat(_wbs(), None, None, schedule_days={"5.1.1.1": 7}, schedule_org=None)
    ja = json.dumps(a["resource_demand"], sort_keys=True, ensure_ascii=False)
    jb = json.dumps(b["resource_demand"], sort_keys=True, ensure_ascii=False)
    jc = json.dumps(c["resource_demand"], sort_keys=True, ensure_ascii=False)
    assert ja == jb == jc, "新增参数缺省/空值不许改变任何一位输出"


def test_ResourceNode从排程行取organization():
    """`ResourceNode.run` 与 `schedule_days` **同一次**取 `_organization`。

    注意 `run` 返回的是 `to_nested_resources` 之后的形状（resources:{名:{per_day,total_days}}）。
    """
    from pipeline.nodes.resource import ResourceNode
    node = ResourceNode()
    node._emit = lambda e, d: None
    ctx = {"wbs": _wbs(), "extracted_params": None, "boundary_conditions": None,
           "schedule": {"schedule": [{"task_id": "5.1.1.1", "es": 0, "ef": 7,
                                      "_organization": dict(ORG)}]}}
    out = node.run(ctx)
    t = {x["task_id"]: x for x in out["resource_demand"]["tasks"]}["5.1.1.1"]
    assert t["planned_duration_days"] == 7
    assert t["resources"]["钢筋工"]["per_day"] == 38
    assert t["_organization_crew"]["crew_total"] == 38
    assert t["_resource_source"]["钢筋工"]["origin"] == "org_layer"
