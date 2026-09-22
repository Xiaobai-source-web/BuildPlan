# -*- coding: utf-8 -*-
"""第 40 轮 · 面积↔体积（墙厚）与「写法归一」的单位口径。

这批叶子实测绑到了**真实定额**、却因为量纲对不上被判 unusable：
`6.1.1.1~6.1.18.1 ALC墙板安装`：任务按 **m²** 计量，`LDT724_砌块墙` 的 6 行定额
**全是 工日/m³**（`LN_782` 加气混凝土砌块>200mm 0.806…），换算需要**墙厚**。
本项目 `meta.extracted_params` 里**根本没有厚度参数**（只有 total_area/total_pile=null
等），KB 那行条件里的 `>200mm` 是**定额适用条件**、不是项目实测墙厚 —— 拿它当厚度就是
编造。所以这里钉死三件事：

  1. 写法归一只认"同一量纲的另一种写法"（`㎡`/`m²`/`m2`/`M2`/`平方米` 都是 m²）；
  2. 面积↔体积**没有厚度就必须 unusable**，且原因里要写清"补哪个参数"（可执行），
     绝不 1:1 硬套、也绝不从定额条件里"读"出一个厚度；
  3. 有厚度（且单位明确）时才换算，`factor = 厚度(m)`。

顺带钉死第 40 轮修掉的那个回归：**binding 里没有 `unit` 键**（合成/遗留绑定）时
resource 必须保持旧行为放行 —— 拿不到定额分母单位不等于"单位不一致"。

运行：python -m pytest backend/tests/test_unit_area_volume.py -q
"""

import os
import sys

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from pipeline import kb_units as U                       # noqa: E402
from pipeline.nodes import norm_bind as NB                # noqa: E402
from pipeline.nodes import resource as R                  # noqa: E402


# ==================== 1. 写法归一：同一面积的两种写法都要放行 ====================

@pytest.mark.parametrize("raw", ["㎡", "m²", "m2", "M2", "M^2", "平方米"])
def test_area_spellings_normalize_and_are_same(raw):
    """「㎡」（CJK 兼容字形 U+33A1）是**实测数据里真实存在的写法**（7.1.x 预留预埋）。

    救回那几条的依据就是这一条：归一之后 `㎡ vs 工日/m²` 从 unusable 变 same。
    """
    assert U.normalize_unit(raw) == "m²"
    assert U.unit_family(raw) == "area"
    assert U.check_unit_pair(raw, "工日/m²")["verdict"] == "same"
    assert U.check_unit_pair(raw, "工日/m²") == U.check_unit_pair("m²", "工日/m²")


def test_volume_and_length_spellings_normalize():
    assert U.normalize_unit("㎥") == "m³"          # CJK 兼容字形
    assert U.normalize_unit("立方米") == "m³"
    assert U.normalize_unit("延米") == "m"          # 延长米 = m
    assert U.normalize_unit("延长米") == "m"
    assert U.normalize_unit("㎜") == "mm" and U.unit_family("㎜") == "length"
    assert U.check_unit_pair("延米", "台班/m")["verdict"] == "same"


@pytest.mark.parametrize("raw,expected", [("大", "大"), ("方", "方"), ("项", "项"),
                                          ("m³/工日", "m³/工日"), ("", "")])
def test_unknown_spellings_are_returned_as_is(raw, expected):
    """未知/有歧义的写法**原样返回**，不许猜（"方"可能是 m³ 也可能是 m²）。

    这条是 `test_kb_units.py` 的既有契约，别名表扩容后必须仍然成立。
    """
    assert U.normalize_unit(raw) == expected


# ==================== 2. 面积↔体积：没厚度就是不可换算（不许编） ====================

def test_area_to_volume_without_thickness_is_unusable():
    r = U.check_unit_pair("m²", "工日/m³")
    assert r["verdict"] == "unusable"
    assert r["factor"] is None
    assert "不可换算" in r["detail"]                    # 既有测试/缺口报告的锚点
    assert "thickness_m" in r["detail"], "原因必须点名缺哪个参数（可执行）"
    assert U.convert(100, "m²", "m³") is None, "缺厚度绝不许 1:1 硬套"
    assert U.convert(100, "m²", "m³", {}) is None
    assert U.convert(100, "m²", "m³", {"厚度": 200}) is None, "键名不对也不算数"


