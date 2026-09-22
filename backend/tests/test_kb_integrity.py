# -*- coding: utf-8 -*-
"""第 37 轮 WS2 · KB 数据层与 kb.py 接口的完整性门禁（直连 kb.db，无 LLM）。

运行：cd backend && python -m pytest tests/test_kb.py tests/test_kb_conformance.py tests/test_kb_integrity.py -q

## 本文件守的是什么（每条断言都对应一个真实缺陷，不是形式检查）

第 37 轮的根因：`Norm_Labor_Table.productivity_value` 有 1030+ 行是按
`raw_quantity_basis / labor_norm_value` 写进去的，而 `labor_norm_value` **入库时
就已经归一**（= raw_value / raw_quantity_basis）。于是所有 basis≠1 的行产能偏大
basis 倍（1/10/100/1000），工日需求被缩小同样倍数。本文件把正确口径钉死：

  ① `productivity_value == 1 / labor_norm_value`（DB 全量）；
  ② `raw_value / raw_quantity_basis == labor_norm_value`（留档不变式，说明归一发生在入库阶段）；
  ③ `kb.labor_norms()` 暴露 `raw_quantity_basis` + 废弃别名 `quantity_basis`（同值），
     且 `norm_unit` 归一、`productivity_value` 为 1/norm；
  ④ 单位写法只有一种：`quantity_unit` / `L4.unit` / `Norm_Equipment_Table.quantity_unit`
     都已是规范写法（'m3' → 'm³'），`labor_norm_unit` 的批量分母（10/100/1000）已折进 norm_value；
  ⑤ **域 1.6（第 6 批）已删 `Workface_Capacity_Rule` 表，连同 `kb.workface_capacity()`
     函数一并删除**（调用会 `AttributeError`）；本节原有 4 条断言该函数返回值的用例
     随之整条删除。容量改由**叶子自带** `workface_capacity` 字典提供，KB 不再补齐
     （见 `scheduler.resolve_workface` / `resource._resolve_workface`），不在本文件覆盖；
  ⑥ `kb.production_method_baseline()` 给出量级阈值与机械活动提示；
  ⑦ 表缺失 / DB 不可读时，所有接口**优雅降级且绝不抛异常**（契约硬要求）。

合表前这里是"读 v2 表 + 读旧表取并集"；后来旧两表改成只读归档
（`Workface_Capacity_Rule_legacy_v1` / `_legacy_v2`）。**H2/H3（2026-09-21）
已把这两张归档表删除**（连同 H1/H4/H5/H6 的 4 张废表，见本文件 ④b）：现在
运行时与库里都没有任何 `Workface%` 容量表，"逐列对拍"的证据已随
归档件一并移除，改为直接断言"归档表不存在"。

`production_method_baseline` 的降级测试要造一个"缺表 DB"，所以把真 `kb.db` 复制到
`backend/_test_tmp/`（**不用 tmp_path / tempfile.mkdtemp**：本仓库沙箱下
tempfile 建目录会 WinError 5，见契约 §测试口径）。
"""

import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline import config, kb, kb_units  # noqa: E402

DB = Path(config.KB_DB_PATH)
TMP_ROOT = BACKEND / "_test_tmp" / ("p%d" % os.getpid())

EPS = 1e-9

#: H2/H3（2026-09-21）：原两张归档表（`_legacy_v1` / `_legacy_v2`）已删除，
#: `_DEP_BANNER_RE` / `_strip_dep_banner` 随之失去存在理由（归档件没了，
#: 没有"带废弃横幅的原文"可比对）。
_H_GROUP_DROPPED_TABLES = (
    "Unit_Conversion",                     # H1
    "Workface_Capacity_Rule_legacy_v1",    # H2
    "Workface_Capacity_Rule_legacy_v2",    # H3
    "Norm_Adjustment",                     # H4
    "Norm_Adjustment_Target",              # H5
    "L4_Labor_Type_Override",              # H6
)


# ==================== 夹具 ====================
@pytest.fixture(autouse=True)
def _clean_kb_cache():
    """每个用例前后都清 kb 的进程内缓存。

    为什么必须：`kb._KB_CACHE` 是模块级全局；本文件里换了 `config.KB_DB_PATH`
    （降级用例）之后，前一个用例查到的旧值会留在缓存里串味 —— 那是测试脚手架的
    问题，不是被测代码的问题，所以在这里统一清掉。
    """
    kb.clear_cache()
    yield
    kb.clear_cache()


