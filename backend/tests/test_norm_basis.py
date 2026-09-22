# -*- coding: utf-8 -*-
"""第 37 轮口径回归：产能 = 1/norm_value；单位校验唯一真源 = kb_units.check_unit_pair。

本文件**改写**自旧版（旧版断言的是错的 `productivity = quantity_basis / norm_value`，
元凶脚本 `devtools/fix_norm_basis.py` 当年就是按那个公式写库的）。现口径：

  · `labor_norm_value` 入库时**已经归一**（留档不变式
    `raw_value / raw_quantity_basis == labor_norm_value`）→ 产能 `= 1 / norm_value`；
    `raw_quantity_basis`（兼容别名 `quantity_basis`）只作溯源，**不参与乘法**。
  · 单位校验一律走 `kb_units.check_unit_pair`（双向 + 默认拒绝）：分母缺失 / 不可换算
    → `unusable`，绑定层写 `not_usable_reason` + `norm_is_evidence=False`。
  · 机械台班的分母**只能**来自 KB 行的 `quantity_unit`；缺分母 → "缺计量单位"。
  · 换算参数 ctx 的取值顺序：文本显式数字 → boundary_conditions.materials →
    AI 行业默认（标 `ctx_source='ai_estimate'`，覆盖率单列 "AI估算换算参数"）→ unusable。

运行：python -m pytest tests/test_norm_basis.py -q
"""

import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline import kb          # noqa: E402
from pipeline import kb_units    # noqa: E402
from pipeline.nodes import scheduler as S  # noqa: E402
from pipeline.nodes.norm_bind import NormBindNode  # noqa: E402

KB = ROOT / "BuildPlan_KB" / "kb.db"


def _con():
    return sqlite3.connect(str(KB))


def _node():
    node = NormBindNode(llm=None)
    node._emit = lambda *a, **k: None
    return node


# ---------------- 1. 数据侧：产能列必须是 1/norm，basis 只作溯源 ----------------
def test_KB产能列等于定额值的倒数():
    """全表不变量：productivity_value == 1/labor_norm_value（basis 不参与）。"""
    con = _con()
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(Norm_Labor_Table)")]
        assert "raw_quantity_basis" in cols, "列未改名：quantity_basis → raw_quantity_basis"
        bad = con.execute(
            "SELECT COUNT(*) FROM Norm_Labor_Table "
            "WHERE labor_norm_value > 0 AND productivity_value IS NOT NULL "
            "AND ABS(productivity_value - 1.0/labor_norm_value) > 1e-9").fetchone()[0]
        assert bad == 0, "有 %d 行的 productivity_value != 1/labor_norm_value" % bad
    finally:
        con.close()


def test_KB里确实存在basis不等于1的定额():
    """防止测试变成空转：库里必须真有 basis≠1 的样本（旧口径的受害者）。"""
    con = _con()
    try:
        n = con.execute("SELECT COUNT(*) FROM Norm_Labor_Table "
                        "WHERE raw_quantity_basis > 1").fetchone()[0]
    finally:
        con.close()
    assert n > 100, "basis≠1 的定额只有 %d 条，样本太少" % n


def test_KB定额单位分母与quantity_unit一致且已归一():
    """`labor_norm_unit` 必须是已归一的规范串（scale=1），且分母 == quantity_unit。

    绑定层的单位校验完全建立在这条不变量上：分母 == 工程量单位 → verdict=same。
    """
    con = _con()
    try:
        rows = con.execute(
            "SELECT norm_id, labor_norm_unit, quantity_unit FROM Norm_Labor_Table "
            "WHERE labor_norm_unit IS NOT NULL AND labor_norm_unit != ''").fetchall()
    finally:
        con.close()
    assert rows, "没有可校验的定额单位行"
    bad = []
    for norm_id, nu, qu in rows:
        p = kb_units.parse_norm_unit(nu)
        if p["scale"] != 1.0 or not p["denominator"] \
                or p["denominator"] != kb_units.normalize_unit(qu):
            bad.append((norm_id, nu, qu))
    assert not bad, "定额单位未归一/分母与 quantity_unit 不一致，前 3 条：%s" % bad[:3]


def test_产能公式就是倒数():
    assert kb_units.productivity_of(0.025) == pytest.approx(40.0)
    assert kb_units.productivity_of(0.025) != pytest.approx(400.0)      # basis/norm
    assert kb_units.productivity_of(0.025) != pytest.approx(0.0025)     # norm/basis
    # 带 raw_quantity_basis 也不改口径（只作交叉校验）
    assert kb_units.productivity_of(0.025, 10) == pytest.approx(40.0)


