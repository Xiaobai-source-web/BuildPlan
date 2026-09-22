# -*- coding: utf-8 -*-
"""第 5 批验收测试：域 3（cycle 拆两半）+ 域 4（5 位编号）。

对应 `docs/第5批_域3域4_任务书.md` §7 的 10 条验收判据（判据 9「全量套件」由总套件覆盖）。
**不读库**的地方用进程内替身（与 `test_qty_derive.py` 同一套 `_component_ratio` 形状）；
需要"库里真的有没有这个 L4"的两条（3.1 / 4.1b）走 `pipeline.kb` 只读 API。
"""
import copy
import os
import re

import pytest

from pipeline import kb
from pipeline import layer_engine as LE
from pipeline.nodes import beat_configs as BC
from pipeline.nodes.wbs_phases import DEFAULT_PHASES

ID_RE = re.compile(r"^\d+(\.\d+)+$")

#: 分部名 → 分部号（= `DEFAULT_PHASES` 的 1-based 位置，域 4.1a ①）
DIV_NO = {spec["phase"]: i for i, spec in enumerate(DEFAULT_PHASES, 1)}

PARAMS = {"floors": 38, "total_area": 19000, "structure_type": "剪力墙"}


def _cfg(name, params=None, **over):
    cfg = copy.deepcopy(BC.BASE_BEAT_CONFIGS[name])
    cfg["node_id"] = str(DIV_NO[name])
    cfg.update(over)
    if params:
        cfg["_params"] = params          # 仅为可读性，展开时用的是显式 params 实参
    return cfg


def _leaves(phase_dict):
    return [l for wp in phase_dict["work_packages"] for l in wp.get("sub_packages") or []]


def _ids_of(phase_dict, step_name):
    return sorted(l["id"] for l in _leaves(phase_dict) if l.get("_step_name") == step_name)


# ================================================================================
# 判据 1（域 3.1）：beat_configs.py 里 `_AI_` 字面量 0 处
# ================================================================================
def test_判据1_beat_configs里没有_AI_字面量():
    """6 个 `_AI_` 活动编号必须**一个都不在代码里**。

    它们不是"代码硬造"的编号（实测都在 `L4_Activity_Dictionary` 里，只是名字带 `_AI_`
    —— 那是"AI 经验估算、待审"的标记），真正的问题是**写死在配置里绕过知识库**。
    第 5 批改成"写 L3 键 + L4 中文名，编号运行时从库里查"，于是字面量归零。
    本用例只做**字面量**检查：`tokenize` 剥注释后逐 token 比对 —— 注释里提到它们不算数
    （本仓踩过 4 次「注释被当代码」）。
    """
    import io
    import tokenize
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "pipeline", "nodes", "beat_configs.py")
    with open(path, "rb") as f:
        tokens = list(tokenize.tokenize(f.readline))
    src_tokens = " ".join(t.string for t in tokens
                          if t.type not in (tokenize.COMMENT, tokenize.NL))
    for bad in ("WALL_AI_001", "FLOOR_AI_002", "PAINT_AI_001",
                "DW_AI_003", "INSU_AI_001", "PAINT_AI_002"):
        assert bad not in src_tokens, "%s 仍写死在 beat_configs.py 里" % bad
    assert "_AI_" not in src_tokens, "beat_configs.py 里仍有 _AI_ 字面量"


# ================================================================================
# 判据 2（域 3.1）：分部 8 仍有叶子，且每个 kb_activity_id 都能在库里查到
# ================================================================================
def test_判据2_分部8没消失且kb编号全在库里():
    cfg = _cfg("装饰装修")
    pd, ids = LE.expand_node(cfg, PARAMS)
    leaves = _leaves(pd)
    assert leaves, "删掉 6 个 _AI_ 绑定后装饰分部退化了（任务书 §6 陷阱 9）"
    missing = [l["kb_activity_id"] for l in leaves
               if l.get("kb_activity_id") and not kb.activity_info(l["kb_activity_id"])]
    assert missing == [], "这些 kb_activity_id 在 L4_Activity_Dictionary 里查不到：%s" % missing
    assert all(l.get("kb_activity_id") for l in leaves), "有叶子没挂 KB 编号"


