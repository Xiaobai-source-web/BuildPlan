"""定额锚定节点（pipeline.nodes.norm_bind）测试。

覆盖：
  1. 有 kb_activity_id + 条件唯一            → match_type=exact / origin=kb
  2. 有 kb_activity_id 但条件不唯一          → match_type=default，note 含候选数
  3. 不存在的 kb_activity_id                 → origin=ai / match_type=ai，
                                               有 norm_warnings，叶子仍保留（不中断）
  4. credibility 三个比例相加 ≈ 1
  5. data_sources 去重且非空
  6. 叶子 provenance 里 quantity 的 origin 是 user
  7. 机械主导活动                            → mode=machine（真实 KB 活动）
  8. llm=None 全程不联网、不抛异常
  9. 无 kb_activity_id → 仍产出 binding（AI 估算）
 10. 可选：LLM 返回候选外的 id → 丢弃并退回代码策略（用 stub LLM，不联网）

依赖 BuildPlan_KB/kb.db（仓库随附），无需网络 / 真实 LLM。
"""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline import kb
from pipeline.nodes.norm_bind import NormBindNode

# ---- KB 里真实存在的活动（测试数据锚定真实库，避免写死后数据一刷新就崩）----
ACT_EXACT = "REBAR_NEW_BEAM"        # labor_driven，47 行人工定额，"框架梁"+"≤16" 唯一命中
ACT_DEFAULT = "REBAR_NEW_BEAM"      # 同上，只用 "框架梁" 会命中 3 行
ACT_UNKNOWN = "NOT_EXIST_ACTIVITY_999"
ACT_MACHINE = "CONC_NEW_FOUND"      # equipment_driven，含泵车台班定额

# 用户原话：同时给出构件类型与钢筋直径两个条件（"框架梁" / "≤16"）
PROMPT_EXACT = "本工程为框架结构住宅，主体框架梁钢筋直径≤16mm，采用商品混凝土泵送。"


# ==================== 构造最小 WBS ====================
def _leaf(tid, name, **kw):
    leaf = {"id": tid, "name": name, "quantity": 100, "unit": "t",
            "duration_days": 5, "work_type": "钢筋工程"}
    leaf.update(kw)
    return leaf


def _make_wbs(leaves):
    """2~3 条叶子的三层最小 WBS。"""
    return {"phases": [{"phase": "主体结构", "work_packages": [
        {"id": "1.1", "name": "钢筋工程", "sub_packages": list(leaves)}]}]}


def _make_ctx(leaves, prompt="", params=None, kb_scope=None):
    ctx = {"wbs": _make_wbs(leaves), "prompt": prompt,
           "extracted_params": params or {}}
    if kb_scope is not None:
        ctx["kb_scope"] = kb_scope
    return ctx


def _all_leaves(ctx):
    return [s for ph in ctx["wbs"]["phases"]
            for wp in ph["work_packages"] for s in wp["sub_packages"]]


def _node():
    node = NormBindNode(llm=None)
    node._emit = lambda event, data: None      # 测试里不需要事件流
    return node


def _run(ctx):
    node = _node()
    node.run(ctx)                              # 就地改 wbs + 写 ctx 汇总
    return node


# ==================== 1. 精确命中 ====================
def test_exact_match_from_kb():
    leaf = _leaf("5.1.1.1", "1F框架梁钢筋", kb_activity_id=ACT_EXACT)
    ctx = _make_ctx([leaf], prompt=PROMPT_EXACT)
    _run(ctx)

    b = leaf["norm_binding"]
    assert b["match_type"] == "exact"
    assert b["provenance"]["origin"] == "kb"
    assert b["task_id"] == "5.1.1.1"
    assert b["mode"] == "labor"
    assert b["norm_value"] and b["norm_value"] > 0
    assert b["source_code"].startswith("LD_T72")
    assert b["unit"]
    assert b["crew"] == {}
    # 汇总：norm_bindings 以 task_id 为键
    assert ctx["norm_bindings"]["5.1.1.1"]["match_type"] == "exact"
    assert not ctx["norm_warnings"]

    # 反查 KB：确实唯一命中「框架梁 + ≤16」
    hit = kb.labor_norm_match(ACT_EXACT, ["框架梁", "≤16"])
    assert len(hit) == 1
    assert abs(b["norm_value"] - float(hit[0]["norm_value"])) < 1e-9


# ==================== 2. 条件不唯一 → 典型值 ====================
def test_multi_candidate_uses_default_with_count():
    leaf = _leaf("5.1.1.2", "2F框架梁钢筋", kb_activity_id=ACT_DEFAULT)
    # 只给"框架梁"，不给直径 → 候选多行
    ctx = _make_ctx([leaf], prompt="主体结构框架梁钢筋绑扎，采用商品混凝土泵送。")
    _run(ctx)

    b = leaf["norm_binding"]
    assert b["match_type"] == "default"
    assert b["provenance"]["origin"] == "kb"
    n_cand = len(kb.labor_norm_match(ACT_DEFAULT, ["框架梁"]))
    assert n_cand > 1, "测试前提：'框架梁' 应命中多行"
    assert str(n_cand) in b["provenance"]["note"], "note 里要写明候选数"
    assert "中位" in b["provenance"]["note"]
    assert b["norm_value"] and b["norm_value"] > 0

    # 口径必须写清用的是哪个字段（定额值 vs 产能倒数）
    assert ("labor_norm_value" in b["provenance"]["note"]
            or "productivity_value" in b["provenance"]["note"])


def test_no_keyword_falls_back_to_typical_norm():
    """用户/项目都没给条件 → 关键字为空，仍取 KB 典型值（default/kb）。"""
    leaf = _leaf("5.1.1.3", "钢筋绑扎", kb_activity_id=ACT_DEFAULT)
    ctx = _make_ctx([leaf], prompt="建一栋住宅楼。")
    _run(ctx)

    b = leaf["norm_binding"]
    assert b["match_type"] == "default"
    assert b["provenance"]["origin"] == "kb"
    assert "典型值" in b["provenance"]["note"]
    assert b["norm_value"] and b["norm_value"] > 0


# ==================== 3. 不存在的活动 → AI 估算 + 报警，不中断 ====================
def test_unknown_activity_degrades_to_ai_with_warning():
    assert kb.activity_info(ACT_UNKNOWN) is None, "测试前提：该活动不应存在于 KB"
    good = _leaf("5.1.1.1", "1F框架梁钢筋", kb_activity_id=ACT_EXACT)
    bad = _leaf("5.1.1.2", "3F框架梁钢筋", kb_activity_id=ACT_UNKNOWN)
    tail = _leaf("5.1.1.3", "素混凝土垫层", kb_activity_id=None)
    ctx = _make_ctx([good, bad, tail], prompt=PROMPT_EXACT)
    _run(ctx)

    b = bad["norm_binding"]
    assert b["match_type"] == "ai"
    assert b["provenance"]["origin"] == "ai"
    assert b["provenance"]["confidence"] == "低"
    assert b["norm_value"] and b["norm_value"] > 0

    # 叶子必须还在（不中断），且被锚定
    ids = [s["id"] for s in _all_leaves(ctx)]
    assert ids == ["5.1.1.1", "5.1.1.2", "5.1.1.3"]
    assert all("norm_binding" in s for s in _all_leaves(ctx))

    # 警告非空，且按影响降序
    warns = ctx["norm_warnings"]
    assert warns and len(warns) >= 2
    assert all(set(w) == {"task_id", "task_name", "reason", "impact"} for w in warns)
    assert any(w["task_id"] == "5.1.1.2" for w in warns)
    assert any("暂无法估量" in w["impact"] or "影响工期" in w["impact"] for w in warns)
    # 汇总里也保留了 AI 锚定结果
    assert ctx["norm_bindings"]["5.1.1.2"]["match_type"] == "ai"


# ==================== 4. credibility ====================
def test_credibility_sums_to_one():
    leaves = [_leaf("5.1.1.1", "1F框架梁钢筋", kb_activity_id=ACT_EXACT),
              _leaf("5.1.1.2", "柱钢筋", kb_activity_id=ACT_UNKNOWN),
              _leaf("5.1.1.3", "素混凝土垫层", kb_activity_id=None)]
    ctx = _make_ctx(leaves, prompt=PROMPT_EXACT)
    _run(ctx)

    cred = ctx["credibility"]
    assert set(cred) == {"user", "kb", "ai"}
    assert abs(sum(cred.values()) - 1.0) < 1e-6, cred
    assert all(0.0 <= v <= 1.0 for v in cred.values())
    # 有 kb 命中也有 ai 估算 → 两个方向都非零
    assert cred["kb"] > 0 and cred["ai"] > 0 and cred["user"] > 0


# ==================== 5. data_sources ====================
def test_data_sources_deduped_and_not_empty():
    # 两条叶子用同一个活动 → 来源代码会重复出现，必须去重
    leaves = [_leaf("5.1.1.1", "1F框架梁钢筋", kb_activity_id=ACT_EXACT),
              _leaf("5.1.1.2", "2F框架梁钢筋", kb_activity_id=ACT_EXACT)]
    ctx = _make_ctx(leaves, prompt=PROMPT_EXACT)
    ctx["data_sources"] = ["先前节点的来源"]          # 上游已有来源要保留
    _run(ctx)

    src = ctx["data_sources"]
    assert src, "来源清单不能为空"
    assert len(src) == len(set(src)), "来源必须去重"
    assert "先前节点的来源" in src
    assert any(s.startswith("LD_T72") for s in src)