@pytest.fixture(scope="module")
def con():
    """只读连接真 kb.db（模块级复用；测试里不写库）。"""
    if not DB.exists():
        pytest.skip("kb.db 不存在：%s" % DB)
    c = sqlite3.connect(str(DB))
    yield c
    c.close()


def _numeric(text):
    """TEXT → float；解析不出返回 None。"""
    if text is None:
        return None
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


@pytest.fixture(scope="function")
def db_without_new_tables():
    """把真 kb.db 复制一份并删掉 pmb 表 → 供"迁移未跑"的降级测试用。

    （域 1.6 已删 `Workface_Capacity_Rule`，故只对 `Production_Method_Baseline`
    再断降级；那条 `DROP ... Workface_Capacity_Rule` 是 `IF EXISTS`，已无表可删。）

    作用域必须是 `function`**不能是 `module`**：本夹具会改全局 `config.KB_DB_PATH`，
    而 module 作用域的 finalizer 会被推迟到模块里更晚的时刻才执行 —— 实测它会在
    后一个不依赖本夹具的用例跑完之后才恢复路径，于是那个用例读到了"缺表副本"
    拿到 None 挂掉。用 function 作用域，请求它的用例一结束就恢复。
    """
    if not DB.exists():
        pytest.skip("kb.db 不存在：%s" % DB)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / "kb_no_v3.db"
    if path.exists():
        path.unlink()
    shutil.copy2(str(DB), str(path))
    assert path.stat().st_size == DB.stat().st_size, "复制产物大小与源不一致"
    c = sqlite3.connect(str(path))
    c.execute("DROP TABLE IF EXISTS Workface_Capacity_Rule")
    c.execute("DROP TABLE IF EXISTS Production_Method_Baseline")
    c.commit()
    c.close()
    old = config.KB_DB_PATH
    config.KB_DB_PATH = Path(path)
    kb.clear_cache()
    try:
        yield path
    finally:
        config.KB_DB_PATH = old
        kb.clear_cache()


# ==================== ① 产能口径：1 / labor_norm_value ====================
def test_productivity_is_reciprocal_of_norm_value(con):
    """全量：productivity_value 必须等于 1/labor_norm_value（basis 不参与）。

    这是本轮的**核心回归门**。旧口径 `basis/norm_value` 会让本断言在
    basis≠1 的行上直接爆掉（例如 LN_256 旧值 571.4286 vs 正确 57.1429）。
    """
    rows = con.execute(
        "SELECT norm_id, activity_id, labor_norm_value, productivity_value "
        "FROM Norm_Labor_Table WHERE labor_norm_value IS NOT NULL AND labor_norm_value > 0"
    ).fetchall()
    assert rows, "前置条件：Norm_Labor_Table 应有 labor_norm_value > 0 的行"

    bad = []
    for nid, aid, nv, pv in rows:
        if pv is None or abs(float(pv) - 1.0 / float(nv)) > EPS:
            bad.append((nid, aid, nv, pv))
    assert not bad, "productivity_value != 1/labor_norm_value 的行：%s" % bad[:5]


