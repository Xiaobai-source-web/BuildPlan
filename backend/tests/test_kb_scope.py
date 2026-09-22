"""kb_scope 节点测试 — 知识库范围装配（纯查库，无 LLM）。

运行：cd backend && python -m pytest tests/test_kb_scope.py -q

覆盖核心设计约束：
  ① 结构映射只过滤 L4，**不**整工种降级；
  ② 结构映射缺数据（mapping_absent）时保留全量 L4；
  ③ 未识别结构形式 / 未识别建筑类型时降级不报错；
  ④ 每个 L4 都带工人种类，且 stats 与实际长度一致。
依赖 BuildPlan_KB/kb.db（仓库随附）。
"""

import sys
from functools import lru_cache
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline import kb
from pipeline.nodes.kb_scope import (KBScopeNode, merge_warnings,  # noqa: F401
                                     _WARN_MAPPING_ABSENT, _WARN_STRUCTURE)


# ==================== 测试夹具 / 小工具 ====================
class _Recorder(KBScopeNode):
    """把 emit 的事件收集起来，便于断言进度事件。"""

    def __init__(self):
        KBScopeNode.__init__(self)
        self.events = []
        self._emit = lambda ev, data: self.events.append((ev, data))


def _run(building=None, structure=None):
    """跑一次节点，返回 (node, result)。building/structure 为 None 时不放进 ctx。"""
    params = {}
    if building is not None:
        params["building_type"] = building
    if structure is not None:
        params["structure_type"] = structure
    node = _Recorder()
    result = node.run({"extracted_params": params})
    return node, result


@lru_cache(maxsize=None)
def _scope_cached(building, structure):
    """(building, structure) → kb_scope；同一组合只查库一次（KB 为只读静态数据）。"""
    return _run(building, structure)[1]["kb_scope"]


def _scope(building=None, structure=None):
    return _scope_cached(building, structure)


# 住宅 × 剪力墙 的"缺映射"警告条数 = **知识库现算**，不写死。
# 为什么：这个数就是「Structure_Type_L4_Mapping 里没有数据的 L3 个数」，是数据事实；
# 断言的意义是"同类警告要归并成一句且不丢"，与具体条数无关。写死会让"补一行表数据"
# 变成"改一批测试"（本次给桩基工程补映射就正好触发了这件事）。
_MISS_WARN = "结构映射表中没有该工种的映射数据，已保留其全部 L4（无结构约束）。"
_N_MAPPING_ABSENT = len([w for w in _scope_cached("住宅", "剪力墙结构")["warnings"]
                         if w.endswith(_MISS_WARN)])
_MERGED_ALL = "警告 %d 条（%d 条均为「结构映射表缺该工种数据 → 保留全部 L4」）" % (
    _N_MAPPING_ABSENT, _N_MAPPING_ABSENT)


def _ids(items):
    return [x["activity_id"] for x in items]


def _l3_ids(items):
    return [x["work_type_id"] for x in items]


# ==================== 1. 住宅 + 剪力墙：结构映射过滤 L4 ====================
def test_residential_shear_wall_concrete_filtered():
    """剪力墙下柱/屋架浇筑应被剔除，墙浇筑应保留。"""
    scope = _scope("residential", "shear_wall")

    assert scope["building_type"] == "residential"
    assert scope["building_type_name"] == "住宅"
    assert scope["structure_type"] == "shear_wall"
    assert scope["structure_type_name"]

    conc = scope["l4_candidates"]["concrete"]
    ids = _ids(conc)
    assert "CONC_NEW_COLUMN" not in ids, "剪力墙不应保留柱浇筑"
    assert "CONC_NEW_ROOF" not in ids, "剪力墙不应保留屋架浇筑"
    assert "CONC_NEW_WALL" in ids, "剪力墙必须保留墙浇筑"
    assert "CONC_NEW_FOUND" in ids
    # 结构映射存在且做了过滤 → 不带 mapping_absent 标记，且带适用等级
    assert all(x["structure_mapping_absent"] is False for x in conc)
    wall = [x for x in conc if x["activity_id"] == "CONC_NEW_WALL"][0]
    assert wall["applicability_level"] == "REQUIRED"
    assert wall["unit"] == "m³"
    assert wall["production_mode"]