# ---------------- 2. 绑定层：写进 binding 的产能 = 1/norm（P0-1 收口点） ----------------
def _row(norm_value=0.204, basis=10.0, quantity_unit="m²", norm_unit="工日/m²"):
    return {"norm_id": "LN_TEST", "condition_text": "测试行",
            "norm_value": norm_value, "norm_unit": norm_unit,
            "quantity_unit": quantity_unit, "quantity_basis": basis,
            "raw_quantity_basis": basis, "productivity_value": 1.0 / norm_value,
            "source_code": "TEST_BASIS_001"}


def test_绑定层写入的产能等于1除定额值():
    """铝模实例 LN_3853：norm=0.025、basis=10 → 正确产能 40（不是 400）。"""
    b = {}
    _node()._fill_from_labor_row(b, _row(norm_value=0.025), "exact", "kb", "高",
                                 "测试", leaf_unit="m²")
    assert b["productivity_value"] == pytest.approx(40.0)
    assert b["productivity_value"] != pytest.approx(400.0)
    # quantity_basis 保留（兼容下游）但只是溯源值
    assert b["quantity_basis"] == 10.0
    # 单位一致 → 可作依据
    assert b["norm_is_evidence"] is True and b["unit_check"]["verdict"] == "same"


def test_绑定层不用quantity_basis做乘法():
    """AST 级守卫的轻量版：写库产能与 basis 无关（basis=1 与 basis=10 同值）。"""
    b1, b2 = {}, {}
    node = _node()
    node._fill_from_labor_row(b1, _row(norm_value=0.204, basis=1.0), "exact", "kb", "高",
                              "测试", leaf_unit="m²")
    node._fill_from_labor_row(b2, _row(norm_value=0.204, basis=10.0), "exact", "kb", "高",
                              "测试", leaf_unit="m²")
    assert b1["productivity_value"] == b2["productivity_value"] == pytest.approx(1 / 0.204)


# ---------------- 3. 单位校验：唯一真源 kb_units.check_unit_pair ----------------
def test_台班单位分母只来自KB():
    from pipeline.nodes.norm_bind import _shift_unit_from_kb

    assert _shift_unit_from_kb("台班", "m³") == "台班/m³"
    assert _shift_unit_from_kb("台班", "m3") == "台班/m³"
    assert _shift_unit_from_kb("台班", "") == ""      # 缺分母 → 不可用
    assert _shift_unit_from_kb(None, None) == ""


def test_单位校验三态与降级标记():
    node = _node()
    # same
    b = {"unit": "工日/m³"}
    chk = node._apply_unit_check(b, "m³")
    assert chk["verdict"] == "same" and b["norm_is_evidence"] is True
    assert b["convert_factor"] == 1.0
    # convertible：根 → 台班/m，桩长 18m/根 → factor 18
    b = {"unit": "台班/m"}
    chk = node._apply_unit_check(b, "根", {"pile_length_m": 18.0})
    assert chk["verdict"] == "convertible" and chk["factor"] == pytest.approx(18.0)
    assert b["convert_factor"] == pytest.approx(18.0) and b["norm_is_evidence"] is True
    # unusable：裸「台班」缺分母 → "缺计量单位" + 非证据
    b = {"unit": "台班"}
    chk = node._apply_unit_check(b, "根", {"pile_length_m": 18.0})
    assert chk["verdict"] == "unusable"
    assert b["norm_is_evidence"] is False and b["usable"] is False
    assert "缺计量单位" in b["not_usable_reason"], b["not_usable_reason"]
    # unusable：不可换算（t vs 工日/m³，没有容重）
    b = {"unit": "工日/m³"}
    assert node._apply_unit_check(b, "t")["verdict"] == "unusable"


# ---------------- 4. 端到端：土方量级基线（契约 §5-WS3②④） ----------------
def _run_leaf(leaf, prompt="", params=None, boundary=None):
    node = _node()
    ctx = {"wbs": {"phases": [{"phase": "P", "work_packages": [
        {"id": "1.1", "name": "P", "sub_packages": [leaf]}]}]},
        "prompt": prompt, "extracted_params": params or {}}
    if boundary is not None:
        ctx["boundary_conditions"] = boundary
    node.run(ctx)
    return leaf["norm_binding"], ctx