def test_norm_value_is_raw_over_basis(con):
    """全量：raw_value / raw_quantity_basis == labor_norm_value（归一发生在入库阶段）。

    这条不变式是"为什么正确产能是取倒数"的证据：书页的「工日/10m²」已经被
    折成 norm_value，「10」只留在 raw_quantity_basis 溯源列里。若哪天有人在
    入库侧改了归一方式，这条会先炸，而不是等到 1085 行产能静默错掉。
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(Norm_Labor_Table)")}
    assert "raw_quantity_basis" in cols, \
        "第 37 轮后应有 raw_quantity_basis 列（旧名 quantity_basis 已废弃）：%s" % sorted(cols)

    rows = con.execute(
        "SELECT norm_id, raw_value, raw_quantity_basis, labor_norm_value "
        "FROM Norm_Labor_Table").fetchall()
    checked, skipped, bad = 0, 0, []
    for nid, raw, basis, nv in rows:
        f, b, n = _numeric(raw), _numeric(basis), _numeric(nv)
        if f is None or b in (None, 0) or n is None:
            skipped += 1
            continue
        checked += 1
        if abs(f / b - n) > EPS:
            bad.append((nid, f, b, f / b, n))
    assert checked > 3800, "前置条件：应有 ~3877 行可校验，实测 %d" % checked
    assert not bad, "raw_value/basis != labor_norm_value 的行：%s（跳过 %d）" % (bad[:5], skipped)


def test_deprecated_alias_column_matches_canonical_basis(con):
    """`quantity_basis`（只读兼容别名）必须逐行等于 `raw_quantity_basis`。

    为什么留这一列：`tests/test_norm_basis.py` 的 SQL 直接写 `quantity_basis`，
    而契约把该文件列为**严禁修改**；留一个同值别名列，那条测试才能跑到断言
    （它断言的旧口径与第 37 轮相反，仍会失败，但那是口径冲突，不是"列不存在"）。
    别名不得成为第二真源：`kb.labor_norms` 只读 `raw_quantity_basis`，
    且**禁止**下游拿它当乘数（那正是第 37 轮修掉的 bug）。
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(Norm_Labor_Table)")}
    if "quantity_basis" not in cols:
        pytest.skip("未保留只读兼容别名列 quantity_basis（可选）")
    mism = con.execute(
        "SELECT COUNT(*) FROM Norm_Labor_Table WHERE "
        "IFNULL(raw_quantity_basis, -999) IS NOT IFNULL(quantity_basis, -999)"
    ).fetchone()[0]
    assert mism == 0, "%d 行的 quantity_basis != raw_quantity_basis" % mism


# ==================== ② 单位写法唯一 ====================
def test_units_are_canonical_everywhere(con):
    """`quantity_unit` / `L4.unit` / 机械 `quantity_unit` 都已是规范写法。"""
    bad = []
    for nid, unit in con.execute("SELECT norm_id, quantity_unit FROM Norm_Labor_Table"):
        if kb_units.normalize_unit(unit) != (unit or ""):
            bad.append(("Norm_Labor_Table", nid, unit))
    for aid, unit in con.execute("SELECT activity_id, unit FROM L4_Activity_Dictionary"):
        if kb_units.normalize_unit(unit) != (unit or ""):
            bad.append(("L4_Activity_Dictionary", aid, unit))
    for nid, unit in con.execute("SELECT norm_id, quantity_unit FROM Norm_Equipment_Table"):
        if kb_units.normalize_unit(unit) != (unit or ""):
            bad.append(("Norm_Equipment_Table", nid, unit))
    assert not bad, "残留 'm3'/'m2' 写法的行：%s" % bad[:5]


def test_labor_norm_unit_has_no_batch_denominator(con):
    """`labor_norm_unit` 的 scale 必须是 1：批量分母属于 raw_quantity_basis，不属于定额单位。"""
    bad = []
    for nid, unit, qu in con.execute(
            "SELECT norm_id, labor_norm_unit, quantity_unit FROM Norm_Labor_Table"):
        parsed = kb_units.parse_norm_unit(unit)
        if parsed["scale"] != 1.0:
            bad.append((nid, unit, parsed["scale"]))
        if parsed["denominator"] != kb_units.normalize_unit(qu):
            bad.append((nid, unit, parsed["denominator"], qu))
    assert not bad, "labor_norm_unit 批量分母/分母不匹配：%s" % bad[:5]


def test_equipment_shift_unit_json_is_literal_taiban(con):
    """机械台班单位 JSON 统一成字面「台班」，且与数值数组等长。"""
    import json
    bad = []
    for nid, nj, uj in con.execute(
            "SELECT norm_id, machine_shift_norm_json, machine_shift_unit_json "
            "FROM Norm_Equipment_Table"):
        try:
            norms, units = json.loads(nj or "[]"), json.loads(uj or "[]")
        except ValueError as exc:
            bad.append((nid, "JSON 解析失败", str(exc)))
            continue
        if len(norms) != len(units):
            bad.append((nid, "长度不一致", len(norms), len(units)))
        if any(u != "台班" for u in units):
            bad.append((nid, "单位非字面台班", units))
    assert not bad, "机械台班单位异常：%s" % bad[:5]