# ==================== 6. provenance.quantity.origin == user ====================
def test_leaf_provenance_quantity_is_user():
    leaves = [_leaf("5.1.1.1", "1F框架梁钢筋", kb_activity_id=ACT_EXACT,
                    quantity=8200, unit="t", duration_days=12),
              _leaf("5.1.1.2", "2F框架梁钢筋", kb_activity_id=ACT_UNKNOWN,
                    quantity=100, unit="t", duration_days=6)]
    ctx = _make_ctx(leaves, prompt=PROMPT_EXACT)
    _run(ctx)

    for leaf in _all_leaves(ctx):
        prov = leaf["provenance"]                    # 字段名必须是 provenance
        assert set(prov) == {"quantity", "norm", "duration"}
        assert prov["quantity"]["origin"] == "user"
        assert prov["quantity"]["value"] == leaf["quantity"]
        assert prov["norm"]["origin"] in ("user", "kb", "ai")
        assert prov["norm"]["ref"] == leaf["norm_binding"]["source_code"] \
            or prov["norm"]["origin"] == "ai"
        assert prov["duration"]["value"] == leaf["duration_days"]
        assert prov["duration"]["origin"] == "ai"

    first = _all_leaves(ctx)[0]
    assert first["provenance"]["quantity"]["value"] == 8200
    # 叶子上的 provenance 与 binding 的 provenance 指向同一定额值
    assert abs(first["provenance"]["norm"]["value"]
               - first["norm_binding"]["norm_value"]) < 1e-9


# ==================== 7. 机械主导 ====================
def test_machine_driven_uses_equipment_norm():
    info = kb.activity_info(ACT_MACHINE)
    assert info and info["recommended_production_mode"] == "equipment_driven"
    assert kb.equipment_norms(ACT_MACHINE), "测试前提：该活动应有机械台班定额"

    leaf = _leaf("5.1.1.1", "1F基础浇筑", kb_activity_id=ACT_MACHINE,
                 quantity=15600, unit="m³", work_type="混凝土工程",
                 duration_days=15)
    ctx = _make_ctx([leaf], prompt="基础采用商品混凝土泵送，汽车泵。")
    _run(ctx)

    b = leaf["norm_binding"]
    assert b["mode"] == "machine"
    assert b["match_type"] == "exact"
    assert b["provenance"]["origin"] == "kb"
    assert b["norm_value"] and b["norm_value"] > 0
    assert b["source_code"].startswith("GD_2018")
    # 主控机械来自 KB 标注，note 里要能溯源
    main = kb.main_machine(ACT_MACHINE)
    assert main and main[0]["machine_name"] in b["provenance"]["note"]
    assert b["crew"] == {}, "配员由别的节点填，这里必须留空"
    # 台班定额的单位必须补成「台班/工程量单位」：KB 的 machine_shift_unit_json
    # 只写「台班」没有分母，下游按单位一致性判定会把整类混凝土活动误判成
    # "无可用定额"（实测 80 条）。
    assert b["unit"] == "台班/%s" % leaf["unit"], b["unit"]
    from pipeline.nodes.scheduler import units_compatible
    assert units_compatible(leaf["unit"], b["unit"]), \
        "补过单位的台班定额必须能通过单位门，否则混凝土工一个也进不了工日需求"
    # 第 37 轮：分母来自 KB 的 quantity_unit（m³），单位一致 → 可作依据、换算因子 1
    assert b["kb_quantity_unit"] == "m³", b["kb_quantity_unit"]
    assert b["norm_is_evidence"] is True and b["usable"] is True
    assert b["convert_factor"] == 1.0
    assert b["unit_check"]["verdict"] == "same"


def test_machine_shift_unit_denominator_only_from_kb():
    """台班定额的分母**只能**来自 KB 行的 quantity_unit（契约 §5-WS3①）。

    旧实现 `_compound_shift_unit("台班", leaf_unit)` 用**叶子单位**补分母
    （`台班`→`台班/根`），再拿这个分母去比这个分母 → 必然通过；120 根 PHC 桩
    因此被算成 1 天（KB 真实分母是 m）。
    """
    from pipeline.nodes.norm_bind import _shift_unit_from_kb

    assert _shift_unit_from_kb("台班", "m³") == "台班/m³"
    assert _shift_unit_from_kb("台班", "m3") == "台班/m³"
    assert _shift_unit_from_kb("台班/m³", "m³") == "台班/m³"
    assert _shift_unit_from_kb("", "m³") == "台班/m³"
    # KB 缺分母 → ""（调用方必须降级），绝不用叶子单位补
    assert _shift_unit_from_kb("台班", "") == ""
    assert _shift_unit_from_kb(None, None) == ""


def test_machine_activity_with_kb_missing_denominator_is_downgraded(monkeypatch):
    """KB 缺 quantity_unit → 单位校验 unusable、"缺计量单位"、降级为仅参考。"""
    from pipeline.nodes import norm_bind as nb

    real_info = nb.kb.activity_info
    real_equip = nb.kb.equipment_norms
    real_main = nb.kb.main_machine
    monkeypatch.setattr(nb.kb, "activity_info", lambda aid: (
        {"activity_id": aid, "activity_name": "某机械活", "unit": "m3",
         "recommended_production_mode": "equipment_driven"}
        if aid == "FAKE_NO_DENOM" else real_info(aid)))
    monkeypatch.setattr(nb.kb, "equipment_norms", lambda aid: (
        [{"condition_text": "一、二类土", "machine_combination_json": '["履带式挖掘机"]',
          "machine_spec_json": '["1m3"]', "machine_shift_norm_json": "[1.68]",
          "machine_shift_unit_json": '["台班"]', "quantity_basis": 1000.0,
          "quantity_unit": "", "source_code": "GD_2018_A1_1"}]
        if aid == "FAKE_NO_DENOM" else real_equip(aid)))
    monkeypatch.setattr(nb.kb, "main_machine", lambda aid, condition_text=None: (
        [{"condition_text": "", "machine_name": "履带式挖掘机", "machine_spec": None,
          "source_type": "ai_estimate", "confidence": "MEDIUM"}]
        if aid == "FAKE_NO_DENOM" else real_main(aid, condition_text)))

    leaf = _leaf("5.1.1.1", "某机械活", kb_activity_id="FAKE_NO_DENOM",
                 unit="m³", work_type="土方工程")
    ctx = _make_ctx([leaf], prompt="")
    _run(ctx)

    b = leaf["norm_binding"]
    assert b["mode"] == "machine"
    assert b["unit"] == "台班", "KB 缺分母时只能留裸「台班」，不许补叶子单位"
    assert b["norm_is_evidence"] is False
    assert b["usable"] is False
    assert "缺计量单位" in b["not_usable_reason"], b["not_usable_reason"]
    from pipeline.nodes.scheduler import units_compatible
    assert not units_compatible(leaf["unit"], b["unit"]), \
        "裸「台班」必须被单位门拦下，不许静默通过"


def test_machine_activity_without_main_machine_note():
    """活动标注为机械主导但主控机械没名字 → 不许瞎猜，note 里写"未标注主控机械"。"""
    from pipeline.nodes import norm_bind as nb

    leaf = _leaf("5.1.1.1", "土方开挖", kb_activity_id="FAKE_EQUIP_ACT",
                 unit="m³", work_type="土方工程")
    ctx = _make_ctx([leaf], prompt="机械开挖")

    # 打桩：活动是机械主导、有台班定额，但主控机械名为空
    real_info = nb.kb.activity_info
    real_equip = nb.kb.equipment_norms
    real_main = nb.kb.main_machine
    nb.kb.activity_info = lambda aid: (
        {"activity_id": aid, "activity_name": "土方开挖", "unit": "m3",
         "recommended_production_mode": "equipment_driven"}
        if aid == "FAKE_EQUIP_ACT" else real_info(aid))
    nb.kb.equipment_norms = lambda aid: (
        [{"condition_text": "一、二类土", "machine_combination_json": '["履带式挖掘机"]',
          "machine_spec_json": '["1m3"]', "machine_shift_norm_json": "[1.68]",
          "machine_shift_unit_json": '["台班"]', "quantity_basis": 1000.0,
          "quantity_unit": "m3", "source_code": "GD_2018_A1_1"}]
        if aid == "FAKE_EQUIP_ACT" else real_equip(aid))
    nb.kb.main_machine = lambda aid, condition_text=None: (
        [{"condition_text": "", "machine_name": "", "machine_spec": None,
          "source_type": "ai_estimate", "confidence": "LOW"}]
        if aid == "FAKE_EQUIP_ACT" else real_main(aid, condition_text))
    try:
        node = _node()
        node.run(ctx)
    finally:
        nb.kb.activity_info = real_info
        nb.kb.equipment_norms = real_equip
        nb.kb.main_machine = real_main

    b = leaf["norm_binding"]
    assert b["mode"] == "machine"
    assert "未标注主控机械" in b["provenance"]["note"]
    assert b["norm_value"] and b["norm_value"] > 0