def test_基坑土方开挖4260立方米不得按人工定额定工期():
    """契约 §5-WS3④：基坑土方开挖 + 4260 m³ + EARTH0032 → 改绑机械，绝不用 0.827 工日/m³。"""
    leaf = {"id": "2.2.1", "name": "基坑土方开挖", "quantity": 4260.0, "unit": "m³",
            "duration_days": 15, "work_type": "土方工程", "kb_activity_id": "EARTH0032"}
    b, _ctx = _run_leaf(leaf, prompt="某项目基坑土方开挖 4260 m³")

    assert b["mode"] == "machine", b
    assert leaf["kb_activity_id"] != "EARTH0032", "必须按量级基线改绑机械活动"
    assert b["quantity_band"] == "high"
    assert b["norm_value"] != pytest.approx(0.827), "不得沿用人工挖土定额"
    assert not str(b["unit"]).startswith("工日"), b["unit"]
    assert "台班" in str(b["unit"])
    # 机械口径：2160? 不——这里是 4260 m³ / 1000 × 1.68 台班
    assert b["norm_is_evidence"] is True and b["usable"] is True


def test_小工程量走人工口径():
    """<50 m³ → 人工（量级基线覆盖任务名里的机械词）。"""
    leaf = {"id": "9.4.1", "name": "机械挖基坑土方（小坑）", "quantity": 30.0, "unit": "m³",
            "duration_days": 3, "work_type": "土方工程",
            "kb_activity_id": "GD_A11_机械挖土方、淤泥流砂"}
    b, _ctx = _run_leaf(leaf)
    assert b["quantity_band"] == "low"
    assert b["mode"] == "labor", b
    assert str(b["unit"]).startswith("工日"), b["unit"]


def test_中间量级机械人工两行都绑():
    """50~500 m³ → 机械 + 人工两行都绑（机械为主，人工侧只算修边量）。

    ⚠️ 第 41 轮更新（契约 §3）：本用例原先绑 `EARTH0030 挖地槽（沟）`，而任务名是
    「室外给排水管网开挖」—— 两者最长连续共同片段只有 1，同 L3 里却有
    `EARTH0008 人工扩眼沟槽开挖`（共同片段 2）。按新加的绑定一致性校验，这属于
    "明显不符且存在更像的替代"，会先改绑、改绑不到就降级未绑定 —— 那样就没有
    `quantity_band` 可断言了。这里把夹具改成**语义正确的绑定**，用例要验证的
    "中间量级两行都绑"这条行为本身没有改动。
    """
    leaf = {"id": "9.1.1", "name": "室外给排水管网开挖", "quantity": 350.0, "unit": "m³",
            "duration_days": 15, "work_type": "土方工程",
            "kb_activity_id": "EARTH0008"}
    b, _ctx = _run_leaf(leaf)
    assert b["quantity_band"] == "mid", b
    assert b["mode"] == "machine", "中间量级以机械为主"
    assert "台班" in str(b["unit"]), b["unit"]
    side = b.get("dual_binding") or {}
    assert side, "中间量级必须把人工侧也绑上"
    assert side["role"] == "edge_trim" and side["mode"] == "labor"
    assert side["norm_value"] and str(side["unit"]).startswith("工日")
    # 人工侧来自原活动（EARTH0008）的定额行，可溯源
    assert side["labor_activity_id"] == "EARTH0008"


def test_量级基线改绑不到就降级且不进工期计算(monkeypatch):
    """改绑不到机械活动 → 定额降级"仅参考"，绝不进入工期计算。"""
    leaf = {"id": "2.2.1", "name": "基坑土方开挖", "quantity": 4260.0, "unit": "m³",
            "duration_days": 18, "work_type": "土方工程", "kb_activity_id": "EARTH0032"}
    real = kb.production_method_baseline
    monkeypatch.setattr(
        kb, "production_method_baseline",
        lambda l3: ({"default_mode": "machine", "qty_threshold_high": 500.0,
                     "qty_threshold_low": 50.0, "machine_activity_hint": "NOT_EXIST_HINT",
                     "notes": "", "source_type": "contract_rule", "confidence": "MEDIUM"}
                    if l3 == "earthwork" else real(l3)))
    b, _ctx = _run_leaf(leaf)
    assert b["mode"] == "labor"
    assert b["norm_is_evidence"] is False and b["usable"] is False
    assert b.get("method_conflict"), "必须写冲突标记（scheduler 的证据门读它）"
    assert "量级基线" in b["not_usable_reason"], b["not_usable_reason"]

    # 排程层：沿用 WBS 工期 18 天、覆盖率单列「定额口径不符」、bound=0
    wbs = {"phases": [{"phase": "土方", "work_packages": [{
        "id": "3.2", "name": "土方", "sub_packages": [leaf]}]}]}
    out = S.compute_schedules(wbs, [], {}, {}, None)
    ver = (out.get("schedule_versions") or {}).get("resource_ok") or {}
    rows = {str(r["task_id"]): r for r in (ver.get("schedule") or [])}
    got = int(rows["2.2.1"]["ef"]) - int(rows["2.2.1"]["es"])
    assert got == 18, "降级后必须沿用 WBS 工期，实际 %d 天" % got