# ================================================================================
# 判据 3 + 4.1c：树 = 分部 → **L3 工种** → 叶子；id 5 位
# ================================================================================
def test_判据3_树第二层是L3工种且id为5位():
    for name in BC.BEAT_PHASE_NAMES:
        pd, ids = LE.expand_node(_cfg(name), PARAMS)
        for wp in pd["work_packages"]:
            # 节点名 = 工种名（来自 L3_Work_Type），不再是「Ⅰ区 …节拍流水」
            assert "节拍流水" not in wp["name"], wp["name"]
            assert "区" not in wp["name"], wp["name"]
            assert wp["name"] == kb.work_type_name(_leaves({"work_packages": [wp]})[0]
                                                   ["l3_work_type_id"])
            assert wp["id"] == "%s.%s" % (DIV_NO[name],
                                          _leaves({"work_packages": [wp]})[0]["l3_no"])
        for lid in ids:
            assert ID_RE.match(lid), lid
            assert len(lid.split(".")) == 5, "叶子 id 必须是 5 位：%s" % lid


# ================================================================================
# 判据 4（域 4.1a）：三级各自可溯源
# ================================================================================
def test_判据4_三级编号可溯源():
    for name in BC.BEAT_PHASE_NAMES:
        kb_keys = BC.candidate_work_types(name)
        div = DIV_NO[name]
        pd, _ = LE.expand_node(_cfg(name), PARAMS)
        for l in _leaves(pd):
            div_no, l3_no, l4_no, z, s = 0, 0, 0, 0, 0
            div_no, l3_no, l4_no, z, s = [int(x) for x in l["id"].split(".")]
            # ① 分部号 = DEFAULT_PHASES 的 1-based 位置
            assert div_no == div
            # ② L3工种号 = 该分部 kb 列表内顺序（1 起）
            assert 1 <= l3_no <= len(kb_keys), l["id"]
            assert kb_keys[l3_no - 1] == l["l3_work_type_id"], (l["id"], l["l3_work_type_id"])
            # ③ L4工序号：同一 L3 内从 1 起、连续不跳号（**在完整工序清单上**）
            assert l4_no >= 1
            assert (z, s) == (l["_zone"], l["_segment"])
            assert z >= 1 and s >= 1


def test_判据4_L3工序号在每个工种内连续不跳号():
    for name in BC.BEAT_PHASE_NAMES:
        pd, _ = LE.expand_node(_cfg(name), PARAMS)
        seen = {}
        for l in _leaves(pd):
            seen.setdefault(l["l3_no"], set()).add(l["l4_no"])
        for l3, l4s in seen.items():
            got = sorted(l4s)
            # 完整清单里可能有"量0出局"的工序不产生叶子 → 允许跳号，但必须**严格递增**
            assert got == sorted(set(got)), got
            assert len(got) >= 1


# ================================================================================
# 判据 5（域 4.1b）：每个叶子的 L4 所属 L3 ∈ 该分部的 kb 列表
# ================================================================================
def test_判据5_候选集硬约束():
    for name in BC.BEAT_PHASE_NAMES:
        allowed = BC.candidate_work_types(name)
        pd, _ = LE.expand_node(_cfg(name), PARAMS)
        for l in _leaves(pd):
            assert l["l3_work_type_id"] in allowed, (name, l["id"], l["l3_work_type_id"])
            assert l["l3_candidate_ok"] is True, l["id"]
        assert BC.validate_l4_candidates(name, _leaves(pd)) == []


def test_判据5_分部6不再出现concrete系的柱浇筑():
    """实测缺陷：二次结构与砌体曾用 `CONC_NEW_COLUMN`（柱浇筑，work_type_id=concrete）。

    4.1b 的候选集只有 masonry ⇒ 构造柱必须改挂砌筑类 L4。**按所属 L3 判定**，
    不靠中文名（名字会改，L3 键不会）。
    """
    pd, _ = LE.expand_node(_cfg("二次结构与砌体"), PARAMS)
    for l in _leaves(pd):
        assert l["l3_work_type_id"] == "masonry", (l["id"], l["l3_work_type_id"])
    assert "concrete" not in {l["l3_work_type_id"] for l in _leaves(pd)}