def test_book_condition_over_200mm_is_not_a_project_thickness():
    """KB 行的适用条件「混凝土空心砌块，>200mm」**不是**项目实测墙厚。

    `_resolve_convert_ctx` 只认用户明写的数字（①文本 / ①'项目参数键名 / ②材料），
    真实计划的 `extracted_params` 里没有任何厚度键 → 必须返回空 ctx（→ unusable），
    而不是从 condition_text 里"读"出一个 200mm 来把 18 条 ALC 硬换算成体积。
    """
    real_params = {"project_name": "x", "total_area": 14200, "total_concrete": 4260,
                   "total_infill_wall": 120, "total_pile": None, "floors": 18}
    ctx = {"extracted_params": real_params,
           "boundary_conditions": {"materials": [
               {"name": "蒸压加气混凝土砌块", "spec": "A3.5"},
               {"name": "混凝土", "spec": "C30"}]}}
    got, src, note = NB._resolve_convert_ctx(
        "m²", "工日/m³", "ALC墙板安装 混凝土空心砌块，>200mm", ctx)
    assert got == {} and src == "" and note == ""
    assert U.check_unit_pair("m²", "工日/m³", got or None)["verdict"] == "unusable"

    # 反面：条件文本里"明写厚度"就算数（那是用户给的数字，不是定额条件）
    got2, src2, _ = NB._resolve_convert_ctx("m²", "工日/m³", "墙厚200mm 的 ALC 墙板", ctx)
    assert got2 == {"thickness_m": 0.2} and src2 == "text"


@pytest.mark.parametrize("key", ["wall_thickness_mm", "墙厚_mm", "板厚_cm"])
def test_thickness_param_is_read_by_key_with_explicit_unit(key):
    """项目参数按**键名**取值，且键名/值里必须写明单位（`_mm` / `_cm` / `_m`）。"""
    scale, unit = (0.001, "mm") if key.endswith("_mm") else (0.01, "cm")
    ctx = {"extracted_params": {key: 200}}
    got, src, _ = NB._resolve_convert_ctx("m²", "工日/m³", "", ctx)
    assert got == {"thickness_m": pytest.approx(200 * scale)}, (key, got)
    assert src == "text"


def test_thickness_param_without_unit_is_refused_not_guessed():
    """`{"wall_thickness": 200}` 是 200mm 还是 200m？只有用户知道 —— 宁可不猜。

    猜错就是 1000 倍的工程量偏差，所以这条参数**弃用**（退回"缺换算参数"）。
    """
    ctx = {"extracted_params": {"wall_thickness": 200, "厚度": 200}}
    got, src, _ = NB._resolve_convert_ctx("m²", "工日/m³", "", ctx)
    assert got == {} and src == ""
    # 值里写明单位就采用
    got2, _, _ = NB._resolve_convert_ctx(
        "m²", "工日/m³", "", {"extracted_params": {"wall_thickness": "200mm"}})
    assert got2 == {"thickness_m": 0.2}


def test_area_to_volume_with_thickness_converts():
    """有厚度时 `factor = 厚度(m)`：量(m³) = 量(m²) × 厚度(m)。"""
    r = U.check_unit_pair("m²", "工日/m³", {"thickness_m": 0.2})
    assert r["verdict"] == "convertible"
    assert r["factor"] == pytest.approx(0.2)
    assert U.convert(100, "m²", "m³", {"thickness_m": 0.2})[0] == pytest.approx(20.0)
    # 反方向：m³ → m² 用 1/厚度
    r2 = U.check_unit_pair("m³", "工日/m²", {"thickness_m": 0.2})
    assert r2["verdict"] == "convertible" and r2["factor"] == pytest.approx(5.0)


# ==================== 3. 没有换算依据的组合：不许硬换算 ====================

