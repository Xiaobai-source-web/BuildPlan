"""kb_conformance 测试 — 知识库范围一致性硬校验（纯代码，无 LLM）。

运行：cd backend && python -m pytest tests/test_kb_conformance.py -q

## 为什么要有这个模块（本文件守的就是这几条）

上游 `kb_scope` 算出的合法工序范围，以前**只喂给 WBS 生成的提示词**，没有任何代码
在事后核对。实测后果：节拍展开（beat_node → layer_engine）走的是 `beat_configs` 的
硬编码配置，**完全不看 kb_scope**，于是 `"构造柱浇筑"` 挂着的 `CONC_NEW_COLUMN`
（知识库里在剪力墙结构下是 EXCLUDED）在剪力墙住宅项目里照样进了计划，全程无告警。

本文件覆盖：
  ① 无范围时不误报（"上游没跑" ≠ "计划违规"）；
  ② 范围内 → 通过；四种违规各自归类正确；
  ③ 没挂 kb_activity_id 的叶子不算违规（临建/验收类本来就没有编号）；
  ④ 同（阶段，编号）合并计数、明细限流；
  ⑤ 真的抓得到 beat_configs 里那条硬编码的违规（端到端，附回归护栏）。

依赖 BuildPlan_KB/kb.db（仓库随附）。
"""

import sys
from functools import lru_cache
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline import kb  # noqa: E402
from pipeline.nodes import kb_conformance as kc  # noqa: E402
from pipeline.nodes.kb_scope import KBScopeNode  # noqa: E402


# ==================== 夹具 ====================
@lru_cache(maxsize=None)
def _scope(building="住宅", structure="剪力墙结构"):
    """跑一次 kb_scope（KB 只读，缓存住）。"""
    node = KBScopeNode()
    node._emit = lambda *a, **k: None
    return node.run({"extracted_params": {"building_type": building,
                                          "structure_type": structure}})["kb_scope"]


def _wbs(*leaves):
    """把 `(phase, wp, name, activity_id)` 逐条拼成一棵最小 WBS 树。"""
    phases = {}
    for phase, wp, name, aid in leaves:
        wps = phases.setdefault(phase, {})
        leaf = {"name": name}
        if aid is not None:
            leaf["kb_activity_id"] = aid
        wps.setdefault(wp, []).append(leaf)
    return {"phases": [
        {"phase": ph, "work_packages": [
            {"id": "", "name": wpn, "sub_packages": ls} for wpn, ls in wps.items()]}
        for ph, wps in phases.items()]}


def _ids(result):
    return [it["activity_id"] for it in result["issues"]]


def _kinds(result):
    return {it["activity_id"]: it["kind"] for it in result["issues"]}


# ==================== ① 没有范围 → 不核对、不误报 ====================
@pytest.mark.parametrize("scope", [None, {}, {"l4_candidates": {}}, {"l4_candidates": None},
                                   "不是字典", 0])
def test_no_scope_never_reports_violations(scope):
    """拿不到范围时 checked=False 且 0 违规 —— 绝不能拿"上游没跑"当成"计划违规"。"""
    wbs = _wbs(("主体", "结构", "柱浇筑", "CONC_NEW_COLUMN"),
               ("主体", "结构", "砌块墙", "LDT724_砌块墙"))
    r = kc.check_scope_conformance(wbs, scope)
    assert r["checked"] is False
    assert r["violations"] == 0
    assert r["issues"] == []
    assert "未做范围一致性核对" in r["summary"]
    # 明确区别于"核对通过"：一个说没做，一个说做了且没问题
    assert "通过" not in r["summary"]