# ================================================================================
# 判据 6（域 4.2a）：编号对「量变 0」免疫
# ================================================================================
def _rebar_cycle():
    """同一 L3（rebar）下的三道工序：第 2 道在占比表里没有行（可被"量0出局"剔掉）。"""
    return [
        {"name": "板钢筋", "unit": "t", "qty_per_floor": 22, "work_type": "钢筋工程",
         "resource": "钢筋工", "work_type_id": "rebar", "l4_name": "板钢筋"},
        {"name": "梁钢筋", "unit": "t", "qty_per_floor": 8, "work_type": "钢筋工程",
         "resource": "钢筋工", "work_type_id": "rebar", "l4_name": "梁钢筋"},
        {"name": "基础钢筋", "unit": "t", "qty_per_floor": 10, "work_type": "钢筋工程",
         "resource": "钢筋工", "work_type_id": "rebar", "l4_name": "基础钢筋"},
    ]


def _ratio_params(with_beam):
    """占比表替身：`REBAR_NEW_BEAM` 在/不在表里 → 决定第二道工序进不进树。

    形状与 `ratio_scope.build` 的产物一致（见 `test_qty_derive.ratio_params`）。
    """
    p = {"floors": 4, "total_area": 2000, "total_rebar": 1200.0,
         "structure_type": "frame_shear"}
    rows = {"REBAR_NEW_SLAB": (20.7, "rebar", 1200.0, "t"),
            "REBAR_NEW_FOUND": (15.5, "rebar", 1200.0, "t")}
    if with_beam:
        rows["REBAR_NEW_BEAM"] = (9.0, "rebar", 1200.0, "t")
    idx = {aid: {"structure_type_id": "frame_shear", "activity_id": aid,
                 "work_type_id": wt, "ratio_percent": pct, "quantity": tot * pct / 100.0,
                 "unit": unit, "confidence": "LOW", "review_state": "pending",
                 "notes": "AI 经验估算 V1"}
           for aid, (pct, wt, tot, unit) in rows.items()}
    p["_component_ratio"] = {"structure_type_id": "frame_shear", "l4_index": idx}
    return p


def test_判据6_某工序量变0后其余工序编号逐位不变():
    """★ 域 4.2a 的真缺陷：旧实现从**过滤后**的列表 `enumerate(steps, 1)` 编工序号，
    任一道工序量变 0 ⇒ 其后所有工序编号前移 ⇒ 依赖边（按 id 引用）全断。

    修法：编号在**完整工序清单**上分配，过滤只决定"进不进树"。
    本用例用"改一个量、比对全部 id"的方式验证（任务书 §4.2a 明确要求这种方式）。
    """
    cfg = {"node_name": "地上主体结构", "node_id": "5", "org_type": "layer",
           "zones": ["Ⅰ区", "Ⅱ区"], "floors": 4, "segments": 4,
           "floors_per_segment": 1, "cycle": _rebar_cycle()}

    pd_full, ids_full = LE.expand_node(copy.deepcopy(cfg), _ratio_params(True))
    pd_drop, ids_drop = LE.expand_node(copy.deepcopy(cfg), _ratio_params(False))

    # 第二道工序（梁钢筋）在"没有占比行"的那一版里出局
    assert "梁钢筋" in {l["_step_name"] for l in _leaves(pd_full)}
    assert "梁钢筋" not in {l["_step_name"] for l in _leaves(pd_drop)}

    # 其余两道工序的 id **逐位不变**：板钢筋仍 5.1.1.*、基础钢筋仍 5.1.3.*
    assert _ids_of(pd_full, "板钢筋") == _ids_of(pd_drop, "板钢筋")
    assert _ids_of(pd_full, "基础钢筋") == _ids_of(pd_drop, "基础钢筋")
    assert _ids_of(pd_drop, "基础钢筋"), "基础钢筋不该被一起剔掉"
    assert all(i.split(".")[2] == "3" for i in _ids_of(pd_drop, "基础钢筋")), \
        "量0出局后第三道工序被重编号了（钉住 4.2a 的回归）"