def test_pile_count_to_volume_needs_single_pile_volume():
    """`根 ↔ m³`（2.1.2/3.1.1）：只有 `volume_per_pile_m3` 才换算，缺了就拒绝。"""
    r = U.check_unit_pair("根", "台班/m³")
    assert r["verdict"] == "unusable"
    assert "volume_per_pile_m3" in r["detail"], r["detail"]
    assert U.convert(120, "根", "m³") is None
    ok = U.check_unit_pair("根", "台班/m³", {"volume_per_pile_m3": 0.5})
    assert ok["verdict"] == "convertible" and ok["factor"] == pytest.approx(0.5)
    # 「根」≠「块」：计数类各自成族，不许互相换
    assert U.convert(120, "根", "块") is None


def test_item_to_length_has_no_basis_and_says_so():
    """`项 ↔ m`（7.4.2）：两者之间**根本没有**换算依据 —— 原因里不许许诺"补参数"。

    文案的区别是刻意的：能靠补参数救的写"缺换算参数 X"，救不了的写"请改绑/先换算"，
    否则用户会去找一个不存在的参数。
    """
    r = U.check_unit_pair("项", "工日/m")
    assert r["verdict"] == "unusable"
    assert "不可换算" in r["detail"]
    assert "没有量纲换算依据" in r["detail"]
    assert "缺换算参数" not in r["detail"], r["detail"]
    assert U.needed_context_keys("项", "m") == []
    assert U.convert(1, "项", "m") is None


# ==================== 4. 回归：binding 缺 unit 必须保持旧行为 ====================

def test_binding_without_unit_key_still_passes_legacy_gate():
    """合成/遗留 binding 里没有 `unit` 键 → **放行**（旧 `units_compatible` 行为）。

    第 40 轮我一度把它判成"单位不一致"，导致整条任务连资源都不给
    （`test_crew_bind.py` 12 个用例崩）。拿不到定额分母单位 ≠ 单位不一致。
    """
    binding = {"crew": {}, "crew_kind": "labor", "labor_types": ["钢筋工"],
               "match_type": "kb", "mode": "labor", "norm_value": 0.08,
               "quantity_basis": 1.0, "source_code": "LD_T72_7_2008", "task_id": "T1"}
    assert "unit" not in binding
    assert R.norm_binding_usable(binding) is True
    assert R._norm_evidence_reason(binding, "t") is None, "缺 unit 不许当单位不换算拦"
    assert R._norm_evidence_reason(binding, "") is None
    # 叶子单位认不出来（说不清）同样不拦 —— 与旧 units_compatible 的契约一致
    assert R._norm_evidence_reason({"unit": "工日/根"}, "???") is None


def test_binding_with_two_units_still_blocks_when_unconvertible():
    """两侧单位都在、且确实不可换算 → 仍然拦（闸门只对"说不清"松手）。"""
    b = {"match_type": "kb", "mode": "labor", "norm_value": 0.806,
         "unit": "工日/m³", "source_code": "LD_T72_4_2008"}
    why = R._norm_evidence_reason(b, "m²")
    assert why, "m² vs 工日/m³ 没厚度必须拦"
    assert "不可换算" in why and "thickness_m" in why

    # 绑定层显式判过 unusable 时以它为准；显式判过同一/可换算时放行
    assert R._norm_evidence_reason(dict(b, unit_check={"verdict": "unusable"}), "m²")
    assert R._norm_evidence_reason(
        dict(b, unit_check={"verdict": "convertible", "factor": 0.2}), "m²") is None
    assert R._norm_evidence_reason(
        dict(b, unit_check={"verdict": "same"}), "t") is None


def test_explicit_verdict_wins_over_missing_unit_fallback():
    """绑定层**显式**判过 `unusable`（如"台班"缺分母）→ 没有副作用地继续拦。

    契约 §5-WS4 ④ 的"默认拒绝"不能被"缺 unit 就放行"这条兜底吃掉。
    """
    b = {"match_type": "kb", "mode": "machine", "norm_value": 0.53,
         "unit_check": {"verdict": "unusable"}, "source_code": "GD_2018_A1_3"}
    assert "unit" not in b
    assert R._norm_evidence_reason(b, "根")


# ==================== 5. 换算系数必须真的进 resource 的工程量 ====================