# ==================== ③ kb.labor_norms 接口 ====================
def test_labor_norms_exposes_raw_basis_and_deprecated_alias():
    rows = kb.labor_norms("EARTH0034")   # 挖路槽（厚度≤100mm）：basis=10、norm=0.0175
    assert rows, "前置条件：EARTH0034 应有人工定额"
    r = rows[0]
    for key in ("raw_quantity_basis", "quantity_basis", "quantity_unit",
                "norm_value", "norm_unit", "productivity_value", "productivity_unit"):
        assert key in r, "labor_norms 缺键 %s：%s" % (key, sorted(r))
    # 废弃别名与正式键同值（旧调用方不炸，但语义已改为"仅溯源"）
    assert r["quantity_basis"] == r["raw_quantity_basis"]
    # 批量基数留在溯源列里，定额单位的分母已是规范单位
    assert r["raw_quantity_basis"] == 10.0, r["raw_quantity_basis"]
    assert r["norm_unit"] == "工日/m²", r["norm_unit"]
    assert r["quantity_unit"] == "m²", r["quantity_unit"]
    # 产能 = 1/norm（若按旧的 basis/norm 口径，这里会是 571.4286）
    assert abs(r["productivity_value"] - 1.0 / r["norm_value"]) < 1e-9
    assert abs(r["productivity_value"] - 57.142857142857142) < 1e-6
    assert r["productivity_unit"] == "m²/工日", r["productivity_unit"]


def test_labor_norms_productivity_falls_back_without_touching_db(monkeypatch):
    """库里 productivity_value 为 NULL/<=0 时，用 kb_units.productivity_of 现场兜底。"""
    real = kb._query_all

    def fake(sql, params=()):
        rows = real(sql, params)
        if params and params[0] == "EARTH0034" and "FROM Norm_Labor_Table" in sql:
            # 把 (productivity_value, productivity_unit) 两列打空，模拟"库里没算"
            return [tuple(list(r[:7]) + [None, None] + list(r[9:])) for r in rows]
        return rows

    monkeypatch.setattr(kb, "_query_all", fake)
    rows = kb.labor_norms("EARTH0034")
    assert rows, "前置条件：EARTH0034 应有人工定额"
    assert all(r["productivity_value"] is not None for r in rows), \
        "productivity_value 为空时必须兜底成 1/norm_value"
    assert abs(rows[0]["productivity_value"] - 1.0 / rows[0]["norm_value"]) < 1e-9


def test_labor_norms_unknown_activity_is_empty_list():
    assert kb.labor_norms("NO_SUCH_ACTIVITY_XYZ") == []
    assert kb.labor_norms(None) == []


# ==================== ④ 域 1.6：容量表与 kb.workface_capacity() 已删除 ====================
# 迁移测试说明（第 6 批）：本节原有 4 条用例（唯一表公式键 / 兼容键非 crew_max 别名 /
# 旧机械列仍存在 / 未知活动返回 None）断的都是 `kb.workface_capacity(...)` 的返回值。
# 该函数已随 `Workface_Capacity_Rule` 表一起删除，任何调用都会 `AttributeError`，
# 用例无论怎么改写都只剩"函数不存在"这一件事，故**整条删除**。


# ==================== ④b 唯一表 + H 组废表已删（"数值逐位不变"的旧证据已移除） ====================
def test_legacy_tables_are_dropped(con):
    """H2/H3（2026-09-21）：两张归档表已删除；域 1.6 连主表也删了。"""
    names = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Workface%' "
        "ORDER BY name")]
    assert names == [], "域 1.6 已删 Workface_Capacity_Rule，Workface% 表应为空：%s" % names
    assert not con.execute(
        "SELECT 1 FROM sqlite_master WHERE name='Workface_Capacity_Rule_v2'").fetchone(), \
        "Workface_Capacity_Rule_v2 不该存在"
    for t in ("Workface_Capacity_Rule_legacy_v1", "Workface_Capacity_Rule_legacy_v2"):
        assert not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone(), \
            "%s 应已按 H2/H3 删除" % t


def test_h_group_dropped_tables_absent(con):
    """H1–H6：6 张废表必须**不存在**（旧版这里是"归档表逐列对拍"，归档件已删）。"""
    present = [t for t in _H_GROUP_DROPPED_TABLES if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()]
    assert not present, "以下废表应已按 H1–H6 删除：%s" % present
    # 反向守卫：这两张**不许**删（D 组要用 / 402 条未裁决待办）
    # 域 1.6 已删 Workface_Capacity_Rule，不再守卫
    for keep in ("Condition_Dictionary", "data_quality_log"):
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (keep,)).fetchone(), \
            "%s 不该被删" % keep