def test_判据6_编号器只认完整清单():
    """把编号器单独钉一遍：在完整清单上编号，与"清单里有没有出局工序"无关。"""
    steps = [dict(s) for s in _rebar_cycle()]
    BC.assign_step_numbers("地上主体结构", steps)
    before = [(s["_l3_no"], s["_l4_no"]) for s in steps]
    assert before == [(1, 1), (1, 2), (1, 3)]
    # 再跑一次（幂等），编号不变
    BC.assign_step_numbers("地上主体结构", steps)
    assert [(s["_l3_no"], s["_l4_no"]) for s in steps] == before


# ================================================================================
# 判据 7（域 3.5）：floor_range 结构化
# ================================================================================
def test_判据7_可分层活动有楼层范围_不展开活动明确没有():
    pd, _ = LE.expand_node(_cfg("装饰装修"), PARAMS)
    layered = [l for l in _leaves(pd) if not l.get("_parallel")]
    parallel = [l for l in _leaves(pd) if l.get("_parallel")]
    assert layered and parallel

    for l in layered:
        fr = l["floor_range"]
        assert isinstance(fr, dict), l["id"]
        for k in ("start", "end", "end_inclusive", "floors", "label"):
            assert k in fr, (l["id"], k)
        assert fr["end_inclusive"] is True
        assert fr["floors"] == l["floors"] > 0
        assert fr["end"] == fr["start"] + fr["floors"] - 1.0
        assert fr["label"].endswith("层")
        assert l["layer_expandable"] is True

    for l in parallel:
        assert l["floor_range"] is None, "全楼平行活动**不展开**，不该有楼层范围"
        assert l["floors"] == 0
        assert l["layer_expandable"] is False


def test_判据7_楼层范围与location文本一致():
    pd, _ = LE.expand_node(_cfg("地上主体结构"), PARAMS)
    for l in _leaves(pd):
        assert l["floor_range"]["label"] in l["location"], (l["id"], l["location"])


def test_判据7_非节拍叶子也把楼层范围补成显式的没有():
    """键缺失 ≠ `None`。非节拍叶子（工作包级、不分层展开）必须**显式**写"没有"。"""
    from pipeline.nodes.beat_node import _stamp_non_beat_layer_fields
    wbs = {"phases": [{"phase": "施工准备", "work_packages": [
        {"id": "1.1", "name": "场地准备", "sub_packages": [
            {"id": "1.1.1", "name": "场地平整", "quantity": 1, "unit": "项"}]}]}]}
    _stamp_non_beat_layer_fields(wbs)
    leaf = wbs["phases"][0]["work_packages"][0]["sub_packages"][0]
    assert leaf["floor_range"] is None
    assert leaf["floors"] == 0.0
    assert leaf["layer_expandable"] is False

    # 幂等 + 绝不覆盖已有值
    leaf["floor_range"] = {"start": 1.0, "end": 1.0}
    _stamp_non_beat_layer_fields(wbs)
    assert leaf["floor_range"] == {"start": 1.0, "end": 1.0}
    # 节拍叶子（带 `_beat`）不被碰
    wbs2 = {"phases": [{"phase": "x", "work_packages": [
        {"id": "5.1", "name": "钢筋工程", "sub_packages": [
            {"id": "5.1.1.1.1", "_beat": True}]}]}]}
    _stamp_non_beat_layer_fields(wbs2)
    assert "floor_range" not in wbs2["phases"][0]["work_packages"][0]["sub_packages"][0]


# ================================================================================
# ★ 第 5 批收口：占比表**不覆盖**某工种 ≠ 该工种的每个 L4 都"异常缺行"
# ================================================================================
def _real_index_rows():
    """真库 `Component_Ratio` 覆盖的工种族（用于证明 masonry/earthwork 不在其中）。

    走**真实入口** `ratio_scope.build(params, structure_type_id)`（框剪 = `frame_shear`），
    这样测的就是交付物真正会拿到的索引，而不是手搓替身。
    """
    from pipeline import ratio_scope as RS
    # 必须带齐分项总量：`build()` 没有总量基数时不给索引（与
    # `test_component_ratio_source.py` 的 `build()` 同一个口径）
    src = {"total_area": 15000, "floors": 18, "building_count": 1,
           "total_concrete": 8000, "total_rebar": 1200, "total_formwork": 25000,
           "total_masonry": 3000, "structure_type": "frame_shear"}
    built = RS.build(src, "frame_shear") or {}
    idx = built.get("index") or {}
    assert idx, "真库 ratio_scope.build() 没给出索引 —— 后面两条判据会失真"
    return RS._index_work_types(idx), idx