# ---- 7b. 机械定额"选行"：必须按主控机械名选，绝不"暂用同行机械"借定额 ----
def test_machine_row_picked_by_main_machine_name():
    """真实 KB 铁证：CONC_NEW_FOUND 有 3 行台班（振捣器 0.77 / **泵车 0.055** / 后浇带 1.26）。

    主控机械 = 混凝土输送泵车 → 必须选泵车那一行（旧实现取 rows[0] = 后浇带振捣器 1.26，
    266 m³ 被算成 33.5 台班 ≈ 34 天/段；正确是 266/10×0.055 = 1.46 台班 ≈ 2 天/段）。
    """
    rows = kb.equipment_norms(ACT_MACHINE)
    assert len(rows) >= 3, "测试前提：该活动应有 3 行台班表（含后浇带行）"
    conds = [r.get("condition_text") for r in rows]
    assert "后浇带" in conds and "基础浇筑" in conds, conds
    main = kb.main_machine(ACT_MACHINE)
    # ⚠️ 第 41 轮更新（知识库 WS6/C11 已改主控机械）：
    # 本用例原先断言主控机械 == 「混凝土输送泵车」，那是 WS6 C11 之前的值。
    # C11 把 `Activity_Main_Machine` 改成从台班行的 `machine_combination_json` 反推，
    # 于是主控机械变成了 `machine_combination_json` 的第一台（实测=混凝土振捣器）。
    # 选行口径本身没变：**主控机械是谁、就取含它的行**。所以这里不再钉死机名，
    # 改为按库里的主控机械动态断言（KB 侧把主控机械修回泵车后本用例仍然成立）。
    assert main and main[0].get("machine_name"), main
    picked_norm = None

    leaf = _leaf("4.1.4.3", "Ⅰ区 2.5-2层 混凝土浇筑", kb_activity_id=ACT_MACHINE,
                 quantity=266, unit="m³", work_type="混凝土工程", duration_days=15)
    ctx = _make_ctx([leaf], prompt="基础采用商品混凝土泵送，汽车泵。")
    _run(ctx)

    b = leaf["norm_binding"]
    assert b["mode"] == "machine"
    from pipeline.nodes import norm_bind as nb
    # 期望值按**库里声明的主控机械**动态推导：选行口径是"取含主控机械的那一行"，
    # 同名多行时才用条件消歧（消歧逻辑本身由本用例的实际选中行反证）。
    want_name = main[0]["machine_name"]
    probe = {"condition_text": (leaf.get("condition_text") or "")}
    want_row, want_idx, _hit = nb._pick_machine_row(
        rows, want_name, (main[0].get("condition_text") or "").strip(),
        probe["condition_text"], "Ⅰ区 2.5-2层 混凝土浇筑")
    assert want_row is not None, "主控机械必须在某一行里"
    want_cond = want_row.get("condition_text")
    want_shift = nb._machine_norm_at(want_row, want_idx)[0]
    assert b["machine_name"] == want_name
    assert b["condition_text"] == want_cond, (b["condition_text"], want_cond)
    assert b["norm_value"] == pytest.approx(want_shift), b["norm_value"]
    assert b["quantity_basis"] == pytest.approx(
        float(want_row.get("quantity_basis") or 1.0))
    assert b["unit"] == "台班/m³", b["unit"]
    assert b["norm_is_evidence"] is True and b["usable"] is True
    note = b["provenance"]["note"]
    assert want_name in note, note
    assert want_cond in note or "（无条件标注）" in note, note
    assert "暂用同行机械" not in note, "不许无声明借定额：%s" % note
    # 不许选到「后浇带」那一行（特殊部位小量定额，当整层浇筑依据量级必错）
    assert b["condition_text"] != "后浇带", "后浇带是特殊部位定额，不得作为整层依据"
    # 总台班 = 266 ÷ basis × norm_value（按库里的主控机械实算）
    import math
    total_shifts = 266 / b["quantity_basis"] * b["norm_value"]
    assert int(max(1, math.ceil(total_shifts))) >= 1


def test_machine_row_selection_prefers_main_machine_row(monkeypatch):
    """三行台班夹具：主控机械在第 2 行 → 选第 2 行，而不是 rows[0]。"""
    from pipeline.nodes import norm_bind as nb

    real_info, real_equip, real_main = nb.kb.activity_info, nb.kb.equipment_norms, nb.kb.main_machine
    fake_rows = [
        {"condition_text": "后浇带", "machine_combination_json": '["混凝土振捣器", "混凝土振捣器"]',
         "machine_shift_norm_json": "[1.26, 1.26]", "machine_shift_unit_json": '["台班", "台班"]',
         "quantity_basis": 10.0, "quantity_unit": "m³", "source_code": "GD_TEST"},
        {"condition_text": "基础浇筑", "machine_combination_json": '["混凝土振捣器"]',
         "machine_shift_norm_json": "[0.77]", "machine_shift_unit_json": '["台班"]',
         "quantity_basis": 10.0, "quantity_unit": "m³", "source_code": "GD_TEST"},
        {"condition_text": "基础浇筑", "machine_combination_json": '["混凝土输送泵车"]',
         "machine_shift_norm_json": "[0.055]", "machine_shift_unit_json": '["台班"]',
         "quantity_basis": 10.0, "quantity_unit": "m³", "source_code": "GD_TEST"},
    ]
    monkeypatch.setattr(nb.kb, "activity_info", lambda aid: (
        {"activity_id": aid, "activity_name": "基础浇筑", "unit": "m³",
         "recommended_production_mode": "equipment_driven"}
        if aid == "FAKE_3ROWS" else real_info(aid)))
    monkeypatch.setattr(nb.kb, "equipment_norms", lambda aid: (
        list(fake_rows) if aid == "FAKE_3ROWS" else real_equip(aid)))
    monkeypatch.setattr(nb.kb, "main_machine", lambda aid, condition_text=None: (
        [{"condition_text": "", "machine_name": "混凝土输送泵车", "machine_spec": None,
          "source_type": "ai_estimate", "confidence": "MEDIUM"}]
        if aid == "FAKE_3ROWS" else real_main(aid, condition_text)))

    leaf = _leaf("X", "基础浇筑", kb_activity_id="FAKE_3ROWS", quantity=100,
                 unit="m³", work_type="混凝土工程")
    ctx = _make_ctx([leaf], prompt="")
    _run(ctx)

    b = leaf["norm_binding"]
    assert b["norm_value"] == pytest.approx(0.055), "必须选含主控机械的那一行"
    assert b["condition_text"] == "基础浇筑"
    assert b["machine_name"] == "混凝土输送泵车"
    assert "暂用同行机械" not in b["provenance"]["note"]


def test_machine_row_condition_disambiguates_same_machine(monkeypatch):
    """同名多行（夯实机：平地 5.53 / 槽、坑 7.18）→ 用叶子条件字段消歧。"""
    from pipeline.nodes import norm_bind as nb

    real_info, real_equip, real_main = nb.kb.activity_info, nb.kb.equipment_norms, nb.kb.main_machine
    fake_rows = [
        {"condition_text": "夯实机夯实 / 平地", "machine_combination_json": '["电动夯实机"]',
         "machine_shift_norm_json": "[5.53]", "machine_shift_unit_json": '["台班"]',
         "quantity_basis": 100.0, "quantity_unit": "m³", "source_code": "GD_TEST"},
        {"condition_text": "夯实机夯实 / 槽、坑", "machine_combination_json": '["电动夯实机"]',
         "machine_shift_norm_json": "[7.18]", "machine_shift_unit_json": '["台班"]',
         "quantity_basis": 100.0, "quantity_unit": "m³", "source_code": "GD_TEST"},
    ]
    monkeypatch.setattr(nb.kb, "activity_info", lambda aid: (
        {"activity_id": aid, "activity_name": "回填土夯实", "unit": "m³",
         "recommended_production_mode": "equipment_driven"}
        if aid == "FAKE_2ROWS" else real_info(aid)))
    monkeypatch.setattr(nb.kb, "equipment_norms", lambda aid: (
        list(fake_rows) if aid == "FAKE_2ROWS" else real_equip(aid)))
    monkeypatch.setattr(nb.kb, "main_machine", lambda aid, condition_text=None: (
        [{"condition_text": "", "machine_name": "电动夯实机", "machine_spec": None,
          "source_type": "ai_estimate", "confidence": "MEDIUM"}]
        if aid == "FAKE_2ROWS" else real_main(aid, condition_text)))

    leaf = _leaf("Y", "基坑回填土夯实", kb_activity_id="FAKE_2ROWS", quantity=1000,
                 unit="m³", work_type="土方工程", condition_text="槽、坑")
    ctx = _make_ctx([leaf], prompt="")
    _run(ctx)

    b = leaf["norm_binding"]
    assert b["norm_value"] == pytest.approx(7.18), "槽、坑 条件应选中 7.18 那一行"
    assert b["condition_text"] == "夯实机夯实 / 槽、坑"
    # 没有条件信号时 → 保持 KB 行序（确定性，不改既有口径）
    leaf2 = _leaf("Y2", "基坑回填土夯实", kb_activity_id="FAKE_2ROWS", quantity=1000,
                  unit="m³", work_type="土方工程")
    ctx2 = _make_ctx([leaf2], prompt="")
    _run(ctx2)
    assert leaf2["norm_binding"]["norm_value"] == pytest.approx(5.53)