# ---------------- 5. 换算 ctx：文本 → 材料 → AI 默认 → unusable ----------------
def test_桩基价格换算因子18打通2160米():
    """契约锚点：120 根 PHC 桩 × 18 m/根 = 2160 m → 10.6 台班 → ≥8 天（WS4 验收前提）。

    型号 PHC-A400-95 里给不出桩长 → 走 AI 行业默认 18 m，必须标 ctx_source='ai_estimate'
    并进 by_reason='AI估算换算参数'。
    """
    leaf = {"id": "2.1.1", "name": "预应力管桩（PHC-A400-95）施工", "quantity": 120.0,
            "unit": "根", "duration_days": 30, "work_type": "桩基工程",
            "kb_activity_id": "GD_A13_压管桩"}
    b, _ctx = _run_leaf(leaf, boundary={"materials": [
        {"name": "预应力管桩", "total_quantity": 120, "unit": "根"}]})

    assert b["mode"] == "machine", b
    assert b["unit"] == "台班/m", b["unit"]
    assert b["unit_check"]["verdict"] == "convertible"
    assert b["convert_factor"] == pytest.approx(18.0)
    assert b["ctx_source"] == "ai_estimate"
    assert b["ctx_value"] == {"pile_length_m": 18.0}
    assert b["coverage_reason"] == "AI估算换算参数"
    # 2160 m → 总台班 → ≥ 8 天（台数 ≥1）
    total_m = leaf["quantity"] * b["convert_factor"]
    assert total_m == pytest.approx(2160.0)
    total_shifts = total_m / b["quantity_basis"] * b["norm_value"]
    assert total_shifts == pytest.approx(10.6, abs=0.1)
    import math
    assert int(max(1, math.ceil(total_shifts / 1))) >= 8


def test_文本显式桩长优先且不算AI():
    leaf = {"id": "2.1.1", "name": "预应力管桩施工（桩长25m，PHC-A500）", "quantity": 100.0,
            "unit": "根", "duration_days": 30, "work_type": "桩基工程",
            "kb_activity_id": "GD_A13_压管桩"}
    b, _ctx = _run_leaf(leaf, prompt="管桩 桩长25m")
    assert b["ctx_source"] == "text"
    assert b["ctx_value"] == {"pile_length_m": 25.0}
    assert b["convert_factor"] == pytest.approx(25.0)
    assert "coverage_reason" not in b, "参数来自文本，不该标 AI 换算参数"


def test_换算参数推不出来就unusable(monkeypatch):
    """ctx 无法推断 → unusable（默认拒绝），不得按 1:1 瞎猜。"""
    real_info = kb.activity_info
    real_equip = kb.equipment_norms
    real_main = kb.main_machine
    monkeypatch.setattr(kb, "activity_info", lambda aid: (
        {"activity_id": aid, "activity_name": "某工序", "unit": "m³",
         "recommended_production_mode": "equipment_driven"}
        if aid == "FAKE_NOLEN" else real_info(aid)))
    monkeypatch.setattr(kb, "equipment_norms", lambda aid: (
        [{"condition_text": "通用", "machine_combination_json": '["某机械"]',
          "machine_spec_json": '["x"]', "machine_shift_norm_json": "[0.9]",
          "machine_shift_unit_json": '["台班"]', "quantity_basis": 100.0,
          "quantity_unit": "m³", "source_code": "GD_TEST"}]
        if aid == "FAKE_NOLEN" else real_equip(aid)))
    monkeypatch.setattr(kb, "main_machine", lambda aid, condition_text=None: (
        [{"condition_text": "", "machine_name": "某机械", "machine_spec": None,
          "source_type": "ai_estimate", "confidence": "LOW"}]
        if aid == "FAKE_NOLEN" else real_main(aid, condition_text)))

    leaf = {"id": "X", "name": "某工序", "quantity": 10.0, "unit": "根",
            "duration_days": 5, "work_type": "土方工程", "kb_activity_id": "FAKE_NOLEN"}
    b, _ctx = _run_leaf(leaf)
    assert b["unit"] == "台班/m³"
    assert b["norm_is_evidence"] is False and b["usable"] is False
    assert "单位不可用" in b["not_usable_reason"], b["not_usable_reason"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok  %s" % name)
    print("全部通过")