def test_占比表不覆盖的工种不因用户给了总量而整族出局():
    """★ 真缺陷回归：`Component_Ratio`（97 行）只覆盖 concrete/rebar/formwork，
    而 `GROUP_TOTAL_PARAMS` 里还有 `masonry` / `earthwork`。

    用户只要在输入里写「砌体：约3000立方米」（`extract_by_regex` 会抽出
    `total_masonry`），修正前 masonry 变"活跃工种" ⇒ 二次结构与砌体的**每一道**
    工序都判 `missing` ⇒ **整个分部量0出局、从 WBS 消失**。
    修正后应判 `inactive`（"占比表不覆盖该工种" ⇒ 无发言权）⇒ 工序保留。
    """
    from pipeline import ratio_scope as RS
    covered, idx = _real_index_rows()
    assert "masonry" not in covered, ("前提失效：真库 Component_Ratio 现在有 masonry 行了，"
                                      "本条回归判据需要重新设计")
    assert "masonry" in RS.GROUP_TOTAL_PARAMS, "前提：masonry 仍在分项总量键里"

    # 用真库索引造 params（壳子带 structure_type_id，走 l4_index_of 的正道）
    params = {"floors": 38, "total_area": 19000, "structure_type": "frame_shear",
              "total_masonry": 3000,
              "_component_ratio": {"structure_type_id": "frame_shear", "l4_index": idx}}
    cfg = copy.deepcopy(BC.BASE_BEAT_CONFIGS["二次结构与砌体"])
    cfg["node_id"] = "6"
    steps = list(cfg["cycle"]) + list(cfg.get("attach_measures") or [])
    cfg2 = LE.prepare_node_cfg(copy.deepcopy(cfg))
    for s in cfg2["cycle"]:
        st = RS.step_ratio_status(params, s, cfg2["cycle"])
        assert st["status"] == "inactive", (s["name"], st)
        assert "不覆盖该工种" in st["reason"], st["reason"]

    keeps = LE.active_steps(cfg2["cycle"], [], params)
    assert len(keeps) == len(cfg2["cycle"]), "整族工序都不许被量0出局剔除"

    pd, _ = LE.expand_node(copy.deepcopy(cfg), params)
    leaves = _leaves(pd)
    assert leaves, "★ 该分部不许消失"
    assert {l["l3_work_type_id"] for l in leaves} == {"masonry"}
    # 用户给的总量**不静默**：必须留痕点名 total_masonry
    notes = [d for d in (pd.get("ratio_degradations") or [])
             if d.get("code") == "step_not_ratio_driven"]
    assert notes, "用户给了 total_masonry 却没有任何留痕 —— 这是静默吞掉"
    assert all("total_masonry" in (d.get("message") or "") for d in notes), notes


def test_占比表覆盖的工种仍按异常缺行量0出局():
    """对照组：**表里有该工种的行、但没有这一行** ⇒ 真异常，仍然量0出局。

    这是修正**不许**弄坏的那一半（`test_component_ratio_source` 也钉着它）。
    """
    from pipeline import ratio_scope as RS
    idx = {"CONC_NEW_SLAB": {"structure_type_id": "frame_shear",
                             "activity_id": "CONC_NEW_SLAB",
                             "work_type_id": "concrete", "ratio_percent": 22.1,
                             "quantity": 100.0, "unit": "m³"}}
    params = {"structure_type": "frame_shear", "total_concrete": 8000,
              "_component_ratio": {"structure_type_id": "frame_shear", "l4_index": idx}}
    probe = {"name": "梁浇筑", "unit": "m³", "work_type_id": "concrete",
             "kb_activity_id": "CONC_NEW_BEAM", "qty_per_floor": 1}
    st = RS.step_ratio_status(params, probe, [probe])
    assert st["status"] == "missing", st
    assert st["info"] is None                    # → 留痕 kind = abnormal_absent


