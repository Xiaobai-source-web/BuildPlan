# -*- coding: utf-8 -*-
"""用户给定的**模板总量 / 砌体总量**优先于系数推算 —— 第三项（2026-09-21）回归测试。

背景：最终验收输入 `项目样例/示例3_住宅楼_对比版.txt` 里明写「模板：约25000平方米」、
「砌体：约3000立方米」，但参数模型原先没有 `total_formwork` / `total_masonry` 两个键，
这两个量被静默丢弃、只能靠系数推算（模板 2.5 倍面积、砌体 = 内隔墙面积 × 墙厚 0.2 m）。

本文件钉住三件事：
  ① **用户给了就用用户的**：总量按「各阶段层数占比」分摊到用了同一计算器的阶段
     （每层量都与阶段无关 ⇒ 分摊比例 = 层数之比），各阶段合计 ≈ 用户给的那**一个**总量；
  ② **用户没给 → 逐位不变**（把改动前的数字与公式冻成字面量，这条是最重要的回归守卫）；
  ③ **单位不许 1:1 套用**：`total_masonry` 是 m³，砌块墙也是 m³（可 1:1），
     但「勾缝」的 unit 是 m² ⇒ 必须按墙厚 `BLOCK_WALL_THICKNESS = 0.2 m` 换算，
     换算过程要写进依据文案（可复核）。

运行：python -m pytest backend/tests/test_beat_total_qty.py -q -p no:cacheprovider
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest                                                  # noqa: E402

from pipeline.nodes import beat_configs as BC                  # noqa: E402

# 最终验收输入的口径：15000 m² / 18 层 / 1 栋 → 单栋标准层 833.33 m² → B2 后 2 个分区
PARAMS = {"total_area": 15000, "floors": 18, "building_count": 1}
#: 用户明写的两项总量（验收输入原文：模板约 25000 m²、砌体约 3000 m³）
USER_TOTALS = {"total_formwork": 25000, "total_masonry": 3000}
#: 模板类各阶段层数之和（地下室 2 层 + 地上主体 18 层）；砌体类只有二次结构 18 层
FORMWORK_SPAN = 20.0
MASONRY_SPAN = 18.0
#: 多栋参数（12 栋 / 215000 m² / 18 层）——**不带**用户总量，用于默认口径的回归守卫
MULTI_PARAMS_NO_TOTALS = {"total_area": 215000, "floors": 18, "building_count": 12}
#: 多栋参数 + 全项目总量（12 栋 × 各 25000 m² 模板 / 3000 m³ 砌体）—— 用于 I1-1 折算白名单
MULTI_PARAMS = dict(MULTI_PARAMS_NO_TOTALS, total_formwork=300000, total_masonry=36000)
#: 多栋默认口径的冻结值（改动前实测：单栋标准层 995.37 m²、2 个分区）
MULTI_FROZEN = {
    ("地上主体结构", "铝模安装"): (1244.21,
        "全项目共 12 栋（总建筑面积215000m²÷12栋 = 17916.67m²/栋），按单栋口径编制、各栋平行施工；"
        "单栋标准层995.37m²×2.5（模板接触面积系数）÷2区 = 1244.21 m²/层"),
    ("地下室结构", "模板安装"): (1244.21,
        "全项目共 12 栋（总建筑面积215000m²÷12栋 = 17916.67m²/栋），按单栋口径编制、各栋平行施工；"
        "单栋标准层995.37m²×2.5（模板接触面积系数）÷2区 = 1244.21 m²/层"),
    ("二次结构与砌体", "砌块墙"): (179.17,
        "全项目共 12 栋（总建筑面积215000m²÷12栋 = 17916.67m²/栋），按单栋口径编制、各栋平行施工；"
        "单栋标准层995.37m²×1.8（内隔墙面积系数）×0.2 m（墙厚）÷2区 = 179.17 m³/层"),
    ("二次结构与砌体", "勾缝"): (179.17,
        "全项目共 12 栋（总建筑面积215000m²÷12栋 = 17916.67m²/栋），按单栋口径编制、各栋平行施工；"
        "单栋标准层995.37m²×1.8（内隔墙面积系数）×0.2 m（墙厚）÷2区 = 179.17 m³/层"),
    ("二次结构与砌体", "ALC墙板安装"): (895.83,
        "全项目共 12 栋（总建筑面积215000m²÷12栋 = 17916.67m²/栋），按单栋口径编制、各栋平行施工；"
        "单栋标准层995.37m²×1.8（内隔墙面积系数）÷2区 = 895.83 m²/层"),
}


def _detail(phase, params, step_name):
    cfg, note = BC.derive_beat_quantities(BC.BASE_BEAT_CONFIGS[phase], params)
    step = next(s for s in cfg["cycle"] if s["name"] == step_name)
    info = ((note or {}).get("detail") or {}).get(step_name) or {}
    return step, info, (note or {}).get("source")


def _phase_total(phase, params, step_name):
    """该工序在阶段内的总量（∑ 叶子 quantity）——真正落进计划的数字。"""
    from pipeline import layer_engine as LE                    # noqa: PLC0415
    phase_dict, _ids = LE.expand_node(BC.BASE_BEAT_CONFIGS[phase], params)
    return sum(float(l.get("quantity") or 0)
               for wp in phase_dict["work_packages"]
               for l in wp.get("sub_packages") or []
               if (l.get("_step_name") or "") == step_name)


# ==================== ① 用户给了总量：由**占比表**拆到各构件 ====================
#: 占比表（`Component_Ratio`）在本文件里的进程内替身（frame_shear 实测值；测试不读库）。
#: 形状 = `ratio_scope.build` 的产物：`l4_quantities` + `_component_ratio.l4_index`。
RATIO_ROWS = {
    "FORM_NEW_FOUND": (14.0, "formwork", 25000.0, "m²"),
    "FORM_NEW_OTHER": (8.0, "formwork", 25000.0, "m²"),
    "LDT724_砌块墙": (35.4, "masonry", 3000.0, "m³"),
    "LDT724_砖墙_混水内": (17.8, "masonry", 3000.0, "m³"),
}


def with_ratio(base, *, exempt=None):
    """挂上占比表替身（**唯一真源**），使这几道工序走 B1/B4 的唯一量链路。"""
    p = dict(base)
    idx, qs = {}, {}
    for aid, (pct, wt, total, unit) in RATIO_ROWS.items():
        q = total * pct / 100.0
        qs[aid] = q
        idx[aid] = {"structure_type_id": "frame_shear", "activity_id": aid,
                    "work_type_id": wt, "ratio_percent": pct, "quantity": q,
                    "unit": unit, "confidence": "LOW", "review_state": "pending",
                    "notes": "AI 经验估算 V1"}
    p["structure_type"] = "frame_shear"
    p["l4_quantities"] = qs
    p["_component_ratio"] = {"structure_type_id": "frame_shear", "l4_index": idx}
    if exempt:
        p["component_ratio_exempt"] = list(exempt)
    return p


class TestUserTotalsWin:
    """用户给了总量 ⇒ 由 `Component_Ratio` 拆到各 L4，再按**层面积 / 段面积**落到叶子。

    ⚠️ 与接线前最大的不同：**不再**按「各阶段层数占比」把总量摊到阶段上
    （那套 `_floors_total_same_scope` 口径已随旧阶段比例表一起退役）。
    量由「该 L4 在组内的占比」决定 —— 所以某个阶段拿到的量取决于**它用的 L4**，
    而不是它的层数。
    """

    def test_模板总量由占比表拆到模板类L4_阶段合计等于该L4总量(self):
        p = with_ratio(dict(PARAMS, **USER_TOTALS))
        # 铝模安装 → FORM_NEW_OTHER（8% of 25000 = 2000 m²）
        got, info, _ = _detail("地上主体结构", p, "铝模安装")
        assert info["source"] == BC.SOURCE_RATIO, info
        assert "Component_Ratio" in info["formula"] and "2000" in info["formula"]
        assert abs(_phase_total("地上主体结构", p, "铝模安装") - 2000.0) <= 1.0
        # 模板安装 → FORM_NEW_FOUND（14% of 25000 = 3500 m²）
        assert abs(_phase_total("地下室结构", p, "模板安装") - 3500.0) <= 1.0
        # 组内 ∑=100：本阶段用到的两个 L4 合计 5500，其余 19500 属梁/柱/铝模安拆四个 L4，
        # 它们**不在节拍叶子**上（落在 kb_scope 候选 → LLM WBS 侧）—— 见 BLOCKERS
        assert 5500.0 == 25000.0 * (8.0 + 14.0) / 100.0

    def test_砌体总量由占比表拆到砌体类L4_合计等于该L4总量(self):
        p = with_ratio(dict(PARAMS, **USER_TOTALS))
        wall, info, source = _detail("二次结构与砌体", p, "砌块墙")
        assert info["source"] == BC.SOURCE_RATIO, (info, source)
        assert "Component_Ratio" in info["formula"]
        assert abs(_phase_total("二次结构与砌体", p, "砌块墙") - 3000.0 * 0.354) <= 1.0

    def test_缺total_area时占比表分解不可用_如实退回基线(self):
        """占比表的两步都**必须**有层面积（②层量、③段量）⇒ 缺 total_area 时不能假装拆分。"""
        p = with_ratio({"floors": 18, "building_count": 1, "total_formwork": 25000})
        got, info, _ = _detail("地上主体结构", p, "铝模安装")
        assert info["source"] == BC.SOURCE_BASE, info
        assert got["qty_per_floor"] == 1900.0, got     # 配置里的基线写死值（未动）

    def test_多栋项目按单栋口径折算(self):
        """2 栋 × 各 25000 m² 模板 + 各 3000 m³ 砌体 → 单栋折算后与单栋样例一致。"""
        single = with_ratio(dict(PARAMS, **USER_TOTALS))
        multi = with_ratio(dict(PARAMS, **dict(USER_TOTALS, building_count=2,
                                               total_formwork=50000,
                                               total_masonry=6000,
                                               total_area=30000)))
        for phase, step in (("地上主体结构", "铝模安装"),
                            ("二次结构与砌体", "砌块墙")):
            a, _, _ = _detail(phase, single, step)
            b, _, _ = _detail(phase, multi, step)
            assert a["qty_per_floor"] == b["qty_per_floor"], (phase, step, a, b)

    def test_总量非法时退回系数推算(self):
        for bad in ("", None, "abc", -1, 0):
            p = dict(PARAMS, total_formwork=bad, total_masonry=bad)
            tmpl, _, _ = _detail("地上主体结构", p, "铝模安装")
            wall, _, _ = _detail("二次结构与砌体", p, "砌块墙")
            assert tmpl["qty_per_floor"] == 1041.67, (bad, tmpl)
            assert wall["qty_per_floor"] == 150.0, (bad, wall)


# ==================== ② 用户没给 → 逐位不变的回归守卫 ====================
class TestDefaultUnchanged:
    """**本项最重要的一类断言**：没给新参数时，数字与公式与改动前**逐位相同**。

    冻结值口径 = 最终验收输入（15000 m² / 18 层 / 1 栋，B2 后 2 个分区）。
    """

    def test_模板默认口径逐位不变(self):
        for phase, step in (("地上主体结构", "铝模安装"), ("地下室结构", "模板安装")):
            got, info, _ = _detail(phase, PARAMS, step)
            assert got["qty_per_floor"] == 1041.67, (phase, got)
            assert info["formula"] == \
                "单栋标准层833.33m²×2.5（模板接触面积系数）÷2区 = 1041.67 m²/层", info["formula"]
            assert info["source"] == BC.SOURCE_PARAM, info

    def test_砌体默认口径逐位不变(self):
        wall, info, _ = _detail("二次结构与砌体", PARAMS, "砌块墙")
        assert wall["qty_per_floor"] == 150.0, wall
        assert info["formula"] == \
            ("单栋标准层833.33m²×1.8（内隔墙面积系数）×0.2 m（墙厚）÷2区 = 150 m³/层"), \
            info["formula"]

    def test_ALC默认口径逐位不变(self):
        alc, info, _ = _detail("二次结构与砌体", PARAMS, "ALC墙板安装")
        assert alc["qty_per_floor"] == 750.0, alc
        assert info["formula"] == "单栋标准层833.33m²×1.8（内隔墙面积系数）÷2区 = 750 m²/层", \
            info["formula"]

    def test_多栋参数下默认口径逐位不变(self):
        """I1-5 第二条：**多栋参数**（12 栋 / 215000 m² / 18 层）下同样逐位不变。

        覆盖模板（地上+地下）、砌体、勾缝、ALC 四条路径 —— 新参数一个都没给，
        走的必须是与改动前完全相同的那条分支。
        """
        for (phase, step), (qty, formula) in MULTI_FROZEN.items():
            got, info, _ = _detail(phase, MULTI_PARAMS_NO_TOTALS, step)
            assert got["qty_per_floor"] == qty, (phase, step, got)
            assert info["formula"] == formula, (phase, step, info["formula"])

    def test_勾缝默认口径与砌块墙一致_逐位不变(self):
        joint, info, _ = _detail("二次结构与砌体", PARAMS, "勾缝")
        assert joint["qty_per_floor"] == 150.0, joint
        assert info["formula"] == \
            ("单栋标准层833.33m²×1.8（内隔墙面积系数）×0.2 m（墙厚）÷2区 = 150 m³/层"), \
            info["formula"]

    def test_勾缝与ALC不从砌体总量拆分_恒走系数路径(self):
        """裁定 I1-3：勾缝（m²）与 ALC（m²）是**附属项**，量纲与砌体总量（m³）不同 ⇒ 不接。

        若哪天要接，必须走 `kb_units.assumed_context(from_unit, to_unit, condition_text)`
        取定额行厚度档位，**不许硬编码厚度**（0.2 m 的待审问题见 `_block_wall_by_coef`）。
        """
        p = dict(PARAMS, **USER_TOTALS)
        joint, info_j, _ = _detail("二次结构与砌体", p, "勾缝")
        alc, info_a, _ = _detail("二次结构与砌体", p, "ALC墙板安装")
        assert joint["qty_per_floor"] == 150.0, joint      # 与"没给总量"逐位相同
        assert "用户给定" not in info_j["formula"], info_j["formula"]
        assert alc["qty_per_floor"] == 750.0, alc
        assert "用户给定" not in info_a["formula"], info_a["formula"]

    def test_分摊层数取自实际展开的层数_不是写死的18或20(self):
        """层数一律取自**实际展开**的层数（`layer_engine._eff_floors` + `segment_floors`）。

        ⚠️ 接线前的 `_floors_total_same_scope`（"各阶段层数之和"当分摊分母）已随旧阶段
        比例表一起**删除**：新口径下真正决定量的是**层面积**，不是阶段层数。
        本用例改为守住"层数确实来自实际展开、不是写死值"这一条。
        """
        from pipeline import layer_engine as LE                # noqa: PLC0415
        covered = 0.0
        for phase in ("地下室结构", "地上主体结构"):
            cfg = BC.BASE_BEAT_CONFIGS[phase]
            floors = LE._eff_floors(cfg, PARAMS)
            segs = BC.segment_floors(floors, int(cfg.get("segments") or 1),
                                     per=cfg.get("floors_per_segment"))
            assert abs(sum(end - start for start, end in segs) - floors) < 1e-9
            covered += sum(end - start for start, end in segs)
        assert covered == 20.0, covered            # 地下 2 + 地上 18 = 实际展开层数

        # 层数变了展开跟着变（证明没有写死）：36 层 → 地上实际展开 36 层
        p36 = dict(PARAMS, floors=36)
        cfg = BC.BASE_BEAT_CONFIGS["地上主体结构"]
        assert LE._eff_floors(cfg, p36) == 36.0
        # 占比表口径下层量 = L4 总量 ÷ 层数（层面积之和固定 = total_area）⇒
        # 不论 18 层还是 36 层，**该阶段的模板总量恒 = L4 总量**（只是摊到更多层上）
        p18 = with_ratio(dict(PARAMS, **USER_TOTALS))
        p36r = with_ratio(dict(p36, **USER_TOTALS))
        assert _phase_total("地上主体结构", p18, "铝模安装") == pytest.approx(2000.0, rel=1e-3)
        assert _phase_total("地上主体结构", p36r, "铝模安装") == pytest.approx(2000.0, rel=1e-3)

    def test_多栋时总量先折算成单栋_I1_1白名单(self):
        """裁定 I1-1：`total_formwork` / `total_masonry` 必须在 `per_building_params` 折算白名单里。

        不加的话多栋项目会把**全项目总量当单栋量**用，量级直接 ×N（单栋样例看不出来）。
        """
        p = MULTI_PARAMS
        pb = BC.per_building_params(p)
        assert pb["total_formwork"] == 25000.0, pb.get("total_formwork")
        assert pb["total_masonry"] == 3000.0, pb.get("total_masonry")
        assert pb["building_count"] == 1
        # 端到端：全项目 12×25000 模板 / 12×3000 砌体 → 单栋折算后与单栋样例逐位一致
        single = with_ratio(dict(PARAMS, **USER_TOTALS))
        for phase, step in (("地上主体结构", "铝模安装"), ("二次结构与砌体", "砌块墙")):
            a, _, _ = _detail(phase, single, step)
            b, _, _ = _detail(phase, MULTI_PARAMS, step)   # 无占比表 → 系数路径，不做拆分
            assert b["qty_per_floor"] > 0, (phase, step, b)
        # 有占比表 + 12 栋 ⇒ 单栋总量折算后再按占比拆，与单栋样例逐位一致
        multi_ratio = with_ratio(dict(PARAMS, **dict(USER_TOTALS, building_count=12,
                                                     total_area=180000,
                                                     total_formwork=300000,
                                                     total_masonry=36000)))
        for phase, step in (("地上主体结构", "铝模安装"), ("二次结构与砌体", "砌块墙")):
            a, _, _ = _detail(phase, single, step)
            b, _, _ = _detail(phase, multi_ratio, step)
            assert a["qty_per_floor"] == b["qty_per_floor"], (phase, step, a, b)

    def test_没有用户总量也没有标准层面积时仍是基线(self):
        """两头都没有 → 不猜：退回基线并标「基线默认」（既有第 8 条规则不变）。"""
        got, info, source = _detail("地上主体结构", {"floors": 18}, "铝模安装")
        assert source == BC.SOURCE_BASE, source
        assert info["source"] == BC.SOURCE_BASE, info
        assert got["qty_per_floor"] == 1900  # 「铝模安装」配置里的基线写死值（未动）


if __name__ == "__main__":
    import inspect

    for cls_name, cls in sorted(globals().items()):
        if not (inspect.isclass(cls) and cls_name.startswith("Test")):
            continue
        obj = cls()
        for name in sorted(dir(obj)):
            if name.startswith("test_"):
                getattr(obj, name)()
                print("  PASS  %s.%s" % (cls_name, name))
    print("全部 beat_total_qty 用例通过 ✔")
