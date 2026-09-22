"""必含工程类型判据：三态区分 + "没校验成"不许被读成"不缺"。

真实背景（用户实测提问）：每次生成的评审门上都弹同一条 HIGH「缺失的必含工程类型」，
用户问"是不是知识库的缺陷"。实测定性（`plan_run_1789818211`，338 节点）：

  · 天棚工程 / 退场与恢复 = 树里一条相关工序都没有 → **生成器真漏**，KB 没错；
  · 防水工程             = 树里唯一带"防水"的叶子是「防水隐蔽验收」（挂 hidden_accept）
                           → **只有验收、没有施工工序**；验收节点不能当"分项存在"的证据；
  · 材料运输与加工工程    = KB 标 REQUIRED，名下 116 个 L4 全是「XX运输」、给不出可注入
                           工序 → **KB 侧真缺陷**，已降级为"可选"（A1 三档化后枚举名
                           `OPTIONAL`，历史名 `USUAL`；见
                           devtools/migrate_material_transport_to_usual.py）。

另外实测一个静默坑：`building_type=None` 的那份计划读数恒为"缺 0 类"，与"真的不缺"
在界面上完全无法区分 —— 所以校验结果必须带 `checked`/`reason`。
"""

import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline.nodes.wbs_agent import (                                    # noqa: E402
    REPAIR_FILL_TYPES, WBSAgentNode, kb_essentials_report, missing_kb_essentials)
from pipeline.nodes.wbs_phases import default_phases                      # noqa: E402

RESIDENTIAL = {"building_type": "residential"}
KB_DB = BACKEND.parent / "BuildPlan_KB" / "kb.db"


def _wbs(*leaf_names, phase="装饰装修"):
    """造一棵最小树：叶子名按顺序给（挂不挂 kb_activity_id 由具体用例自己设）。"""
    leaves = [{"id": "%s.%d" % (phase, i), "name": nm}
              for i, nm in enumerate(leaf_names, 1)]
    return {"phases": [{"phase": phase, "work_packages": [
        {"id": phase, "name": phase, "sub_packages": leaves}]}]}


# ---------------------------------------------------------------- 没校验成 ≠ 不缺
def test_取不到建筑类型时明说没校验成():
    rep = kb_essentials_report(_wbs("内墙抹灰"), {})
    assert rep["checked"] is False
    assert rep["missing"] == []
    assert "建筑类型" in rep["reason"], rep


def test_认不出的建筑类型也算没校验成():
    rep = kb_essentials_report(_wbs("内墙抹灰"), {"building_type": "古今中外混合结构"})
    assert rep["checked"] is False
    assert rep["missing"] == []
    assert rep["reason"]


def test_校验没跑成时门上要说出来并且不给补齐选项():
    node = WBSAgentNode()
    node.specs = list(default_phases())
    opts, notes, _ = node._repair_options({"wbs": _wbs("内墙抹灰")}, {}, [])
    assert REPAIR_FILL_TYPES not in [o["key"] for o in opts], opts
    assert any("没能校验" in n and "不等于真的不缺" in n for n in notes), notes


def test_校验没跑成会写进告警通道与_ctx():
    node = WBSAgentNode()
    ctx = {"wbs": _wbs("内墙抹灰")}
    rep = node._essentials(ctx, ctx["wbs"], {})
    assert rep["checked"] is False
    assert ctx["kb_essentials"]["checked"] is False
    assert any("未能校验" in w for w in ctx["scope_violations"]), ctx["scope_violations"]


def test_重做回显里_0类_与没校验成分开说():
    line = WBSAgentNode._stat_line(
        {"leaves": 1, "conc_m3": 0, "missing": 0, "missing_checked": False,
         "missing_reason": "项目参数里没有建筑类型（building_type）"},
        {"leaves": 2, "conc_m3": 0, "missing": 0, "missing_checked": True})
    assert "没能校验" in line and "0 类不代表不缺" in line, line


# ------------------------------------------------------- 三态：真缺 / 只有验收 / 没挂编号
def test_真缺_树里没有任何相关工序():
    rep = kb_essentials_report(_wbs("内墙抹灰"), RESIDENTIAL)
    kinds = {m["l3"]: m["kind"] for m in rep["missing"]}
    assert kinds.get("ceiling") == "missing", kinds
    assert kinds.get("demobilization") == "missing", kinds
    # 候选工序要带上，用户才知道该补什么
    ceil = [m for m in rep["missing"] if m["l3"] == "ceiling"][0]
    assert [a["id"] for a in ceil["activities"]][:1] == ["CEIL_AI_001"], ceil["activities"]