def test_convert_factor_enters_norm_math():
    """`convert_factor` 进 `compute_norm_resources`：120 根 × 18 m = 2160 m。

    旧实现只认 `quantity`（120），会少算 18 倍台班 —— 光"放行"不换算 = 换了个错数。
    """
    task = {"id": "2.1.1", "name": "预应力管桩", "unit": "根", "quantity": 120,
            "kb_activity_id": "PILE_NEW_PHC"}
    binding = {"mode": "machine", "norm_value": 0.53, "quantity_basis": 100.0,
               "unit": "台班/m", "source_code": "GD_2018_A1_5", "match_type": "kb",
               "convert_factor": 18.0, "convert_denominator": "m",
               "unit_check": {"verdict": "convertible", "factor": 18.0},
               "machine_name": "静力压桩机"}
    demand = R.compute_norm_resources(task, binding, 120.0, 10.0)
    assert demand, "可换算的台班定额必须算出资源"
    # 总台班 = 2160 m × 0.53 / 100 = 11.448（落库前 round 2 位 → 11.45）；
    # 不乘 factor 的话是 0.636 —— 相差 18 倍，正是"根当米用"的另一种形态。
    assert demand["静力压桩机_total_days"] == pytest.approx(11.45, abs=1e-9)
    assert demand["quantity"] == 120.0, "叶子原始工程量不许被改写"


# ==================== 6. 第 41 轮：缺墙厚时"自处理 + 明确标注" ====================
# 第 40 轮的"没有厚度就 unusable"在 `kb_units` 层**不变**（见第 2 节）；第 41 轮改的是
# **绑定层/资源层**：真实计划里 18 条「N-N层 ALC墙板安装」绑到了真实定额
# `LDT724_砌块墙`（工日/m³），项目里没有任何厚度参数 —— 判 unusable 的后果是这 18 条
# **一条班组都算不出来**。现在改为：
#   · 假定值 = `kb_units.DEFAULT_WALL_THICKNESS_M`，与项目既有常量
#     `beat_configs.BLOCK_WALL_THICKNESS`（内隔墙墙厚 0.2 m）同源同值；
#   · 只在"这条定额是按厚度分层的墙体定额"时生效，且逐处留痕（ctx_source /
#     coverage_reason / unit_assumption / provenance.note / `_unit_assumed`）；
#   · 工程量、WBS 一个字都不改；无物理关系的单位对（根↔m³…）仍然拒绝。

#: 真实 KB 行（`Norm_Labor_Table` LDT724_砌块墙 的 6 行，工日/m³）
_LDT724_ROWS = [
    {"norm_id": "LN_782", "condition_text": "加气混凝土砌块，>200mm", "norm_value": 0.806},
    {"norm_id": "LN_781", "condition_text": "加气混凝土砌块，≤200mm", "norm_value": 0.943},
    {"norm_id": "LN_784", "condition_text": "混凝土空心砌块，>200mm", "norm_value": 0.85},
    {"norm_id": "LN_783", "condition_text": "混凝土空心砌块，≤200mm", "norm_value": 0.902},
    {"norm_id": "LN_786", "condition_text": "陶粒混凝土砌块，>200mm", "norm_value": 0.821},
    {"norm_id": "LN_785", "condition_text": "陶粒混凝土砌块，≤200mm", "norm_value": 0.887},
]

_ALC_ROW = {"norm_id": "LN_781", "condition_text": "加气混凝土砌块，≤200mm",
            "norm_value": 0.943, "norm_unit": "工日/m³", "quantity_unit": "m³",
            "quantity_basis": 1.0, "productivity_value": 1.0 / 0.943,
            "unit": "工日/m³", "leaf_unit": "m²", "source_code": "LD_T72_4_2008"}