def test_residential_shear_wall_l3_selection():
    """REQUIRED/OPTIONAL 进 l3_list（历史 USUAL 归并进 OPTIONAL），EXCLUDED 进 excluded_l3。"""
    scope = _scope("residential", "shear_wall")
    selected = _l3_ids(scope["l3_list"])
    excluded = _l3_ids(scope["excluded_l3"])

    assert "concrete" in selected
    assert "rebar" in selected
    assert "steel_structure" not in selected
    assert "steel_structure" in excluded
    # 被排除的 L3 不应出现在 L4 候选里
    assert "steel_structure" not in scope["l4_candidates"]
    # 等级只允许三种取值（A1 三档化：REQUIRED / OPTIONAL / EXCLUDED；
    # l3_list 里只会出现前两种或 UNKNOWN，历史 USUAL 已归并进 OPTIONAL）
    assert set(x["level"] for x in scope["l3_list"]) <= {"REQUIRED", "OPTIONAL", "UNKNOWN"}
    assert not any(x["level"] == "USUAL" for x in scope["l3_list"]), \
        "历史档位 USUAL 必须归并进 OPTIONAL（见 kb_scope.normalize_level）"
    # 每条 L3 都必须有 note 字段（原表 notes 可能为空串）
    assert all("note" in x for x in scope["l3_list"])
    # excluded_l3 必须带原因
    assert all(x["reason"] for x in scope["excluded_l3"])
    assert scope["stats"]["l3_total"] == 31


# ==================== 2. 钢结构工程在剪力墙下 L4 全被剔除 ====================
def test_steel_structure_excluded_l4_empty():
    """钢结构工程在剪力墙下全部 L4 被剔为 EXCLUDED，候选为空。

    注意：住宅的 steel_structure 在建筑类型层就是 EXCLUDED（已被 excluded_l3 收走），
    所以这里用"工业厂房"（该 L3 为 REQUIRED）来验证结构层过滤：
    L3 本身仍保留（本节点不做"整工种降级"），只是 L4 被清空。
    """
    scope = _scope("industrial", "shear_wall")
    assert scope["l4_candidates"]["steel_structure"] == []
    sel = [x for x in scope["l3_list"] if x["work_type_id"] == "steel_structure"]
    assert sel, "该 L3 只是 L4 被清空，不应被降级剔除"
    assert scope["stats"]["l4_excluded_by_structure"] >= len(kb.l4_for("steel_structure"))
    # 同一结构形式下，钢结构工程的 L4 在 kb 层也确实全被剔除
    assert kb.structure_l4_filtered("steel_structure", "shear_wall") == ([], False)


def test_steel_structure_not_downgraded_whole_l3():
    """护栏：L4 全被剔除也不得把该 L3 降级为 EXCLUDED（已废弃的坏规则）。"""
    scope = _scope("industrial", "shear_wall")
    assert "steel_structure" in _l3_ids(scope["l3_list"])
    assert "steel_structure" not in _l3_ids(scope["excluded_l3"])
    # 住宅下该 L3 被排除来自建筑类型映射，与结构无关
    res = _scope("residential", "shear_wall")
    assert "steel_structure" not in res["l4_candidates"]


# ==================== 3. 无结构形式：不做结构过滤 ====================
def test_no_structure_keeps_all_l4():
    """结构形式缺失 → 不做结构过滤，L4 数量等于该 L3 全量，并给出 warning。"""
    scope = _scope("residential")

    assert scope["structure_type"] == ""
    assert scope["warnings"], "无结构形式必须给出 warning"
    assert any("结构" in w for w in scope["warnings"])

    for wt in ("concrete", "rebar"):
        cand = scope["l4_candidates"][wt]
        assert len(cand) == len(kb.l4_for(wt))
        assert _ids(cand) == _ids(kb.l4_for(wt))
        # 无结构过滤 → 无适用等级，且全部标 mapping_absent
        assert all(x["applicability_level"] is None for x in cand)
        assert all(x["structure_mapping_absent"] is True for x in cand)

    # 未做结构过滤 → 结构剔除计数为 0
    assert scope["stats"]["l4_excluded_by_structure"] == 0
    # 建筑类型仍正常筛选
    assert "steel_structure" in _l3_ids(scope["excluded_l3"])


def test_recognized_structure_id_also_accepted():
    """结构形式传 KB ID（shear_wall）与中文名（剪力墙）结果一致。"""
    a = _scope("residential", "shear_wall")
    b = _scope("住宅", "剪力墙")
    assert a["structure_type"] == b["structure_type"] == "shear_wall"
    assert a["building_type"] == b["building_type"] == "residential"
    assert _ids(a["l4_candidates"]["concrete"]) == _ids(b["l4_candidates"]["concrete"])