def test_只挂验收节点不算工序():
    rep = kb_essentials_report(_wbs("防水隐蔽验收"), RESIDENTIAL)
    m = [x for x in rep["missing"] if x["l3"] == "waterproofing"]
    assert m, rep["missing"]
    assert m[0]["kind"] == "inspection_only", m[0]
    assert m[0]["evidence"] == ["防水隐蔽验收"], m[0]
    assert "验收" in m[0]["kind_label"], m[0]


def test_有施工工序但没挂编号算锚定缺失():
    rep = kb_essentials_report(_wbs("卷材防水"), RESIDENTIAL)
    m = [x for x in rep["missing"] if x["l3"] == "waterproofing"]
    assert m and m[0]["kind"] == "unanchored", m
    assert m[0]["evidence"] == ["卷材防水"], m[0]


def test_挂了编号的类型就不算缺():
    wbs = _wbs("卷材防水")
    wbs["phases"][0]["work_packages"][0]["sub_packages"][0]["kb_activity_id"] = "WP_NEW_ROLL"
    rep = kb_essentials_report(wbs, RESIDENTIAL)
    assert "waterproofing" not in {m["l3"] for m in rep["missing"]}, rep["missing"]
    assert rep["anchored"] >= 1


def test_重试要求把验收节点不算施工说死():
    node = WBSAgentNode()
    node.specs = list(default_phases())
    miss = [{"l3": "waterproofing", "name": "防水工程", "phase": "地下室结构", "keys": [],
             "kind": "inspection_only", "kind_label": "只挂了验收/检测类节点，没有施工工序",
             "evidence": ["防水隐蔽验收"], "activities": []}]
    _targets, reqs = node._repair_targets({"wbs": _wbs("防水隐蔽验收")}, RESIDENTIAL, [],
                                          REPAIR_FILL_TYPES, miss)
    req = reqs.get("地下室结构") or ""
    assert "防水隐蔽验收" in req and "验收节点不算施工" in req, req


# ------------------------------------------------------------------- KB 侧不变量
def test_kb里没有标了必含却给不出工序的空壳类型():
    """material_transport 名下 116 个 L4 全是「XX运输」→ 不许再标 REQUIRED。

    它标 REQUIRED 时，`missing_kb_essentials()` 每次都对不上账：修复选项每次都弹，
    交回「施工准备」重做又补不出东西 —— 用户看到的是永远修不好的 HIGH。
    """
    if not KB_DB.exists():
        pytest.skip("知识库不存在：%s" % KB_DB)
    con = sqlite3.connect(str(KB_DB))
    try:
        bad = con.execute(
            "SELECT COUNT(*) FROM Building_Type_L3_Mapping WHERE work_type_id='material_transport' "
            "AND applicability_level='REQUIRED'").fetchone()[0]
        assert bad == 0, "material_transport 仍有 %d 行标 REQUIRED（应全为 OPTIONAL）" % bad
        # A1 三档化（2026-09-21 用户裁定 ②）：枚举名统一为 OPTIONAL，USUAL 不再写入。
        optional = con.execute(
            "SELECT COUNT(*) FROM Building_Type_L3_Mapping WHERE work_type_id='material_transport' "
            "AND applicability_level='OPTIONAL'").fetchone()[0]
        assert optional >= 10, "迁移后应为 OPTIONAL 的行数是 %d" % optional
        legacy = con.execute(
            "SELECT COUNT(*) FROM Building_Type_L3_Mapping WHERE work_type_id='material_transport' "
            "AND applicability_level='USUAL'").fetchone()[0]
        assert legacy == 0, "A1 之后不许再有 USUAL 行，实测 %d 行" % legacy
        # 迁移必须在 notes 里留痕，否则以后没人知道为什么降级
        noted = con.execute(
            "SELECT COUNT(*) FROM Building_Type_L3_Mapping WHERE work_type_id='material_transport' "
            "AND notes LIKE '%迁移%'").fetchone()[0]
        assert noted == optional, "只有 %d/%d 行写了迁移留痕" % (noted, optional)
    finally:
        con.close()


def test_兼容入口仍返回缺失清单():
    """老调用点用 `missing_kb_essentials()`，返回值形状不能变（list[dict]）。"""
    miss = missing_kb_essentials(_wbs("内墙抹灰"), RESIDENTIAL)
    assert isinstance(miss, list) and miss
    assert {"l3", "name", "phase", "keys"} <= set(miss[0])