def test_default_wall_thickness_constant_is_documented_not_a_fallback():
    """`DEFAULT_WALL_THICKNESS_M` 已**不是**兜底默认值（D5），只是档位值的兼容引用。

    改前语义：缺项目墙厚 → 取这个常量当"具名默认假设"。
    改后语义（D5，2026-09-21）：**禁止全局默认参数值**；厚度只能来自
    ①用户明写 ②**定额行适用条件的厚度档位**（`kb_units.assumed_context`）。
    常量本身保留，是为了与项目既有常量
    `beat_configs.BLOCK_WALL_THICKNESS = 0.2`（「砌块墙（m³）= 内隔墙面积（㎡）× 墙厚」，
    `_calc_block_wall`）**同值同源**，避免同一道墙出现两个厚度。
    """
    from pipeline.nodes import beat_configs as BC
    assert U.DEFAULT_WALL_THICKNESS_M == BC.BLOCK_WALL_THICKNESS == 0.2
    note = U.DEFAULT_WALL_THICKNESS_NOTE
    assert "定额条件" in note and "档位" in note, note
    assert "不是项目实测墙厚" in note, note


def test_assumed_context_only_for_thickness_tiered_wall_rows():
    """厚度取值只在"按厚度分层的墙体定额行"上生效，且**只取上限型档位**（D5）。

    改前：不看档位方向，一律返回全局默认 0.2 m。
    改后（D5 + 裁定-1）：从条件里解析档位值 —— `≤200mm` → 0.2 m，来源标
    **`norm_condition`**（来自定额行适用条件，**不是** AI 估算 —— 假溯源与漏标一样违规）；
    开区间 `>200mm` 档位里没有确定值可取 → 返回空（调用方按"推定不出就报缺"处理）。
    无物理关系的单位对仍然拒绝。
    """
    ctx, src, note = U.assumed_context("m²", "工日/m³", "加气混凝土砌块，≤200mm")
    assert ctx == {"thickness_m": 0.2} and src == "norm_condition" and note
    assert "定额行适用条件的厚度档位" in note
    assert "不是 AI 估算" in note, "来源文案必须说清不是 AI 估算"
    # 开区间档位（>200mm）：档位内没有可取的确定值 → 不取值
    assert U.assumed_context("m²", "工日/m³", "混凝土空心砌块，>200mm") == ({}, "", "")
    assert U.assumed_context("m²", "工日/m³", "墙体厚度≥0.3m") == ({}, "", "")
    # 不是厚度分层的行（普通 m³ 定额）→ 不给值
    assert U.assumed_context("m²", "工日/m³", "后浇带，C30 混凝土") == ({}, "", "")
    # 有厚度档位、但单位对需要别的参数（根↔m³ 要单根体积）→ 不给值
    assert U.assumed_context("根", "台班/m³", "加气混凝土砌块，≤200mm") == ({}, "", "")
    # `check_unit_pair` 本身**永远**不编厚度：没有 ctx 就是 unusable（第 40 轮契约不变）
    assert U.check_unit_pair("m²", "工日/m³")["verdict"] == "unusable"


def test_thickness_tier_parsing_and_material_pick():
    """档位解析 + 同档多材料消歧：ALC → 「加气混凝土砌块」档。"""
    assert NB._thickness_tier_of("加气混凝土砌块，≤200mm") == ("le", 0.2)
    assert NB._thickness_tier_of("加气混凝土砌块，>200mm") == ("gt", 0.2)
    assert NB._thickness_tier_of("混凝土空心砌块") is None
    assert NB._tier_contains(("le", 0.2), 0.2) is True
    assert NB._tier_contains(("gt", 0.2), 0.2) is False, "0.2m 不属于 >200mm 档"

    row, why = NB._pick_row_by_assumed_thickness(_LDT724_ROWS, 0.2, "1-1层 ALC墙板安装")
    assert row["norm_id"] == "LN_781", "ALC = 蒸压加气混凝土 → 取加气砌块档"
    assert "≤200mm" in why and "材料" in why

    row2, _ = NB._pick_row_by_assumed_thickness(_LDT724_ROWS, 0.2, "混凝土空心砌块砌筑")
    assert row2["norm_id"] == "LN_783", "任务里写空心砌块 → 取空心砌块档"

    row3, why3 = NB._pick_row_by_assumed_thickness(_LDT724_ROWS, 0.2, "")
    assert row3["norm_id"] == "LN_781" and "KB 行序" in why3, "认不出材料 → 按 KB 行序（可复现）"

    row4, _ = NB._pick_row_by_assumed_thickness(_LDT724_ROWS, 0.3, "")
    assert row4["norm_id"] == "LN_782", "300mm 落在 >200mm 档"