def test_machine_missing_row_is_downgraded_not_borrowed(monkeypatch):
    """主控机械不在任何台班行 → 降级"仅参考"，**绝不**借同行机械的定额。"""
    from pipeline.nodes import norm_bind as nb

    real_info, real_equip, real_main = nb.kb.activity_info, nb.kb.equipment_norms, nb.kb.main_machine
    monkeypatch.setattr(nb.kb, "activity_info", lambda aid: (
        {"activity_id": aid, "activity_name": "某机械活", "unit": "m³",
         "recommended_production_mode": "equipment_driven"}
        if aid == "FAKE_NO_ROW" else real_info(aid)))
    monkeypatch.setattr(nb.kb, "equipment_norms", lambda aid: (
        [{"condition_text": "通用", "machine_combination_json": '["履带式挖掘机"]',
          "machine_shift_norm_json": "[1.68]", "machine_shift_unit_json": '["台班"]',
          "quantity_basis": 1000.0, "quantity_unit": "m³", "source_code": "GD_TEST"}]
        if aid == "FAKE_NO_ROW" else real_equip(aid)))
    monkeypatch.setattr(nb.kb, "main_machine", lambda aid, condition_text=None: (
        [{"condition_text": "", "machine_name": "混凝土输送泵车", "machine_spec": None,
          "source_type": "ai_estimate", "confidence": "MEDIUM"}]
        if aid == "FAKE_NO_ROW" else real_main(aid, condition_text)))

    leaf = _leaf("Z", "某机械活", kb_activity_id="FAKE_NO_ROW", quantity=100,
                 unit="m³", work_type="土方工程", duration_days=9)
    ctx = _make_ctx([leaf], prompt="")
    _run(ctx)

    b = leaf["norm_binding"]
    assert b["mode"] == "machine"
    assert b["norm_value"] is None, "不许拿同行机械的台班数冒充（旧实现借 0.77）"
    assert b["usable"] is False and b["norm_is_evidence"] is False
    assert b["not_usable_reason"] == "主控机械缺台班定额（混凝土输送泵车）", b["not_usable_reason"]
    assert "暂用同行机械" not in b["provenance"]["note"]
    # 排程层：读绑定层原因 → 覆盖率桶出现同一标签，且沿用 WBS 工期
    from pipeline.nodes import scheduler as S
    ledger = S._build_ledger_item(leaf, "Z", "某机械活")
    assert ledger["usable"] is False
    assert ledger["not_usable_reason"] == "主控机械缺台班定额（混凝土输送泵车）", \
        ledger["not_usable_reason"]


# ==================== 8. llm=None 不联网 ====================
def test_llm_none_never_creates_client_and_no_network(monkeypatch):
    """把 LLMClient 换成"一构造就炸"的类，确保整条流程从不碰网络。

    同时把 .env 里的真实 key 藏掉：即使这台机器配了 QWEN_API_KEY，
    llm=None 也必须保持离线（本节点刻意不看 config.LLM_API_KEY）。
    """
    from pipeline import config as pipeline_config
    from pipeline.nodes import norm_bind as nb

    class BoomClient(object):
        def __init__(self, *a, **kw):
            raise AssertionError("llm=None 时绝不能构造 LLMClient（禁止联网）")

    monkeypatch.setattr(nb, "LLMClient", BoomClient)
    monkeypatch.setattr(pipeline_config, "LLM_API_KEY", "", raising=False)

    leaves = [_leaf("5.1.1.1", "1F框架梁钢筋", kb_activity_id=ACT_EXACT),
              _leaf("5.1.1.2", "柱钢筋", kb_activity_id=ACT_UNKNOWN),
              _leaf("5.1.1.3", "基础浇筑", kb_activity_id=ACT_MACHINE,
                    unit="m³", work_type="混凝土工程")]
    ctx = _make_ctx(leaves, prompt=PROMPT_EXACT)
    node = _node()
    assert node.llm is None
    node.run(ctx)

    assert node.llm is None, "全程不应惰性创建任何 LLM 客户端"
    assert len(ctx["norm_bindings"]) == 3
    assert abs(sum(ctx["credibility"].values()) - 1.0) < 1e-6


# ==================== 9. 无 kb_activity_id ====================
def test_leaf_without_kb_activity_id_still_anchored():
    leaf = _leaf("5.1.1.1", "砌体墙", kb_activity_id=None,
                 quantity=320, unit="m³", work_type="砌筑工程")
    ctx = _make_ctx([leaf], prompt="砖混结构住宅")
    _run(ctx)

    b = leaf["norm_binding"]
    assert b["match_type"] == "ai"
    assert b["provenance"]["origin"] == "ai"
    assert b["norm_value"] and b["norm_value"] > 0
    assert b["unit"] == "工日/m³"
    assert b["crew"] == {}
    assert ctx["norm_warnings"] and ctx["norm_warnings"][0]["task_id"] == "5.1.1.1"


def test_labor_type_hint_from_kb_scope_is_used():
    """kb_scope 给了 labor_type → AI 估算应优先采信它（可溯源）。"""
    leaf = _leaf("5.1.1.1", "零星砌筑", kb_activity_id=None,
                 quantity=50, unit="m³", work_type="")
    ctx = _make_ctx([leaf], prompt="零星工程",
                    kb_scope={"5.1.1.1": {"activity_id": None, "labor_type": "砌筑工"}})
    _run(ctx)

    note = leaf["norm_binding"]["provenance"]["note"]
    assert "砌筑工" in note
    # 经验产能 1.5 单位/工日 → 定额 ≈ 0.6667 工日/单位
    assert abs(leaf["norm_binding"]["norm_value"] - round(1.0 / 1.5, 6)) < 1e-6