# ==================== ② 范围内 → 通过 ====================
def test_activity_inside_scope_passes():
    """范围内 + 没挂编号的叶子都不算违规，摘要要明确说"通过"。"""
    wbs = _wbs(("地下室结构", "结构", "墙浇筑", "CONC_NEW_WALL"),
               ("地下室结构", "结构", "墙钢筋", "REBAR_NEW_WALL"),
               ("施工准备", "临建", "场地平整", None),          # 没编号 → 不参与判定
               ("施工准备", "临建", "临时道路", ""))            # 空串 → 同上
    r = kc.check_scope_conformance(wbs, _scope())
    assert r["checked"] is True
    assert r["violations"] == 0
    assert r["leaves"] == 4
    assert r["anchored"] == 2, "只有挂了非空编号的叶子才进核对"
    assert "通过" in r["summary"]
    assert r["note"] == ""


# ==================== ③ 四种违规各自归类 ====================
def test_banned_by_structure_mapping():
    """剪力墙下柱浇筑（被结构形式剔除）→ banned。"""
    wbs = _wbs(("二次结构与砌体", "砌体", "构造柱浇筑", "CONC_NEW_COLUMN"))
    r = kc.check_scope_conformance(wbs, _scope())
    assert _kinds(r) == {"CONC_NEW_COLUMN": kc.KIND_BANNED}
    it = r["issues"][0]
    assert it["phase"] == "二次结构与砌体"
    assert it["wp"] == "砌体"
    assert it["leaf_name"] == "构造柱浇筑"
    assert it["work_type_id"] == "concrete"
    assert "结构形式剔除" in it["reason"]


def test_banned_by_building_type_exclusion():
    """住宅下金属结构工程整类被排除 → banned（原因要指向建筑类型）。"""
    wbs = _wbs(("主体", "钢结构", "钢梁", "STEEL0009"))
    r = kc.check_scope_conformance(wbs, _scope())
    assert _kinds(r) == {"STEEL0009": kc.KIND_BANNED}
    assert "建筑类型" in r["issues"][0]["reason"]


def test_other_l3_when_work_type_not_in_scope():
    """工种压根不在本工程范围内 → other_l3（与"被结构剔除"分开，修法不同）。"""
    scope = _scope()
    # 造一个范围内没有的工种：拿工业厂房才有、住宅被排除之外的组合不好造，
    # 改用一份最小 scope（只有 concrete）来验证分类逻辑本身。
    tiny = {"l4_candidates": {"concrete": [{"activity_id": "CONC_NEW_WALL"}]},
            "excluded_l3": []}
    wbs = _wbs(("主体", "砌体", "砌块墙", "LDT724_砌块墙"))
    r = kc.check_scope_conformance(wbs, tiny)
    assert _kinds(r) == {"LDT724_砌块墙": kc.KIND_OTHER_L3}
    assert r["issues"][0]["work_type_id"] == "masonry"
    # 对照：真 scope 里砌筑工程是在范围内的（住宅 USUAL）
    assert "masonry" in scope["l4_candidates"]


def test_unknown_activity_id():
    """库里没有这个编号 → unknown（不是范围问题，修法是改编号/补库）。"""
    wbs = _wbs(("主体", "结构", "某种工序", "THIS_ID_DOES_NOT_EXIST"))
    r = kc.check_scope_conformance(wbs, _scope())
    assert _kinds(r) == {"THIS_ID_DOES_NOT_EXIST": kc.KIND_UNKNOWN}
    assert r["issues"][0]["work_type_id"] == ""
    assert "查不到" in r["issues"][0]["reason"]


def test_empty_l4_work_type_still_counts_as_in_scope():
    """某工种的 L4 被结构**全剔空**时，它的活动仍应判 banned 而不是 other_l3。

    这是本项目的一个真实坑：工业厂房 × 剪力墙下 steel_structure 的 64 条 L4 全被
    剔除，`l4_candidates["steel_structure"] == []`。若用"allowed 里的值"判断工种
    是否在范围，就会把"被结构剔空"误报成"工种不在范围内"，指向错误的修法。
    """
    ind = _scope("工业厂房", "剪力墙结构")
    assert ind["l4_candidates"]["steel_structure"] == [], "前置条件：该工种 L4 被全剔空"
    wbs = _wbs(("主体", "钢结构", "钢梁", "STEEL0009"))
    r = kc.check_scope_conformance(wbs, ind)
    assert _kinds(r) == {"STEEL0009": kc.KIND_BANNED}
    assert "结构形式剔除" in r["issues"][0]["reason"]