# ==================== 4. 建筑类型识别不到：降级到全部 31 个 L3 ====================
def test_unknown_building_type_falls_back_to_all_l3():
    """识别不到建筑类型不报错，l3_list 覆盖全部 L3 且标 UNKNOWN。"""
    node, result = _run("一个完全不存在的项目类型", "shear_wall")
    scope = result["kb_scope"]

    assert scope["building_type"] == ""
    assert len(scope["l3_list"]) == 31
    assert scope["stats"]["l3_total"] == 31
    assert scope["stats"]["l3_selected"] == 31
    assert all(x["level"] == "UNKNOWN" for x in scope["l3_list"])
    assert scope["excluded_l3"] == []
    assert any("建筑类型" in w for w in scope["warnings"])
    # L4 仍是全量（建筑类型未知时不做结构过滤）
    assert len(scope["l4_candidates"]["concrete"]) == len(kb.l4_for("concrete"))
    assert node.done_summary
    assert "装配完成" in node.done_summary


def test_unknown_building_and_structure_never_raises():
    """建筑类型 + 结构形式都识别不到：仍返回完整结构，不抛异常。"""
    node, result = _run("未知类型", "未知结构")
    scope = result["kb_scope"]
    assert len(scope["l3_list"]) == 31
    assert scope["structure_type"] == ""
    assert len(scope["warnings"]) >= 2
    assert scope["l4_candidates"]["concrete"]
    assert node.done_summary


def test_missing_params_never_raises():
    """extracted_params 缺失 / 为空时也不能抛异常。"""
    node = _Recorder()
    out = node.run({})
    scope = out["kb_scope"]
    assert len(scope["l3_list"]) == 31
    assert scope["warnings"]
    # emit 的进度事件必须带 node / progress / message
    progress = [d for ev, d in node.events if ev == "node_progress"]
    assert progress
    assert all(d["node"] == "kb_scope" for d in progress)
    assert all(0 <= d["progress"] <= 100 for d in progress)
    assert progress[-1]["progress"] == 100


# ==================== 5. 工人种类 ====================
def test_every_l4_has_labor_type_field():
    """每个 L4 都必须带 labor_type 字段（可能为空串 = 管理/验收类）。"""
    scope = _scope("residential", "shear_wall")
    total = 0
    for wt, cand in scope["l4_candidates"].items():
        assert cand is not None
        for x in cand:
            total += 1
            assert "labor_type" in x, "{} / {} 缺 labor_type".format(wt, x.get("activity_id"))
            assert isinstance(x["labor_type"], str)
    assert total > 0
    assert total == scope["stats"]["l4_selected"]


def test_rebar_l4_labor_type_is_rebar_worker():
    """rebar 下的 L4 工人种类必须是钢筋工。"""
    scope = _scope("residential", "shear_wall")
    rebar = scope["l4_candidates"]["rebar"]
    assert rebar, "剪力墙下钢筋工程应有 L4"
    assert all(x["labor_type"] == "钢筋工" for x in rebar)


def test_concrete_l4_labor_type_is_concrete_worker():
    scope = _scope("residential", "shear_wall")
    assert all(x["labor_type"] == "混凝土工"
               for x in scope["l4_candidates"]["concrete"])


# ==================== 6. stats 自洽 ====================
def test_stats_consistent():
    scope = _scope("residential", "shear_wall")
    stats = scope["stats"]

    assert stats["l3_total"] == 31
    assert stats["l3_selected"] == len(scope["l3_list"])
    assert stats["l4_selected"] == sum(len(v) for v in scope["l4_candidates"].values())
    assert stats["l4_excluded_by_structure"] >= 0
    # 选定的 L3 与 l4_candidates 的键一一对应
    assert set(_l3_ids(scope["l3_list"])) == set(scope["l4_candidates"].keys())
    # l3_total = 选定 + 排除
    assert stats["l3_total"] == stats["l3_selected"] + len(scope["excluded_l3"])


def test_stats_consistent_when_no_structure():
    scope = _scope("residential")
    stats = scope["stats"]
    assert stats["l3_selected"] == len(scope["l3_list"])
    assert stats["l4_selected"] == sum(len(v) for v in scope["l4_candidates"].values())
    assert set(_l3_ids(scope["l3_list"])) == set(scope["l4_candidates"].keys())


# ==================== 7. 结构映射缺数据的 L3：保留全量、标 mapping_absent ====================
def test_l3_without_structure_mapping_keeps_all_l4():
    """映射表只覆盖 4 个 L3，其余工种必须保留全量 L4 并标 mapping_absent。"""
    scope = _scope("residential", "shear_wall")
    wt = "scaffolding"  # 映射表未覆盖
    cand = scope["l4_candidates"][wt]
    assert len(cand) == len(kb.l4_for(wt))
    assert all(x["structure_mapping_absent"] is True for x in cand)
    assert any(wt in w or "脚手架" in w for w in scope["warnings"])