def test_binding_layer_applies_labelled_thickness_assumption():
    """绑定层：缺墙厚 → 取**定额行条件的厚度档位**（`≤200mm`→0.2 m）并留痕（不再是 unusable）。

    改前：取全局默认假设 `DEFAULT_WALL_THICKNESS_M`。
    改后（D5 + 裁定-1）：从定额条件解析档位值 —— `convert_factor` 仍是 0.2，
    但 `ctx_source` 从 `ai_estimate` 改成 **`norm_condition`**（厚度来自定额行条件，
    不是 AI 猜的；假溯源与漏标一样违规），来源文案也改成
    "来自定额行适用条件的厚度档位"。
    """
    from pipeline.nodes.norm_bind import NormBindNode
    node = NormBindNode()
    binding = {}
    node._fill_from_labor_row(binding, dict(_ALC_ROW), "default", "kb", "中",
                              "用户未提供条件，取典型值（候选 6 个）",
                              leaf_unit="m²", ctx={}, text="1-1层 ALC墙板安装")
    assert binding["unit_check"]["verdict"] == "convertible"
    assert binding["convert_factor"] == pytest.approx(0.2)
    assert binding["convert_denominator"] == "m³", \
        "分母也要写下来：下游要知道'换算到哪个单位'，别各自去 parse unit"
    assert binding["ctx_source"] == "norm_condition"
    assert "coverage_reason" not in binding, \
        "来源不是 AI 估算，不许进 'AI估算换算参数' 桶（裁定-1）"
    assert binding["unit_assumption"]["thickness_m"] == pytest.approx(0.2)
    assert binding["unit_assumption"]["source"] == "norm_condition"
    assert binding["norm_is_evidence"] is True and binding["usable"] is True
    note = binding["provenance"]["note"]
    assert "定额行适用条件的厚度档位" in note, note
    assert "≤200mm" in note, "说明里要带上是哪条定额条件"
    # 用户明写墙厚时**不吃条件档位**：factor 用实测值、来源标 text
    b2 = {}
    node._fill_from_labor_row(b2, dict(_ALC_ROW), "default", "kb", "中", "",
                              leaf_unit="m²", ctx={}, text="墙厚300mm 的 ALC 墙板")
    assert b2["convert_factor"] == pytest.approx(0.3)
    assert b2["ctx_source"] == "text"
    assert "unit_assumption" not in b2


def test_legacy_binding_self_heals_with_labelled_assumption():
    """老计划自救：绑定层当时写死的 unusable 会被**具名假设**改写并留痕。

    ⚠️ 只对"按厚度分层的墙体定额行"生效；合成绑定（没有 `condition_text`）仍然被拦
    —— 见 `test_binding_with_two_units_still_blocks_when_unconvertible`。
    """
    b = {"match_type": "kb", "mode": "labor", "norm_value": 0.943,
         "productivity_value": 1.0 / 0.943, "unit": "工日/m³", "leaf_unit": "m²",
         "condition_text": "加气混凝土砌块，≤200mm", "source_code": "LD_T72_4_2008",
         "norm_is_evidence": False, "usable": False,
         "not_usable_reason": "单位不可用：单位不一致且不可换算",
         "unit_check": {"verdict": "unusable", "factor": None}}
    assert R._norm_evidence_reason(b, "m²") is None, "厚度分层的墙体定额行 → 放行"
    assert R._materialize_unit_assumption(b, "m²") is True
    assert b["convert_factor"] == pytest.approx(0.2) and b["usable"] is True
    assert b["not_usable_reason"] == "" and b["unit_assumption"]["thickness_m"] == 0.2

    task = {"id": "6.1.1.1", "name": "1-1层 ALC墙板安装", "unit": "m²", "quantity": 1420.0}
    demand = R.compute_norm_resources(task, b, 1420.0, 3.0)
    assert demand and demand["普工_per_day"] >= 1
    text = demand["_unit_assumed"]
    # 标注必须含：厚度档位来源 → 换算后的体积 → 定额行 → 算出的工日 → 来源说明
    # （W2-C 已把 resource.py 的文案从"AI 假定墙厚"改成"定额行适用条件的厚度档位"，
    #   来源标 `ctx_source=norm_condition` —— 裁定-1；本断言按新语义核对）
    assert "定额行适用条件的厚度档位" in text and "284 m³" in text, text
    assert "0.943" in text
    # 老绑定没有 `norm_id`（第 41 轮才开始写）→ 回落到 source_code + 定额条件，照样可定位
    assert "LD_T72_4_2008" in text and "加气混凝土砌块，≤200mm" in text
    assert "267.81" in text, "理论需求 = 284 m³ × 0.943 工日/m³"
    assert "270 工日" in text, "按排程工期向上取整后的工日"
    assert "norm_condition" in text, "来源必须照实标成 norm_condition（不是 AI 估算）"
    assert "BLOCK_WALL_THICKNESS" in text, "厚度要与项目砌块墙口径可对照"
    assert "_norm_flagged" not in demand and "_warning" not in demand, \
        "算出来了（带假定）≠ 没算出来：不许混进未计算班组的字段"
    assert demand["quantity"] == 1420.0, "工程量一个字都不许改"