# ==================== ④c 机械侧六列：域 1.6 已删表 ====================
def test_machine_side_table_deleted(con):
    """域 1.6 已删 Workface_Capacity_Rule 表，机械侧六列校验随之退役。"""
    has_table = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='Workface_Capacity_Rule'"
    ).fetchone()
    assert has_table is None, "域 1.6 已删 Workface_Capacity_Rule 表"


def test_machine_coverage_and_constant_crew_are_written_down(con):
    """域 1.6 已删 Workface_Capacity_Rule 表，覆盖率校验随之退役。"""
    has_table = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='Workface_Capacity_Rule'"
    ).fetchone()
    assert has_table is None, "域 1.6 已删表，本测试退役"


# 迁移测试说明（第 6 批）：本节点原有 3 条用例
#   test_workface_capacity_exposes_machine_columns（机械侧六列）
#   test_constant_capacity_units_have_step_n_zero（常量容量单位 step_n=0）
#   test_machine_rows_are_deep_copy_safe（缓存返回深拷贝）
# 全部依赖已删除的 `kb.workface_capacity()` → **整条删除**。


# ==================== ⑤ production_method_baseline ====================
def test_production_method_baseline_earthwork():
    b = kb.production_method_baseline("earthwork")
    assert b is not None, "前置条件：earthwork 应已标定"
    for key in ("default_mode", "qty_threshold_high", "qty_threshold_low",
                "machine_activity_hint", "notes", "source_type", "confidence"):
        assert key in b, "production_method_baseline 缺键 %s：%s" % (key, sorted(b))
    assert b["default_mode"] == "machine"
    assert b["qty_threshold_high"] == 500.0 and b["qty_threshold_low"] == 50.0
    # 机械活动提示必须真的存在于 L4，且确实是机械活动 —— 否则下游改绑必炸
    hint = b["machine_activity_hint"]
    assert hint, "earthwork 必须给机械活动提示"
    info = kb.activity_info(hint)
    assert info is not None, "machine_activity_hint=%r 不在 L4_Activity_Dictionary" % hint
    assert info["recommended_production_mode"] == "equipment_driven", info
    assert kb.l3_of_activity(hint) == "earthwork", info


def test_production_method_baseline_unknown_is_none():
    assert kb.production_method_baseline("NO_SUCH_L3") is None
    assert kb.production_method_baseline(None) is None
    assert kb.production_method_baseline("") is None


# ==================== ⑥ 优雅降级：表缺失时绝不抛异常 ====================
def test_degrade_without_pmb_table(db_without_new_tables):
    """`Production_Method_Baseline` 表不存在（迁移未跑 / 旧 DB）→ 返回 None 不抛异常。

    域 1.6（第 6 批）已删 `Workface_Capacity_Rule` 与 `kb.workface_capacity()`，
    本用例原先对它的两条断言随之删除；夹具保留"复制真库 + 删表"的做法，
    改断 pmb 的降级契约。字段缺列/DB 坏掉两种更脏的情况见下面两个用例。
    """
    assert kb.production_method_baseline("earthwork") is None


def test_degrade_when_db_missing(monkeypatch):
    """DB 路径不存在 → 所有接口返回空/None，绝不抛异常。"""
    monkeypatch.setattr(config, "KB_DB_PATH", Path(TMP_ROOT) / "definitely_missing.db")
    kb.clear_cache()
    try:
        assert kb.production_method_baseline("earthwork") is None
        assert kb.labor_norms("EARTH0034") == []
        assert kb.activity_info("REBAR_NEW_FOUND") is None
    finally:
        kb.clear_cache()


def test_degrade_when_db_is_garbage():
    """DB 文件存在但不是 sqlite（或表被改坏）→ 查询异常也必须被吞掉。"""
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    junk = TMP_ROOT / "kb_garbage.db"
    junk.write_bytes(b"this is not a sqlite database at all\n" * 10)
    old = config.KB_DB_PATH
    config.KB_DB_PATH = Path(junk)
    kb.clear_cache()
    try:
        assert kb.production_method_baseline("earthwork") is None
        assert kb.labor_norms("EARTH0034") == []
    finally:
        config.KB_DB_PATH = old
        kb.clear_cache()


# 迁移测试说明（第 6 批）：⑦「缓存不得串味」原有的
# `test_memoized_capacity_returns_deep_copy` 只断言 `kb.workface_capacity()` 的
# 深拷贝语义，函数已删除 → **整条删除**（该节因此清空）。