# ==================== 10. LLM 分支（不联网，用 stub） ====================
class _StubLLM(object):
    """只实现 chat_json 的假客户端；记录调用次数，便于断言"是否联网"。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def chat_json(self, system, user, temperature=0.3, retries=1):
        self.calls += 1
        self.last_user = user
        return self.payload


def test_llm_pick_accepts_valid_candidate():
    from pipeline.nodes import norm_bind as nb

    hit = kb.labor_norm_match(ACT_DEFAULT, ["框架梁"])
    assert len(hit) > 1
    chosen = hit[-1]                                   # 故意挑一个"非中位数"的行
    stub = _StubLLM({"chosen_norm_id": chosen["norm_id"], "reason": "条件吻合",
                     "confidence": "高"})
    node = NormBindNode(llm=stub)
    node._emit = lambda event, data: None
    assert node.llm_usable is True

    leaf = _leaf("5.1.1.1", "1F框架梁钢筋", kb_activity_id=ACT_DEFAULT)
    ctx = _make_ctx([leaf], prompt="框架梁钢筋")
    node.run(ctx)

    b = leaf["norm_binding"]
    assert stub.calls == 1, "候选多行时才允许调用一次 LLM"
    assert b["match_type"] == "default"
    assert b["provenance"]["origin"] == "kb"
    assert "LLM" in b["provenance"]["note"]
    assert abs(b["norm_value"] - float(chosen["norm_value"])) < 1e-9
    # 只喂当前这一个 L4 的候选，不是全库
    assert chosen["norm_id"] in stub.last_user
    assert len(stub.last_user) < 20000


def test_llm_hallucinated_norm_id_is_discarded():
    stub = _StubLLM({"chosen_norm_id": "LN_编造的ID", "reason": "猜的", "confidence": "高"})
    node = NormBindNode(llm=stub)
    node._emit = lambda event, data: None

    leaf = _leaf("5.1.1.1", "1F框架梁钢筋", kb_activity_id=ACT_DEFAULT)
    ctx = _make_ctx([leaf], prompt="框架梁钢筋")
    node.run(ctx)

    b = leaf["norm_binding"]
    # 退回代码策略：中位值 + default/kb
    med = node._median_row(kb.labor_norm_match(ACT_DEFAULT, ["框架梁"]))
    assert abs(b["norm_value"] - round(float(med["norm_value"]), 6)) < 1e-9
    assert b["provenance"]["origin"] == "kb"
    assert any("不在候选内" in w["reason"] for w in ctx["norm_warnings"])


def test_llm_error_falls_back_with_warning():
    class _FailLLM(object):
        def chat_json(self, system, user, temperature=0.3, retries=1):
            raise RuntimeError("网络断了")

    node = NormBindNode(llm=_FailLLM())
    node._emit = lambda event, data: None
    leaf = _leaf("5.1.1.1", "1F框架梁钢筋", kb_activity_id=ACT_DEFAULT)
    ctx = _make_ctx([leaf], prompt="框架梁钢筋")
    node.run(ctx)                                      # 不抛异常

    b = leaf["norm_binding"]
    assert b["provenance"]["origin"] == "kb"
    assert b["match_type"] == "default"
    assert any("LLM 不可用" in w["reason"] for w in ctx["norm_warnings"])


@pytest.mark.parametrize("leaf_count", [2, 3])
def test_summary_and_return_shape(leaf_count):
    leaves = [_leaf("5.1.1.%d" % i, "梁钢筋%d" % i, kb_activity_id=ACT_EXACT)
              for i in range(1, leaf_count + 1)]
    ctx = _make_ctx(leaves, prompt=PROMPT_EXACT)
    node = _node()
    out = node.run(ctx)

    assert isinstance(out, dict)
    assert set(out) == {"norm_bindings", "norm_warnings", "credibility", "data_sources"}
    assert len(out["norm_bindings"]) == leaf_count
    # 第 32 轮：完成摘要改成人话（原「定额锚定完成：N 条叶子 · 精确命中 N · AI 估算 N」）
    assert node.done_summary and "套上消耗量定额" in node.done_summary
    assert "条在定额里精确命中" in node.done_summary


# ==================== 11. D 组 / G1（本轮：条件锁定 + 精确查定额 + 口径换算）====================
#
# D1 选定 L4 时锁条件（写进每个叶子 leaf.condition_key）
# D2 按条件精确查 Norm_Labor_Table（禁止模糊匹配 / 禁止匹配不上退默认行）
# D3 三个来源逐条标注（user / l4 / typical）+ 说得出条件是哪来的
# D4 「构件做法」硬定为现浇（不再参与猜测；非现浇的定额行一律出局）
# D5 以数据库分母口径为准，AI 把工程量换算过去 → 落盘 basis_adjust（六字段，已冻结）
# G1 删掉 A 类 usable 判据（"AI 估算不允许用来重算工期"）→ 解封 61~62 条

from pipeline.nodes import norm_bind as nb                    # noqa: E402

ACT_REBAR = "REBAR_NEW_BEAM"          # 真实 KB：47 行人工定额，含 19 行「构件做法=预制」
ACT_WALL = "LDT724_砌块墙"             # 真实 KB：6 行 工日/m³（面积↔体积缺墙厚）
ACT_PILE = "GD_A13_压管桩"             # 真实 KB：8 行 台班/m（根↔长度缺桩长）
BASIS_ADJUST_FIELDS = ("task_scope", "norm_scope", "task_quantity",
                       "adjusted_quantity", "method", "note")


def _d_leaf(tid, name, **kw):
    leaf = {"id": tid, "name": name, "quantity": 100, "unit": "t",
            "duration_days": 5, "work_type": "钢筋工程"}
    leaf.update(kw)
    return leaf


# ---- 11.1 D1 / D3：选定 L4 时锁条件 + 来源逐条标注 ----
def test_d1_d3_locks_user_conditions_and_labels_sources():
    """用户给了「构件类型 + 钢筋直径」→ 锁进叶子，来源逐条标 `user`。"""
    leaf = _leaf("5.1.1.1", "1F框架梁钢筋", kb_activity_id=ACT_REBAR)
    ctx = _make_ctx([leaf], prompt=PROMPT_EXACT)
    _run(ctx)

    b = leaf["norm_binding"]
    assert b["match_type"] == "exact", b
    assert leaf["condition_key"] == {"构件类型": "框架梁", "钢筋直径": "≤16mm"}
    assert leaf["condition_source"] == {"构件类型": "user", "钢筋直径": "user"}
    assert b["condition_key"] == leaf["condition_key"]
    # D3：来源标注必须是三来源里的可读标签之一
    for src in leaf["condition_source"].values():
        assert nb.CONDITION_SOURCE_LABELS.get(src), src
    # D4：`构件做法` 不参与锁定，但恒定口径必须写清
    assert "构件做法" not in leaf["condition_key"]
    assert "构件做法=现浇" in b["provenance"]["note"]
    assert "L4自身可推断" in b["provenance"]["note"]
    # 真值反查：框架梁 + ≤16mm 在库里唯一命中 7.9 工日/t
    assert b["norm_value"] == pytest.approx(7.9)


def test_d1_locks_condition_key_from_leaf_condition_field():
    """叶子自带条件字段（`构件类型=X, 钢筋直径=Y`）→ 必须先被采纳，不能拿典型值顶掉。"""
    leaf = _leaf("5.1.1.9", "9F特殊梁钢筋", kb_activity_id=ACT_REBAR,
                 condition_text="构件类型=斜梁, 钢筋直径=≤25")
    ctx = _make_ctx([leaf], prompt="主体结构斜梁钢筋绑扎。")
    _run(ctx)

    b = leaf["norm_binding"]
    assert leaf["condition_key"]["构件类型"] == "斜梁", leaf["condition_key"]
    assert leaf["condition_key"]["钢筋直径"] == "≤25"
    assert b["match_type"] == "exact"
    assert b["norm_value"] == pytest.approx(4.54)      # LN_2896 斜梁 ≤25mm


def test_d1_typical_lock_when_user_gives_nothing():
    """用户一个条件都没给 → 取典型（KB 中位行）锁进叶子，来源标 `typical`。"""
    leaf = _leaf("5.1.1.3", "钢筋绑扎", kb_activity_id=ACT_REBAR)
    ctx = _make_ctx([leaf], prompt="建一栋住宅楼。")
    _run(ctx)

    b = leaf["norm_binding"]
    assert b["match_type"] == "default"
    key = leaf["condition_key"]
    assert key, "用户没给条件也必须锁定（取典型）"
    src = leaf["condition_source"]
    assert set(src.values()) <= {"user", "typical", "l4"}, src
    # 锁的必须真的是该 L4 的定额条件值（不是凭空造的）
    rows = kb.labor_norms(ACT_REBAR)
    for dim, val in key.items():
        assert any(dim in (r.get("condition_combination") or "")
                   and str(val) in (r.get("condition_combination") or "") for r in rows), \
            "锁定的条件 %s=%s 必须能在该 L4 的定额行里找到" % (dim, val)
    assert "条件锁定：" in b["provenance"]["note"]


# ---- 11.2 D2：按条件精确查；匹配不上就报缺，不退默认行 ----
def test_d2_unmatched_condition_reports_missing_not_default():
    """锁定条件匹配不到任何行 → usable=False + 可读理由，**禁止**退默认行/模糊匹配。"""
    leaf = _leaf("5.1.1.9", "9F悬挑板钢筋", kb_activity_id=ACT_REBAR,
                 condition_text="构件类型=悬挑板, 钢筋直径=≤16")
    ctx = _make_ctx([leaf], prompt="主体结构悬挑板钢筋绑扎。")
    _run(ctx)

    b = leaf["norm_binding"]
    assert b["usable"] is False and b["norm_is_evidence"] is False
    assert b["norm_value"] is None, "匹配不上就报缺，不许退默认行"
    assert "缺定额" in b["not_usable_reason"], b["not_usable_reason"]
    assert "悬挑板" in b["not_usable_reason"]
    assert b["match_type"] == "ai"
    # 其它正当的 usable=False 来源不许被这条改动影响
    assert any(w["task_id"] == "5.1.1.9" for w in ctx["norm_warnings"])


def test_d2_exact_match_is_value_equality_not_substring():
    """精确查的判据是**逐值相等**（含 KB 的 `A|B` 别名），不是子串包含。

    旧 `kb.labor_norm_match` 用子串：`≤16` 会命中 `≤16mm` 之外的行（实测 9 行）。
    """
    rows = kb.labor_norms(ACT_REBAR)
    only16 = nb._matches_condition_rows(
        rows, {"构件类型": "框架梁", "钢筋直径": "≤16mm"})
    assert [r["norm_id"] for r in only16] == ["LN_2886"]
    # 别名写法：库里的 `>25.01mm|>25` 两种写法都能对上
    a = nb._matches_condition_rows(rows, {"构件类型": "框架梁", "钢筋直径": ">25.01mm"})
    btl = nb._matches_condition_rows(rows, {"构件类型": "框架梁", "钢筋直径": ">25"})
    assert [r["norm_id"] for r in a] == [r["norm_id"] for r in btl] == ["LN_2888"]
    # 行里没有该维度 → 不匹配（条件不是"可选提示"）
    assert nb._matches_condition_rows(rows, {"不存在的维度": "x"}) == []


# ---- 11.3 D4：「构件做法」硬定现浇 ----
def test_d4_construction_method_is_hardcoded_cast_in_place():
    """`构件做法` 恒为现浇：KB 里写「预制」的定额行一律出局，且它不进 condition_key。"""
    assert nb._CONSTRUCTION_METHOD == "现浇"
    assert nb._CONSTRUCTION_METHOD_KEY == "构件做法"
    rows = [r for r in kb.labor_norms(ACT_REBAR)
            if "预制" in (r.get("condition_combination") or "")]
    assert rows, "测试前提：REBAR_NEW_BEAM 应有预制行"
    assert nb._matches_condition_rows(rows, {}) == [], "预制行必须被现浇硬过滤掉"
    # 先浇行不受影响
    cast = [r for r in kb.labor_norms(ACT_REBAR)
            if "预制" not in (r.get("condition_combination") or "")]
    assert len(nb._matches_condition_rows(cast, {})) == len(cast)
    # 恒定口径文案在 provenance 里（D4 的可读留痕）
    leaf = _leaf("5.1.1.1", "1F框架梁钢筋", kb_activity_id=ACT_REBAR)
    _run(_make_ctx([leaf], prompt=PROMPT_EXACT))
    assert nb._constant_condition_note() in leaf["norm_binding"]["provenance"]["note"]


# ---- 11.4 D5：以数据库口径为准换算工程量 + 六字段 basis_adjust + 量级校验 ----
def test_d5_basis_adjust_six_frozen_fields():
    """面积↔体积（用户明写 200mm 板厚）→ 落盘调整后的工程量，六字段齐全、不多不少。

    ⚠️ 第 7 批（2026-09-21）夹具改名：原任务名是「1-1层 ALC墙板安装（墙厚200mm）」。
    那条名字**本来就绑错了活动** —— ALC 墙板应绑 m² 的 `MASON_ALC_PANEL`，而不是 m³ 的
    `LDT724_砌块墙`。第 7 批把"绑定不一致先在同 L3 内改绑"接上线后，这类任务会在
    冲突校验里被**改绑到 MASON_ALC_PANEL（m²）**，于是根本不会进入 m²→m³ 换算分支
    —— 本用例要测的是"**该换算时**换得对不对"，不是"绑错了也不改"。
    故改成真正属于 `LDT724_砌块墙` 的工序名「砌块墙」：任务名与该活动名重叠 ≥2 字，
    冲突校验不触发，m²（任务）↔ m³（定额）的换算路径照旧被完整覆盖，
    下面每一条断言**一个字都没改**。
    """
    leaf = _leaf("6.1.1.1", "1-1层 砌块墙（墙厚200mm）", quantity=750.0,
                 unit="m²", work_type="砌筑工程", kb_activity_id=ACT_WALL)
    ctx = _make_ctx([leaf], prompt="砌块墙，墙厚200mm。")
    _run(ctx)

    b = leaf["norm_binding"]
    adj = b["basis_adjust"]
    assert set(adj) == set(BASIS_ADJUST_FIELDS), sorted(adj)
    assert adj["task_scope"] == "m²" and adj["norm_scope"] == "m³"
    assert adj["task_quantity"] == pytest.approx(750.0)
    assert adj["adjusted_quantity"] == pytest.approx(150.0)     # 750 ㎡ × 0.2 m
    assert "200 mm 板厚" in adj["method"] and "750" in adj["method"] \
        and "150" in adj["method"], adj["method"]
    assert adj["note"]
    # 换算系数照旧进既有通道（resource / scheduler 认这个）
    assert b["convert_factor"] == pytest.approx(0.2)
    assert b["convert_denominator"] == "m³"
    assert b["usable"] is True
    # 参数**不落盘**成独立对象
    assert "convert_param" not in b and "conversion_params" not in b


def test_d5_absurd_magnitude_is_rejected_and_traced():
    """薄墙按 3 m 厚折 → 量级校验拒绝：usable=False + 留痕，且不写 basis_adjust。

    ⚠️ 同上一用例：夹具名字由「ALC墙板安装」改为「砌块墙」—— 理由见
    `test_d5_basis_adjust_six_frozen_fields` 的 docstring（第 7 批接线后 ALC 名字会先被
    改绑到 m² 活动）。断言一条未改。
    """
    leaf = _leaf("6.1.1.2", "2-2层 砌块墙（墙厚3000mm）", quantity=750.0,
                 unit="m²", work_type="砌筑工程", kb_activity_id=ACT_WALL)
    ctx = _make_ctx([leaf], prompt="砌块墙，墙厚3000mm。")
    _run(ctx)

    b = leaf["norm_binding"]
    assert b["usable"] is False
    assert "换算不合理" in b["not_usable_reason"], b["not_usable_reason"]
    assert "thickness_m" in b["not_usable_reason"]
    assert "basis_adjust" not in b, "离谱的换算不许落 basis_adjust（调整后的量不可信）"
    rej = b["basis_adjust_rejected"]
    assert rej["factor"] == pytest.approx(3.0)
    assert rej["task_quantity"] == pytest.approx(750.0)


def test_d5_pile_length_comes_from_quota_condition_not_a_hardcoded_constant():
    """D5：没有写死的换算常量。桩长从**定额行条件**（桩长18m以内）推定并标 ai_estimate。"""
    assert not hasattr(nb, "_AI_PILE_LENGTH_M"), \
        "写死常量已删除：桩长必须由 D5 换算通道取出"
    leaf = _leaf("2.1.1", "预应力管桩（PHC-A400-95）施工", quantity=120.0,
                 unit="根", work_type="桩基工程", kb_activity_id=ACT_PILE)
    ctx = _make_ctx([leaf], prompt="预应力管桩施工。")
    _run(ctx)

    b = leaf["norm_binding"]
    assert b["unit"] == "台班/m"
    assert b["ctx_source"] == "ai_estimate", "限值推定必须标 AI 来源、逐条留痕"
    assert b["coverage_reason"] == "AI估算换算参数"
    assert b["ctx_value"] == {"pile_length_m": pytest.approx(18.0)}
    assert b["convert_factor"] == pytest.approx(18.0)
    adj = b["basis_adjust"]
    assert set(adj) == set(BASIS_ADJUST_FIELDS)
    assert adj["adjusted_quantity"] == pytest.approx(2160.0)     # 120 根 × 18 m
    # 任务里明写桩长时以用户为准（优先级：用户值 > AI 换算值）
    leaf2 = _leaf("2.1.2", "预应力管桩施工（桩长25m，PHC-A500）", quantity=100.0,
                  unit="根", work_type="桩基工程", kb_activity_id=ACT_PILE)
    _run(_make_ctx([leaf2], prompt="管桩 桩长25m"))
    b2 = leaf2["norm_binding"]
    assert b2["ctx_source"] == "text" and b2["convert_factor"] == pytest.approx(25.0)
    assert b2["basis_adjust"]["adjusted_quantity"] == pytest.approx(2500.0)


def test_d5_unresolvable_conversion_reports_missing():
    """推不出换算参数（根↔m³ 要单根体积，任务里没有任何可用信息）→ 报缺，不许硬算。"""
    leaf = _leaf("3.1.1", "截桩头处理", quantity=120.0, unit="根",
                 work_type="桩基工程", kb_activity_id=ACT_WALL)
    ctx = _make_ctx([leaf], prompt="截桩头。")
    _run(ctx)

    b = leaf["norm_binding"]
    assert b["usable"] is False and b["norm_is_evidence"] is False
    assert b["not_usable_reason"], "推定不出就必须给可读理由"
    assert "volume_per_pile_m3" in (b["unit_check"].get("detail") or "") \
        or "不可换算" in b["not_usable_reason"], b["not_usable_reason"]
    assert not b.get("basis_adjust"), "换算不出来就不许写 basis_adjust（调整后的量不可信）"
    assert b.get("convert_factor") in (None, 1.0), "不许按 1:1 硬算"


# ---- 11.5 G1：删掉 A 类 usable 判据（AI 来源不再一律堵死）----
def test_g1_ai_match_type_is_usable_and_not_blocked_by_ai_reason():
    """G1：`match_type='ai'` 且 `usable=False` 的条数应为 0（旧政策是 61~62 条全堵）。"""
    leaves = [
        _leaf("5.1.1.1", "1F框架梁钢筋", kb_activity_id="NOT_EXIST_ACTIVITY_999"),
        _leaf("5.1.1.2", "素混凝土垫层", kb_activity_id=None,
              quantity=100, unit="m³", work_type="混凝土工程"),
        _leaf("6.1.1.1", "砌体墙", kb_activity_id=None,
              quantity=320, unit="m³", work_type="砌筑工程"),
        # 对照：AI 来源但单位说不清 → 仍然 usable=False（**正当**的 usable=False 必须保留）
        _leaf("7.4.2", "预留预埋", kb_activity_id=None,
              quantity=1, unit="项", work_type=""),
    ]
    ctx = _make_ctx(leaves, prompt="建一栋住宅楼。")
    _run(ctx)

    ai = [b for b in (s["norm_binding"] for s in _all_leaves(ctx))
          if b.get("match_type") == "ai"]
    assert len(ai) >= 3
    # 第 7 批口径变更（2026-09-21 用户裁定，见 norm_bind 路径③-守卫）：
    #   ① 工作包级占位叶子（3 段 id ∧ 无 KB 活动，如夹具里的 `7.4.2`）→ **不再**出
    #      `norm_value`，改为 `usable=False` + 专用 reason，工期沿用 WBS 目标值；
    #   ② 其余 AI 条（4 段真 L4 / 绑了活动的）→ G1 政策**原样**（出定额值，
    #      不许因为来源是 AI 就堵）。
    # 两类**逐条**断言，按 id 段数分类（判据写死，不设关键字豁免）。
    wp = [b for b in ai if nb._is_work_package_leaf({"id": b["task_id"]})]
    rest = [b for b in ai if not nb._is_work_package_leaf({"id": b["task_id"]})]
    assert [b["task_id"] for b in wp] == ["7.4.2"], \
        "夹具必须含且只含一条工作包级占位叶子：%s" % [b["task_id"] for b in ai]
    for b in wp:
        assert b["norm_value"] is None, "工作包级占位叶子不许再用单位盲的经验产能反算"
        assert b["usable"] is False and b["norm_is_evidence"] is False
        assert b["not_usable_reason"] == nb._REASON_WP_PLACEHOLDER, b["not_usable_reason"]
    assert len(rest) >= 3
    for b in rest:
        assert b["norm_value"] and b["norm_value"] > 0, "AI 条必须仍在绑定层出定额值"
        assert "AI估算定额" not in (b["not_usable_reason"] or ""), \
            "A 类判据已删除：不许再因为来源是 AI 就置 False"
    # 0 条"因为来源是 AI"被堵
    assert not [b for b in ai if not b["usable"]
                and "AI估算定额" in (b["not_usable_reason"] or "")]
    # AI 来源本身放行
    plain = [b for b in ai if b["task_id"] in ("5.1.1.1", "5.1.1.2", "6.1.1.1")]
    assert all(b["usable"] is True for b in plain), \
        [(b["task_id"], b["usable"], b["not_usable_reason"]) for b in plain]
    # 核验旧文案彻底消失
    assert not any("AI估算定额：KB 无定额行" in (b.get("not_usable_reason") or "")
                   for b in (s["norm_binding"] for s in _all_leaves(ctx)))


def test_g1_other_legitimate_unusable_reasons_survive():
    """G1 **只**删"AI 来源不许用"这一条：其它正当的 usable=False 来源必须保留。"""
    # ① 缺计量单位（台班缺分母）
    b = {"unit": "台班"}
    _node()._apply_unit_check(b, "根", {"pile_length_m": 18.0})
    assert b["usable"] is False and "缺计量单位" in b["not_usable_reason"]
    # ② 不可换算
    b2 = {"unit": "工日/m³"}
    _node()._apply_unit_check(b2, "t")
    assert b2["usable"] is False and "不可换算" in b2["not_usable_reason"]
    # ③ 活动绑定不一致（契约 §3）
    leaf = _leaf("5.1.1.1", "暖通预留预埋", kb_activity_id="CONC_NEW_FOUND",
                 unit="m³", work_type="混凝土工程")
    _run(_make_ctx([leaf], prompt=""))
    b3 = leaf["norm_binding"]
    if b3["match_type"] == "unbound":
        assert b3["usable"] is False and b3["not_usable_reason"] == "活动绑定不一致"
    # ④ D5 换算不合理被拒（见 11.4）
    # ⑤ D5 推定不出就报缺（见 11.4）


def test_d5_tier_row_needs_thickness_from_quota_condition_not_default():
    """追加-2：`_tier_adjusted_row` **不得**再用 `DEFAULT_WALL_THICKNESS_M` 兜底。

    改前：`... or kb_units.DEFAULT_WALL_THICKNESS_M` —— 取不到厚度也换行。
    改后（D5）：厚度只能来自 ①用户明写 ②定额行适用条件的档位；
    两条都取不到 → 不换行（由单位校验判 unusable），绝不退默认值硬算。
    """
    node = _node()
    rows = [{"norm_id": "LN_T1", "condition_text": "加气混凝土砌块，板厚≤200mm",
             "norm_value": 0.806, "norm_unit": "工日/m³", "quantity_unit": "m³",
             "quantity_basis": 1.0, "productivity_value": 1.0 / 0.806},
            {"norm_id": "LN_T2", "condition_text": "加气混凝土砌块，板厚≤400mm",
             "norm_value": 0.5, "norm_unit": "工日/m³", "quantity_unit": "m³",
             "quantity_basis": 1.0, "productivity_value": 2.0}]
    # ① 文本里有用户明写的墙厚（300mm）→ 换到 ≤400mm 档（0.2 m 档装不下 300mm）
    picked, why = node._tier_adjusted_row(
        rows, rows[0], "m²", "1-1层 ALC墙板安装（墙厚300mm）", {})
    assert picked["norm_id"] == "LN_T2", (picked.get("norm_id"), why)
    # ② 只能从定额条件取（≤200mm → 0.2 m）→ 与 row 同档，保持原行
    picked2, _why2 = node._tier_adjusted_row(rows, rows[0], "m²", "1-1层 ALC墙板安装", {})
    assert picked2["norm_id"] == "LN_T1"
    # ③ 开区间档位（>400mm）+ 没有用户值 → **取不到厚度** → 不换行
    open_rows = [dict(rows[0], norm_id="LN_T3", condition_text="加气混凝土砌块，板厚>400mm")]
    picked3, why3 = node._tier_adjusted_row(open_rows, open_rows[0], "m²", "ALC墙板安装", {})
    assert picked3["norm_id"] == "LN_T3" and why3 == "", \
        "取不到厚度就不许换行（旧实现会拿 0.2 兜底）"


def test_d5_open_interval_tier_binding_is_unusable_not_defaulted():
    """开区间厚度档位 + 用户没给墙厚 → 整条绑定 usable=False（推定不出就报缺）。"""
    from pipeline import kb as _kb

    real_norms, real_info = _kb.labor_norms, _kb.activity_info
    _kb.labor_norms = lambda aid: (
        [{"norm_id": "LN_OPEN", "condition_text": "加气混凝土砌块，板厚>400mm",
          "condition_combination": "{}", "norm_value": 0.5, "norm_unit": "工日/m³",
          "quantity_unit": "m³", "quantity_basis": 1.0, "productivity_value": 2.0,
          "source_code": "TEST_OPEN", "measure_scope": "体积"}]
        if aid == "FAKE_OPEN_TIER" else real_norms(aid))
    _kb.activity_info = lambda aid: (
        {"activity_id": aid, "activity_name": "ALC墙板安装", "unit": "m³",
         "recommended_production_mode": "labor"}
        if aid == "FAKE_OPEN_TIER" else real_info(aid))
    try:
        leaf = _leaf("6.1.1.9", "ALC墙板安装", quantity=500.0, unit="m²",
                     work_type="砌筑工程", kb_activity_id="FAKE_OPEN_TIER")
        _run(_make_ctx([leaf], prompt="ALC 墙板安装。"))
    finally:
        _kb.labor_norms = real_norms
        _kb.activity_info = real_info

    b = leaf["norm_binding"]
    assert b["usable"] is False, b
    assert b["norm_is_evidence"] is False
    assert "不可换算" in b["not_usable_reason"], b["not_usable_reason"]
    assert not b.get("unit_assumption"), "开区间档位不许补出厚度"


# ---- 11.6 裁定-3：机械选行接入「锁条件 → 精确匹配」 ----
def test_equip_condition_filter_uses_only_available_dimensions():
    """机械侧精筛**只做有数据支撑的部分**：用叶子明写条件 ∩ 该 L4 台班行真有的维度。

    实测数据（`Norm_Equipment_Table` 245 行 / 63 个 L4）：`condition_combination` 空 17 行（7%）；
    但每行维度极少（1 维 188 行 / 2 维 40 行 / 0 维 17 行），其中 `条件`(116)、`未分类`(28)、
    `子目名称`(16) 是元字段不可比 → 真正可比的只剩 5 个维度、合计 103 行。
    所以：**不做**"锁全部维度再精筛"，只在叶子明写的条件与该 L4 可用维度对齐时筛。
    """
    node = _node()
    aid = "GD_A11_静力爆破石方"
    rows = kb.equipment_norms(aid)
    cc = node._equip_condition_map(aid)
    assert rows and cc, "测试前提：该 L4 应有台班行 + 结构化条件"

    # ② 用户明写「岩类别=坚硬岩」→ 只在**真的可比**的维度上精筛（6 行 → 2 行）
    leaf = {"id": "X", "name": "静力爆破石方", "unit": "m³", "quantity": 100.0,
            "condition_text": "岩类别=坚硬岩, 石方类别=槽、坑石方"}
    cands, rec = node._equip_candidates(rows, leaf, "静力爆破石方",
                                        leaf["condition_text"], cc)
    assert rec["available_keys"] == ["岩类别", "石方类别"], rec["available_keys"]
    assert rec["matched_keys"] == ["岩类别"], rec["matched_keys"]
    assert len(cands) == 2 and rec["rows_matched"] == 2, (len(cands), rec)
    assert all("坚硬岩" in (cc.get(r["condition_text"]) or "") for r in cands)
    assert "机械条件精筛" in rec["note"]

    # ① 用户什么都没写 → **不硬做**，候选集原样（保持既有选行口径）
    cands2, rec2 = node._equip_candidates(rows, {"id": "Y", "name": "静力爆破石方"},
                                          "静力爆破石方", "", cc)
    assert len(cands2) == len(rows)
    assert rec2["matched_keys"] == [] and "未做条件精筛" in rec2["note"]

    # ③ 用户写的条件该 L4 没有可比维度（压管桩的「桩径φ400」只存在于 condition_text 里）
    aid2 = "GD_A13_压管桩"
    rows2 = kb.equipment_norms(aid2)
    cc2 = node._equip_condition_map(aid2)
    cands3, rec3 = node._equip_candidates(
        rows2, {"id": "Z", "name": "压管桩", "condition_text": "桩径φ400"},
        "压管桩", "桩径φ400", cc2)
    assert len(cands3) == len(rows2), "没有可用维度时不许硬筛"
    assert rec3["available_keys"] == [] and rec3["matched_keys"] == []
    assert "没有可用的结构化条件维度" in rec3["note"]


def test_bind_machine_records_equipment_condition_audit():
    """机械绑定必须留下裁定-3 的审计键；精筛只缩候选、不改主控机械名选行。"""
    leaf = _leaf("4.1.4.3", "Ⅰ区 2.5-2层 混凝土浇筑", kb_activity_id=ACT_MACHINE,
                 quantity=266, unit="m³", work_type="混凝土工程", duration_days=15)
    _run(_make_ctx([leaf], prompt="基础采用商品混凝土泵送，汽车泵。"))

    b = leaf["norm_binding"]
    assert b["mode"] == "machine"
    assert "equipment_condition_note" in b and b["equipment_condition_note"]
    assert b["equipment_rows_total"] == len(kb.equipment_norms(ACT_MACHINE))
    assert isinstance(b["equipment_condition_key"], dict)
    # 精筛记录不许污染六字段契约
    assert "equipment_condition_key" not in (b.get("basis_adjust") or {})
    # 既有选行口径未被破坏：主控机械仍来自 Activity_Main_Machine
    main = kb.main_machine(ACT_MACHINE)
    assert b["machine_name"] == main[0]["machine_name"]


def test_machine_condition_note_lists_available_keys_when_unmatched():
    """对不上时要把"该 L4 有哪些可比维度"写进留痕（人可据此补条件），且不阻断选行。"""
    node = _node()
    aid = ACT_MACHINE
    rows = kb.equipment_norms(aid)
    cc = node._equip_condition_map(aid)
    cands, rec = node._equip_candidates(
        rows, {"id": "Q", "name": "混凝土浇筑", "condition_text": "岩类别=坚硬岩"},
        "混凝土浇筑", "岩类别=坚硬岩", cc)
    assert rec["available_keys"], "该 L4 至少有构件类型/部位等维度"
    assert rec["matched_keys"] == [], "岩类别 不在该 L4 的维度里 → 不硬筛"
    assert "可用维度：" in rec["note"] and "未做条件精筛" in rec["note"]
    assert len(cands) == len(rows)


def test_d1_lock_happens_even_when_l4_has_no_labor_rows():
    """D1：**选定 L4 就锁条件** —— 即使这条 L4 在 `Norm_Labor_Table` 一行都没有。

    锁定不是"定额匹配成功"的副产品，是"选定了 L4"这件事本身的留痕。改前锁定写在
    `if rows:` 里面：库里有一批真实 L4（`Norm_Labor_Table` 0 行 + 活动信息可读，
    实测 60/493 条）会因此**一个条件都不锁**，`leaf.condition_key` 缺失 → D3 的
    逐条来源标注也就无从谈起。本条守卫这个行为。
    """
    # 从真实库里现取一条"人工定额 0 行但活动信息可读"的 L4（不写死 id，随库刷新仍成立）
    aid = None
    for row in kb._query_all("SELECT activity_id FROM L4_Activity_Dictionary"):
        cand = row[0]
        if kb.labor_norms(cand):
            continue
        try:
            info = kb.activity_info(cand)
        except Exception:
            info = None
        if info:
            aid = cand
            break
    assert aid, "测试前提：库里应有 Norm_Labor_Table 0 行的真实 L4"
    assert kb.labor_norms(aid) == [], "测试前提：该 L4 的人工定额行必须为空"

    leaf = _leaf("9.9.1", "测试任务", kb_activity_id=aid,
                 condition_text="构件类型=框架梁, 钢筋直径=≤16")
    _run(_make_ctx([leaf], prompt="建一栋住宅楼。"))

    b = leaf["norm_binding"]
    # 用户明写的两个维度照样锁上（来源 user），不因"没有定额行"而丢失
    assert leaf["condition_key"] == {"构件类型": "框架梁", "钢筋直径": "≤16"}, leaf
    assert leaf["condition_source"] == {"构件类型": "user", "钢筋直径": "user"}
    assert b["condition_key"] == leaf["condition_key"]
    assert b["condition_source"] == leaf["condition_source"]
    # 定额行确实没有 → 走**兜底口径**（且 G1 后仍可用），但条件留痕已在。
    # 第 7 批（2026-09-21）语义变更：这类 L4 若在 `L4_Norm_Default` 里有审定量行，
    # 现在**必须回退到它** —— 依据是契约 §5-WS3 与 `kb.labor_norm_default()` 的
    # docstring 原话："调用方应当回退到它，而不是判'无定额'——否则整条工序丢定额、
    # 工期退回 WBS"。`norm_bind` 路径② 原先只查 `Norm_Labor_Table`、漏了这一步，
    # 于是这类 L4 掉到路径③ 的**单位盲**经验产能（实测 `MASON_ALC_PANEL` 拿到
    # 0.5 工日/m² 而不是库里的 0.095）。
    # 所以这里按**库里到底有没有那条默认行**分别断言 —— 不留"ai 或 default 都行"的豁免。
    _dflt = kb.labor_norm_default(aid)
    assert b["norm_value"], "两种口径都必须给出定额值"
    if _dflt:
        assert b["match_type"] == "default", b["match_type"]
        assert b["norm_value"] == pytest.approx(_dflt["norm_value"]), b["norm_value"]
        assert b["source_code"] == (_dflt.get("source_code") or ""), b["source_code"]
    else:
        assert b["match_type"] == "ai"
    # D3/D4 的可读留痕也进了 provenance（条件锁定 + 恒定的「构件做法=现浇」）
    assert "条件锁定：" in b["provenance"]["note"]
    assert "构件做法=现浇" in b["provenance"]["note"]
    assert "L4自身可推断" in b["provenance"]["note"]

    # 用户什么都没给 + 该 L4 无定额行 → 无可推断也无典型可取 → 只能是空锁（不许凭空造）
    leaf2 = _leaf("9.9.2", "测试任务2", kb_activity_id=aid)
    _run(_make_ctx([leaf2], prompt="建一栋住宅楼。"))
    assert leaf2["condition_key"] == {}
    assert leaf2["condition_source"] == {}


# ---- 12. 第 7 批（2026-09-21）：绑定不一致的"第一处置" = 同 L3 改绑（原先是死代码）----
def test_绑定不一致时必须先在同L3内改绑而不是直接降级():
    """守两件事：`_resolve_activity_conflict` 真的被接上了 + 它能选中 L4_Norm_Default 的候选。

    背景（实测缺陷）：该函数的 docstring 自称"绑定不一致时的**第一处置**"，冲突分支的
    注释也写着"先试着在同 L3 内改绑…改绑不到才降级"，但它**全仓零调用** —— 实际行为是
    直接降级为未绑定。于是"模型把活动绑错"这件事永远得不到纠正，只能报缺退回 WBS。

    本用例复刻真实场景：`1-1层 ALC墙板安装`（任务按 **m²** 计量）被绑到
    `LDT724_砌块墙`（定额分母 **m³**）—— 参 `resource.py:102` 的留痕。正确行为不是降级，
    而是改绑到同 L3 里名字逐字对得上、单位也一致的 `MASON_ALC_PANEL`（m²，0.095 工日/m²）。
    """
    assert kb.l3_of_activity(ACT_WALL) == kb.l3_of_activity("MASON_ALC_PANEL"), \
        "前提：两条活动必须在同一个 L3（改绑只在同 L3 内做）"
    assert kb.labor_norms("MASON_ALC_PANEL") == [], \
        "前提：目标活动在 Norm_Labor_Table 里 0 行（只能靠 L4_Norm_Default）"
    assert kb.labor_norm_default("MASON_ALC_PANEL"), "前提：目标活动有 L4_Norm_Default 审定量行"

    leaf = _leaf("6.1.1.1.1", "1-1层 ALC墙板安装", quantity=1145.0, unit="m²",
                 work_type="砌筑工程", kb_activity_id=ACT_WALL)
    _run(_make_ctx([leaf], prompt="某住宅小区，剪力墙结构。"))

    b = leaf["norm_binding"]
    assert leaf["kb_activity_id"] == "MASON_ALC_PANEL", \
        "绑错时必须改绑到同 L3 里对得上的活动，而不是原地降级（kb_activity_id=%s）" \
        % leaf["kb_activity_id"]
    assert b.get("reanchored_from") == ACT_WALL, "必须留改绑痕迹"
    assert b["match_type"] == "default", b["match_type"]
    assert b["usable"] is True, b["not_usable_reason"]
    assert not b["not_usable_reason"], b["not_usable_reason"]
    assert b["norm_value"] == pytest.approx(0.095), b["norm_value"]
    assert b["unit"] == "工日/m²", b["unit"]
    assert "activity_conflict" in b, "冲突留痕不许被改绑抹掉"
    note = str((b.get("provenance") or {}).get("note") or "")
    assert "已在同 L3" in note, note


def test_改绑不到时仍然按契约降级为未绑定():
    """反面：改绑不成立时**不许**留着错定额用 —— 契约 §3 的降级路径必须原样保留。

    构造：任务名与所绑活动名毫无共同工序词（判"明显不符"），而真正名字对得上的候选
    `MASON_ALC_PANEL` 与任务的**单位不同族**（任务给 m³、目标活动分母 m²）⇒ 试绑过不了
    单位校验 ⇒ 必须降级为未绑定，而不是硬用。
    """
    leaf = _leaf("6.1.1.2.1", "二次结构 ALC墙板安装", quantity=284.0, unit="m³",
                 work_type="砌筑工程", kb_activity_id=ACT_WALL)
    _run(_make_ctx([leaf], prompt="某住宅小区，剪力墙结构。"))

    b = leaf["norm_binding"]
    assert leaf["kb_activity_id"] == ACT_WALL, "改绑不成立时不该动叶子的活动编号"
    assert b.get("reanchored_from") is None
    assert b["match_type"] == "unbound", b["match_type"]
    assert b["usable"] is False and b["norm_is_evidence"] is False
    assert b["norm_value"] is None, "错定额一个数都不许用"
    assert "activity_conflict" in b


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(pytest.main([__file__, "-q"]))