def test_compute_flat_marks_assumption_and_group_magnitude():
    """`compute_flat`：18 条同口径任务各自带标注 + 合计与项目规模的对照；不改数。"""
    leaves = [{"id": "6.1.%d.1" % i, "name": "%d-%d层 ALC墙板安装" % (i, i),
               "unit": "m²", "quantity": 1420.0, "duration_days": 3,
               "norm_binding": dict(_ALC_ROW, mode="labor", match_type="kb",
                                    usable=True, norm_is_evidence=True,
                                    unit_check={"verdict": "convertible", "factor": 0.2},
                                    convert_factor=0.2, unit_assumption={
                                        "thickness_m": 0.2, "source": "ai_estimate"},
                                    provenance={"origin": "kb", "ref": "LD_T72_4_2008"})}
              for i in range(1, 19)]
    wbs = {"phases": [{"phase": "二次结构与砌体", "work_packages": [
        {"name": "ALC墙板安装", "sub_packages": leaves}]}]}
    params = {"total_area": 14200, "floors": 18}
    flat = R.compute_flat(wbs, params, None, schedule_days={})
    tasks = {t["task_id"]: t for t in (flat["resource_demand"]["tasks"])}
    assert len(tasks) == 18
    for tid, t in tasks.items():
        assert t["quantity"] == 1420.0, "工程量必须原样"
        assert "_unit_assumed" in t and "_unit_assumed_facts" not in t
    text = tasks["6.1.1.1"]["_unit_assumed"]
    assert "合计 25560 m²" in text and "4860 工日" in text
    assert "1420/层 × 18 层" in text and "ALC_AREA_FACTOR=1.8" in text
    assert "_warning" not in tasks["6.1.1.1"]
    assert [lf["quantity"] for lf in leaves] == [1420.0] * 18, "WBS 也不许被改"


def test_unit_assumed_survives_into_plan_json():
    """新标注字段必须真的进计划 JSON（`plan_assembler` 整块透传 resource_demand）。"""
    from pipeline.nodes import plan_assembler as PA
    demand = {"tasks": [{"task_id": "6.1.1.1", "task_name": "1-1层 ALC墙板安装",
                         "quantity": 1420.0, "planned_duration_days": 3,
                         "普工_per_day": 9, "普工_total_days": 270.0,
                         "_unit_assumed": "按 AI 假定墙厚 200mm 换算：…"}]}
    ctx = {"resource_demand": R.to_nested_resources(demand), "wbs": {}, "extracted_params": {}}
    parts = {"overview": {"project_name": "x"}, "key_milestones": [],
             "critical_path_tasks": [], "all_tasks_schedule": [],
             "resource_plan": {}, "risks": []}
    plan = PA.assemble_plan_json(ctx, parts)
    got = plan["resource_demand"]["tasks"][0]
    assert got["_unit_assumed"].startswith("按 AI 假定墙厚")
    assert got["resources"]["普工"]["per_day"] == 9