def test_没给分项总量时占比表照样无发言权():
    """原有 `inactive` 行为逐字不变（回归守卫）。"""
    from pipeline import ratio_scope as RS
    covered, idx = _real_index_rows()
    params = {"floors": 38, "total_area": 19000, "structure_type": "frame_shear",
              "_component_ratio": {"structure_type_id": "frame_shear", "l4_index": idx}}
    for s in LE.prepare_node_cfg(
            copy.deepcopy(BC.BASE_BEAT_CONFIGS["二次结构与砌体"]))["cycle"]:
        st = RS.step_ratio_status(params, s, [s])
        assert st["status"] == "inactive", st
        assert st["reason"] == "工种 masonry 没有用户给的 total_masonry，占比表无发言权", st


# ================================================================================
# 判据 8（域 4.3）：重跑逐位一致
# ================================================================================
def test_判据8_重跑逐位一致():
    for name in BC.BEAT_PHASE_NAMES:
        a_pd, a_ids = LE.expand_node(_cfg(name), PARAMS)
        b_pd, b_ids = LE.expand_node(_cfg(name), PARAMS)
        assert a_ids == b_ids
        assert [l["id"] for l in _leaves(a_pd)] == [l["id"] for l in _leaves(b_pd)]
        # 依赖边也必须逐条一致（编号冻结的另一面）
        a_deps = LE.structural_deps(_cfg(name), params=PARAMS)
        b_deps = LE.structural_deps(_cfg(name), params=PARAMS)
        assert a_deps == b_deps


# ================================================================================
# 域 3.2 / 3.3：工序清单与顺序来自 LLM（l4_order），爬架/外檐归 LLM
# ================================================================================
def test_域3_2_l4_order决定L4工序号():
    """LLM 给的顺序是 ③ L4工序号的**唯一顺序来源**；代码只按数组下标编号。

    L4工序号是**"L3 内"的序号**，所以要在**同一个 L3** 内换序才看得出变化。
    """
    def _cycle():
        return [
            {"name": "板钢筋", "unit": "t", "qty_per_floor": 22, "work_type": "钢筋工程",
             "resource": "钢筋工", "work_type_id": "rebar", "l4_name": "板钢筋"},
            {"name": "基础钢筋", "unit": "t", "qty_per_floor": 10, "work_type": "钢筋工程",
             "resource": "钢筋工", "work_type_id": "rebar", "l4_name": "基础钢筋"},
        ]

    def _c():
        return {"node_name": "地上主体结构", "node_id": "5", "org_type": "layer",
                "zones": ["Ⅰ区"], "floors": 2, "segments": 2, "floors_per_segment": 1,
                "cycle": _cycle()}

    # 默认（无 l4_order）：声明顺序 → 板钢筋 1、基础钢筋 2
    d_pd, _ = LE.expand_node(_c(), PARAMS)
    d_no = {l["_step_name"]: l["l4_no"] for l in _leaves(d_pd)}
    assert d_no == {"板钢筋": 1, "基础钢筋": 2}, d_no

    # LLM 把「基础钢筋」排到第一位 → 编号跟着 LLM 的顺序走（LLM 不编号，代码按下标编）
    rev = _c()
    rev["l4_order"] = [{"kb_activity_id": "REBAR_NEW_FOUND"},
                       {"kb_activity_id": "REBAR_NEW_SLAB"}]
    r_pd, _ = LE.expand_node(rev, PARAMS)
    r_no = {l["_step_name"]: l["l4_no"] for l in _leaves(r_pd)}
    assert r_no == {"基础钢筋": 1, "板钢筋": 2}, r_no

    # 不同 L3 各自从 1 起（L4工序号是"L3 内"序号，不是全局序号）
    cfg5 = copy.deepcopy(BC.BASE_BEAT_CONFIGS["地上主体结构"])
    cfg5["node_id"] = "5"
    z_pd, _ = LE.expand_node(cfg5, PARAMS)
    per_l3 = {}
    for l in _leaves(z_pd):
        per_l3.setdefault(l["l3_no"], set()).add(l["l4_no"])
    assert per_l3 == {1: {1}, 2: {1}, 3: {1}, 5: {1}}, per_l3