# ==================== ④ 合并计数与限流 ====================
def test_same_phase_and_id_merged():
    """同一相里同一编号重复出现 → 合成一条并计数（不刷屏）。"""
    wbs = _wbs(("二次结构与砌体", "砌体", "构造柱浇筑①", "CONC_NEW_COLUMN"),
               ("二次结构与砌体", "砌体", "构造柱浇筑②", "CONC_NEW_COLUMN"),
               ("二次结构与砌体", "砌体", "构造柱浇筑③", "CONC_NEW_COLUMN"))
    r = kc.check_scope_conformance(wbs, _scope())
    assert r["violations"] == 3, "违规条数按叶子算"
    assert len(r["issues"]) == 1, "明细按（阶段,编号）合并"
    assert r["issues"][0]["count"] == 3


def test_different_phases_not_merged():
    """不同阶段的同一条违规各报一条（修的时候要分别去改）。"""
    wbs = _wbs(("二次结构与砌体", "砌体", "构造柱浇筑", "CONC_NEW_COLUMN"),
               ("主体", "结构", "构造柱浇筑", "CONC_NEW_COLUMN"))
    r = kc.check_scope_conformance(wbs, _scope())
    assert len(r["issues"]) == 2
    assert {it["phase"] for it in r["issues"]} == {"二次结构与砌体", "主体"}


def test_detail_limit_keeps_counting():
    """明细限流：issues 被截断，但 violations 计数必须照实。"""
    leaves = [("相%d" % i, "包", "柱浇筑", "CONC_NEW_COLUMN") for i in range(10)]
    r = kc.check_scope_conformance(_wbs(*leaves), _scope(), limit=3)
    assert len(r["issues"]) == 3
    assert r["violations"] == 10
    assert "其余 7 处" in r["note"]


def test_by_kind_counts():
    """by_kind 分类计数要齐。"""
    wbs = _wbs(("A", "包", "构造柱浇筑", "CONC_NEW_COLUMN"),
               ("B", "包", "钢梁", "STEEL0009"),
               ("C", "包", "编的", "NOPE_123"))
    r = kc.check_scope_conformance(wbs, _scope())
    assert r["by_kind"] == {kc.KIND_BANNED: 2, kc.KIND_UNKNOWN: 1}


# ==================== ⑤ 门 issue / meta 留档 ====================
def test_gate_issues_skip_unknown_and_target_phase():
    """unknown 不进门的结构维度；target 必须是阶段名（一键修复靠它匹配）。"""
    wbs = _wbs(("二次结构与砌体", "砌体", "构造柱浇筑", "CONC_NEW_COLUMN"),
               ("主体", "结构", "编的", "NOPE_123"))
    r = kc.check_scope_conformance(wbs, _scope())
    issues = kc.gate_issues(r)
    assert len(issues) == 1, "unknown 不进结构维度"
    assert issues[0]["severity"] == "HIGH"
    assert issues[0]["target"] == "二次结构与砌体"
    assert "CONC_NEW_COLUMN" in issues[0]["finding"]


def test_metadata_is_json_safe():
    """meta 留档块要能直接进 plan_json（全是基本类型）。"""
    import json
    r = kc.check_scope_conformance(
        _wbs(("A", "包", "构造柱浇筑", "CONC_NEW_COLUMN")), _scope())
    meta = kc.metadata(r)
    assert json.loads(json.dumps(meta, ensure_ascii=False)) == meta
    assert meta["checked"] is True and meta["violations"] == 1
    assert meta["issues"] and meta["summary"]