def test_structure_only_filters_l4_never_drops_l3():
    """护栏：结构形式 + 建筑类型不同，选定 L3 集合应保持稳定（不整工种降级）。"""
    a = _scope("residential", "shear_wall")
    b = _scope("residential", "frame")
    assert _l3_ids(a["l3_list"]) == _l3_ids(b["l3_list"])
    assert a["stats"]["l3_selected"] == b["stats"]["l3_selected"]


# ==================== 8. 警告不许"算完就丢"：进 ctx + 归并回显（第 23 轮） ====================
def test_警告真的进了ctx且摘要归并后回显():
    """真实缺陷：warnings 只被数字化成"警告 N 条"，内容算完就丢。

    现在：① ctx["kb_warnings"] 拿得到逐条原文；
         ② done_summary 只报**归并后**的一句（同类不逐条重复）；
         ③ "…其余 N 条同类"由节点算好、随 node_done 上行给终端。
    """
    node, result = _run("住宅", "剪力墙结构")
    n = _N_MAPPING_ABSENT
    assert n > 0, "前置条件：住宅×剪力墙下确实有未覆盖结构映射的工种"
    assert result["kb_warnings"] == result["kb_scope"]["warnings"]
    assert len(result["kb_warnings"]) == n
    assert all(_WARN_MAPPING_ABSENT in w for w in result["kb_warnings"])

    summary = node.done_summary
    assert _MERGED_ALL in summary
    assert len(summary.splitlines()) == 1, summary
    assert _WARN_MAPPING_ABSENT not in summary, "同类警告不许逐条重复"
    assert node.warning_note.startswith(
        "…其余 %d 条同类（结构映射表缺该工种数据 → 保留全部 L4）" % max(0, n - 3))


def test_同类警告归并成1类加计数():
    """归并函数本身：N 条同类 → 1 类 + 计数（不是 N 行）。"""
    same = ["%d 号工种（w%d）：%s" % (i, i, _WARN_MAPPING_ABSENT)
            for i in range(_N_MAPPING_ABSENT)]
    digest = merge_warnings(same)
    n = _N_MAPPING_ABSENT
    assert digest["total"] == n
    assert len(digest["groups"]) == 1 and digest["groups"][0]["count"] == n
    assert digest["summary"] == _MERGED_ALL
    assert len(digest["samples"]) == 3 and len(digest["detail_lines"]) == 4
    assert digest["note"].startswith("…其余 %d 条同类" % max(0, n - 3))


def test_两三类警告时列出前几类加计数():
    warns = [_WARN_STRUCTURE] + ["%d 号工种（w%d）：%s" % (i, i, _WARN_MAPPING_ABSENT)
                                 for i in range(5)]
    digest = merge_warnings(warns)
    assert digest["total"] == 6 and len(digest["groups"]) == 2
    assert "1 条「未识别结构形式 → 未做结构过滤」" in digest["summary"]
    assert "5 条「结构映射表缺该工种数据 → 保留全部 L4」" in digest["summary"]


def test_缺结构形式时警告也归并回显():
    """单类警告：摘要说"1 条均为…"，且不留"其余 N 条"这种空话。"""
    node, result = _run("住宅")
    assert result["kb_warnings"] == [_WARN_STRUCTURE]
    assert "警告 1 条（1 条均为「未识别结构形式 → 未做结构过滤」）" in node.done_summary
    assert node.warning_note == ""


def test_无警告时摘要不许凭空加一句():
    from pipeline.nodes.kb_scope import merge_warnings as _merge
    digest = _merge([])
    assert digest["total"] == 0 and digest["summary"] == "" and digest["note"] == ""
    assert digest["groups"] == [] and digest["detail_lines"] == []


def test_scope里的warnings语义没变():
    """护栏：归并只加回显，不许动 warnings 本身的既有语义。"""
    scope = _scope("residential", "shear_wall")
    # 住宅×剪力墙下，警告恰好等于"缺结构映射的工种"那一些（每个未覆盖 L3 一条）
    assert len(scope["warnings"]) == _N_MAPPING_ABSENT
    assert all(w.endswith(_MISS_WARN) for w in scope["warnings"])
    assert all(isinstance(w, str) and w for w in scope["warnings"])
    no_struct = _scope("residential")
    assert any("结构" in w for w in no_struct["warnings"])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