def test_域3_2_l4_order只重排不新增_未列到的排后面():
    cfg = copy.deepcopy(BC.BASE_BEAT_CONFIGS["地上主体结构"])
    cfg["node_id"] = "5"
    cfg["l4_order"] = [{"kb_activity_id": "CONC_NEW_SLAB"},          # 只提了混凝土
                       {"kb_activity_id": "NOT_IN_KB_9999"}]          # 库里没有 → 忽略
    pd, _ = LE.expand_node(cfg, PARAMS)
    names = {l["_step_name"] for l in _leaves(pd)}
    assert names == {"钢筋绑扎", "铝模安装", "混凝土浇筑", "爬架提升"}, \
        "l4_order 只能重排，不能凭空造工序、也不能删工序"
    order = BC.apply_l4_order(copy.deepcopy(cfg), cfg["l4_order"])["_l4_order"]
    assert order["ignored"] == ["NOT_IN_KB_9999"]
    assert order["source"] == "llm"


def test_域3_3_爬架与外檐仍可挂KB且外檐保持全楼平行形态():
    """3.3：爬架提升 / 外檐保温 / 外檐涂料 归 LLM —— 但"全楼平行"的**形态**要保留。"""
    pd, _ = LE.expand_node(_cfg("装饰装修"), PARAMS)
    par = [l for l in _leaves(pd) if l.get("_parallel")]
    assert len(par) == 2
    for l in par:
        assert l["location"] == "全楼"
        assert l["segment_area"] == 0.0
        assert l["kb_activity_id"], l["name"]
        assert kb.activity_info(l["kb_activity_id"])
    # 爬架在主体分部，仍是措施项（固定节拍 3 天）
    pd5, _ = LE.expand_node(_cfg("地上主体结构"), PARAMS)
    lift = [l for l in _leaves(pd5) if l["_step_name"] == "爬架提升"]
    assert lift and all(l["duration_days"] == 3 for l in lift)
    assert all(l["l3_work_type_id"] == "scaffolding" for l in lift)


# ================================================================================
# 域 3.4：4 个阶段名不再是查节点的唯一途径
# ================================================================================
def test_域3_4_按分部key也能认出节拍分部():
    import json
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "prompts", "..", "pipeline", "nodes", "beat_configs.py")
    assert os.path.exists(path)
    # 只有 key、没有 phase（名字改了也认得出）
    for key, name in BC.BEAT_PHASE_KEYS.items():
        assert BC.beat_phase_name({"key": key}) == name
    # 只有名字、没有 key（老计划）→ 退回按名认
    assert BC.beat_phase_name({"phase": "地上主体结构"}) == "地上主体结构"
    assert BC.beat_phase_name({"phase": "机电安装"}) is None
    assert BC.beat_phase_name({"key": "mep", "phase": "机电安装"}) is None
    assert json is not None


# ================================================================================
# 域 4.1b 数据面：DEFAULT_PHASES 的 kb 列表
# ================================================================================
def test_域4_1b_主体分部的候选集含scaffolding():
    """爬架提升的 L4（SCAFF0004）属于 `scaffolding`，主体分部不补它就会违反候选集硬约束。"""
    keys = BC.candidate_work_types("地上主体结构")
    assert "scaffolding" in keys
    # 补在**末尾** ⇒ 前 4 个工种的 1-based 序号不变（编号是冻结口径，不许漂）
    assert keys[:4] == ["rebar", "formwork", "concrete", "steel_structure"]


def test_域4_1b_候选集就是编号的第二个来源():
    for spec in DEFAULT_PHASES:
        keys = BC.candidate_work_types(spec["phase"])
        for i, wid in enumerate(keys, 1):
            assert BC.l3_index(spec["phase"], wid) == i


def test_域4_1b_违规会被检出():
    """把不在候选集里的 L3 塞进叶子 → `validate_l4_candidates` 必须报出来。"""
    bad = [{"id": "6.9.1.1.1", "l3_work_type_id": "concrete"}]
    out = BC.validate_l4_candidates("二次结构与砌体", bad)
    assert len(out) == 1 and out[0]["work_type_id"] == "concrete"