# ==================== ⑥ 端到端：真的抓得到 beat_configs 那条硬编码违规 ====================
def test_beat_config_column_pour_is_banned_for_shear_wall():
    """回归护栏：剪力墙结构下 **柱浇筑（CONC_NEW_COLUMN）本就该被判违规**。

    这条是**真缺陷的锚点**，不是假想 —— 它曾经抓到过节拍配置的硬编码违规：
      · 旧 `beat_configs.BASE_BEAT_CONFIGS["二次结构与砌体"]["cycle"]` 里写着
        `kb_activity_id: "CONC_NEW_COLUMN"`；
      · 节拍展开不看 kb_scope，也不经过 WBS 提示词，所以它必然进树；
      · 在剪力墙结构下 CONC_NEW_COLUMN 是 EXCLUDED（KB 事实，下面同时断言）。

    **配置已改对（第 5 批）**：域 4.1b 的硬约束要求该分部叶子的 L4 所属 L3 必须 ∈
    `DEFAULT_PHASES["二次结构与砌体"]["kb"] == ["masonry"]`，所以「构造柱浇筑」已从
    `CONC_NEW_COLUMN`（混凝土族）改挂砌筑族的「方柱-混水」。原 docstring 里那句
    「如果哪天有人把配置改对了…那时应当改成断言'未出现'而不是直接删掉」**就是现在**：
    护栏不拆，只把后半段从「违规**被**抓到」改成「违规**不再出现**」。
    """
    assert "CONC_NEW_COLUMN" not in {
        c["activity_id"] for c in _scope()["l4_candidates"]["concrete"]}, \
        "前置条件：剪力墙结构下不得保留柱浇筑"

    # 判据函数本身仍然有效：把 CONC_NEW_COLUMN 塞进树，照样判违规（护栏不空转）
    probe = kc.check_scope_conformance(
        _wbs(("二次结构与砌体", "二次结构", "构造柱浇筑", "CONC_NEW_COLUMN")), _scope())
    assert probe["violations"] == 1
    assert _kinds(probe) == {"CONC_NEW_COLUMN": kc.KIND_BANNED}

    # 真实配置：声明层 + 展开后的整棵子树都不得再出现柱浇筑/任何混凝土族叶子
    import copy

    from pipeline import layer_engine as LE
    from pipeline.nodes.beat_configs import BASE_BEAT_CONFIGS

    cfg = BASE_BEAT_CONFIGS["二次结构与砌体"]
    declared = list(cfg.get("cycle") or []) + list(cfg.get("attach_measures") or []) \
        + list(cfg.get("parallel_work") or [])
    assert declared
    assert "CONC_NEW_COLUMN" not in {s.get("kb_activity_id") for s in declared}
    assert not [s for s in declared if s.get("work_type_id") == "concrete"], declared

    phase_dict, _ids = LE.expand_node(copy.deepcopy(cfg), {})
    leaves = [l for wp in phase_dict["work_packages"]
              for l in (wp.get("sub_packages") or [])]
    assert leaves
    assert "CONC_NEW_COLUMN" not in {l.get("kb_activity_id") for l in leaves}
    assert not [l for l in leaves
                if (l.get("l3_work_type_id") or l.get("work_type_id")) == "concrete"], \
        "二次结构与砌体的子树里不许再有任何 L3=concrete 的叶子"


def test_scope_conformance_reflects_real_kb_for_all_scope_activities():
    """全量对照：把 kb_scope 允许的活动原样放进树 → 必须 0 违规（防止误报）。

    这是"不误报"的方向性护栏：如果分类逻辑把在范围内的工序判成违规，
    工具就会变成噪声源，用户会直接忽略它。
    """
    scope = _scope()
    leaves = []
    for wt, items in scope["l4_candidates"].items():
        for it in items[:2]:
            leaves.append(("某相", wt, it["activity_name"] or wt, it["activity_id"]))
    assert leaves, "前置条件：范围内有可选工序"
    r = kc.check_scope_conformance(_wbs(*leaves), scope)
    assert r["violations"] == 0, r["issues"]
    assert r["anchored"] == len(leaves)
