# -*- coding: utf-8 -*-
"""多栋项目（building_count）专项测试 —— 12 栋的口径必须一路贯通

背景（真实产品缺陷·最大的一处口径坑）：
    潭村项目的 `total_area = 215000 ㎡` 是**12 栋楼合计**，但系统把它当成一栋
    38 层的楼，于是：
      · `suggest_zones(215000/38 = 5658 ㎡)` → 把一棵 471 ㎡/层的塔楼切成多个施工段
        （B2 前是 4 档上限 4 段；B2 后按 MSSA=500 → 11 段），叶子数被放大 4~11 倍；
      · 面积类工序又每个阶段都把"整栋建筑面积"算一遍 → 模板量凭空翻倍；
      · 地下室只有 2 层，却拿"整栋楼的面积"去除以 2 → 单层量放大 19 倍，
        单条地下室模板任务要 286 天。
    实测总工期因此从可信的 600 天级飙到 8833 天。

修正口径（本文件的断言即契约）：
    1. 栋数来自**用户/资料**（`building_count`），缺失默认 1，**不许 LLM 补全猜测**；
    2. 分区建议用**单栋标准层面积** = 总面积 ÷ 栋数 ÷ 地上层数；
    3. 单层工程量按**单栋**口径（总量类参数先 ÷ 栋数）；
    4. 面积类工序的统一基数是单栋标准层面积，**与阶段层数无关**（地下室不再拿到整栋面积）；
    5. 全项目 N 栋平行施工 ⇒ 单栋工期 ≈ 项目工期；用户给的**全场**资源限额按栋数分摊；
    6. 单班组规模超过"全项目同时在岗上限"时缩编 + 工期等比延长（错峰解决不了）。

运行：python -m pytest backend/tests/test_building_count.py -q
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from pipeline import layer_engine as LE
from pipeline.nodes.beat_configs import (
    BASE_BEAT_CONFIGS,
    FORMWORK_AREA_FACTOR,
    building_count,
    derive_beat_quantities,
    per_building_params,
    standard_floor_area,
    suggest_zones_from_params,
)
from pipeline.nodes.boundary import BoundaryNode
from pipeline.nodes.extractor import extract_by_regex, normalize_params
from pipeline.nodes.plan_assembler import build_meta
from pipeline.nodes.scheduler import compute_schedules, scale_limits_per_building

# 潭村：12 栋、地上 38 层、总建筑面积 21.5 万㎡
TAN = {"total_area": 215000, "total_concrete": 82000, "total_rebar": 12800, "floors": 38}
TAN12 = dict(TAN, building_count=12)
TAN_TEXT = ("广州市白云区潭村城中村改造项目首开区安置地块，共 12 栋，地上 38 层，"
            "总建筑面积 21.5万㎡，混凝土 8.2万m³，钢筋 1.28万吨，开工 2025-04-16")


def approx(want):
    return pytest.approx(float(want), rel=1e-3, abs=0.01)


def _step(cfg, name):
    for s in cfg.get("cycle") or []:
        if s.get("name") == name:
            return s
    raise AssertionError("找不到工序：" + name)


# ================= 1. 参数抽取：栋数 / 层数 =================
def test_extract_building_count_and_floors():
    p = extract_by_regex(TAN_TEXT)
    assert p.get("building_count") == 12, "「共 12 栋」必须被抽出来"
    assert p.get("floors") == 38, "「地上 38 层」必须被抽出来（不是地下 2 层）"
    assert p.get("total_area") == 215000


def test_extract_single_building_and_variants():
    for text, want in (("本工程 1 栋 26 层住宅", 1),
                       ("共3幢高层", 3),
                       ("项目含 8 座塔楼，地上 45 层", 8)):
        assert extract_by_regex(text).get("building_count") == want


def test_extract_floors_ignores_basement():
    """「地下 2 层」不能被当成总层数（只认带 地上/共/总/建筑 限定的说法）。"""
    p = extract_by_regex("地下 2 层车库，总建筑面积 5000 ㎡")
    assert p.get("floors") is None


def test_normalize_params_keeps_building_count_numeric():
    p = normalize_params({"building_count": "12", "floors": "38.0"}, "")
    assert p["building_count"] == 12 and p["floors"] == 38
    assert normalize_params({}, "")["building_count"] is None


# ================= 2. 栋数取值：默认 1，非法回落 =================
def test_building_count_defaults_to_one():
    assert building_count({}) == 1
    assert building_count(None) == 1
    assert building_count({"building_count": None}) == 1
    assert building_count({"building_count": ""}) == 1
    assert building_count({"building_count": 0}) == 1
    assert building_count({"building_count": -3}) == 1
    assert building_count({"building_count": "abc"}) == 1
    assert building_count({"building_count": "12"}) == 12


def test_per_building_params_divides_only_totals():
    pb = per_building_params(TAN12)
    assert pb["total_area"] == approx(215000 / 12)
    assert pb["total_concrete"] == approx(82000 / 12)
    assert pb["total_rebar"] == approx(12800 / 12)
    assert pb["floors"] == 38, "层数不是总量，不能折算"
    assert pb["building_count"] == 1, \
        "折算后必须把栋数置 1：standard_floor_area() 自己会再除栋数，否则重复除（实测单层模板算成 98㎡）"
    # 单栋项目原样返回（同一个对象，不做无意义的复制）
    assert per_building_params(TAN) is TAN


# ================= 3. 标准层面积与分区建议 =================
def test_standard_floor_area_is_per_building():
    assert standard_floor_area(TAN) == approx(215000 / 38)          # 单栋：5658 ㎡
    assert standard_floor_area(TAN12) == approx(215000 / 12 / 38)   # 12 栋：471.5 ㎡
    assert standard_floor_area({"floors": 38}) is None, "缺面积不许猜"


def test_suggest_zones_uses_single_building():
    """核心回归：12 栋的合计面积**不许**被当成一栋的超大平层。

    B2（2026-09-21）后分区口径 = MSSA 500 m²（方案 §4.1）：`n = ceil(层面积 ÷ 500)`
    + 余量判定。这里两个数仍能一眼看出"按不按单栋算"：
      · 当一栋算 → 5658 m² → ceil(11.32)=12、余量 158 < 166.67 → 弃用 MSSA → 11 段
        （那棵 471.5 m²/层的塔楼被切成 4 段是错的；新口径按 500 m²/段切成 11 段）
      · 按单栋算 → 471.5 m² → 1 段
    """
    assert suggest_zones_from_params(TAN) == 11, "当一栋算 → 5658 m² → 11 个分区（错的那版）"
    assert suggest_zones_from_params(TAN12) == 1, "按单栋算 → 471.5 m² → 1 个分区"


def test_zone_count_actually_shrinks_the_tree():
    """分区数一变，展开出来的叶子数应随之变化（不是只改了个展示数字）。"""
    cfg = BASE_BEAT_CONFIGS["地上主体结构"]
    steps = len(cfg.get("cycle") or []) + len(cfg.get("attach_measures") or [])
    z1 = LE._effective_zones_count(cfg, TAN)          # 建议段数 = 11
    n1 = len(LE.expand_node(cfg, TAN)[1])
    n12 = len(LE.expand_node(cfg, TAN12)[1])
    assert n1 == z1 * n12, "叶子数应与分区数同比例（%d:1）" % z1
    assert n12 == 38 * steps, "单栋 38 层 × 一层一段 × 每段工序数"
    assert n12 < 200, "单栋展开后叶子数量级应可控（甘特图不至于上千行）"


# ================= 4. 单层工程量按单栋 =================
def test_area_quantities_follow_single_building():
    """模板/ALC/抹灰/找平/门窗/爬架都要落在"单栋单层"口径上。"""
    cfg = BASE_BEAT_CONFIGS["地上主体结构"]
    o1, _ = derive_beat_quantities(cfg, TAN)
    o12, n12 = derive_beat_quantities(cfg, TAN12)
    fa12 = standard_floor_area(TAN12)
    fa1 = standard_floor_area(TAN)
    z1 = LE._effective_zones_count(cfg, TAN)          # B2：不再写死 4，按 MSSA 算
    assert _step(o12, "铝模安装")["qty_per_floor"] == approx(fa12 * FORMWORK_AREA_FACTOR / 1)
    assert _step(o1, "铝模安装")["qty_per_floor"] == approx(fa1 * FORMWORK_AREA_FACTOR / z1)
    # 说明里要能看见"为什么数字变小了"
    assert "12 栋" in n12["detail"]["铝模安装"]["formula"]
    # 项目口径守恒：单层量 × 层数 × 分区数 × 栋数 = 总建筑面积 × 系数
    for o, p, zones in ((o1, TAN, z1), (o12, TAN12, 1)):
        n = building_count(p)
        total = _step(o, "铝模安装")["qty_per_floor"] * 38 * zones * n
        assert total == approx(TAN["total_area"] * FORMWORK_AREA_FACTOR)


def test_basement_area_not_the_whole_building():
    """面积基数与阶段层数**无关**：2 层地下室的模板不再拿整栋楼的面积去除以 2。"""
    fa = standard_floor_area(TAN12)
    b, _ = derive_beat_quantities(BASE_BEAT_CONFIGS["地下室结构"], TAN12)
    m, _ = derive_beat_quantities(BASE_BEAT_CONFIGS["地上主体结构"], TAN12)
    assert _step(b, "模板安装")["qty_per_floor"] == approx(fa * FORMWORK_AREA_FACTOR), \
        "地下室单层模板 = 单栋标准层面积 × 系数（旧口径是整栋面积÷2，放大 19 倍）"
    assert _step(b, "模板安装")["qty_per_floor"] == \
        approx(_step(m, "铝模安装")["qty_per_floor"]), "同一栋楼，地下室与主体的单层模板量应同量级"


def test_formwork_not_double_counted_across_phases():
    """全楼模板总量 = 单栋标准层面积 × 系数 × 总层数（各阶段不得重复计入整栋面积）。"""
    fa = standard_floor_area(TAN12)
    total = 0.0
    for phase, step_name, floors in (("地下室结构", "模板安装", 2),
                                     ("地上主体结构", "铝模安装", 38)):
        cfg2, _ = derive_beat_quantities(BASE_BEAT_CONFIGS[phase], TAN12)
        total += _step(cfg2, step_name)["qty_per_floor"] * floors
    assert total == approx(fa * FORMWORK_AREA_FACTOR * 40), "地下室 2 层 + 主体 38 层 = 40 层"


# ================= 5. LLM 不许猜栋数 / 层数 =================
class _StubLLM(object):
    """假的 LLM：把栋数/层数也"补全"出来，验证边界节点会不会采信。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def chat_json(self, system, user, temperature=0.3, retries=1):
        self.calls += 1
        return self.payload


def test_boundary_does_not_let_llm_invent_floors_or_building_count():
    llm = _StubLLM({"boundary_conditions": {"labor": {"peak_total": 100}},
                    "floors": 99, "building_count": 99, "total_area": 99999})
    node = BoundaryNode(llm=llm)
    ctx = {"extracted_params": {"total_area": 215000, "floors": None, "building_count": None},
           "prompt": "某项目"}
    node.run(ctx)
    p = ctx["extracted_params"]
    assert p["floors"] is None and p["building_count"] is None, \
        "栋数/层数是硬项目事实，绝不能由 LLM 结合常识补出来"
    # 对照：别的核心参数原值为空时**仍然**允许补全（常规行为没有被误伤）
    ctx2 = {"extracted_params": {"total_area": None}, "prompt": "某项目"}
    BoundaryNode(llm=llm).run(ctx2)
    assert ctx2["extracted_params"]["total_area"] == 99999


# ================= 6. 全场限额 → 单栋限额 =================
def test_scale_limits_per_building():
    limits = {"labor_total": 240, "by_trade": {"钢筋工": 60}, "equipment": {"塔吊": 12},
              "user_target": 600}
    out, notes = scale_limits_per_building(dict(limits), TAN12)
    assert out["labor_total"] == 20 and out["by_trade"]["钢筋工"] == 5
    assert out["equipment"]["塔吊"] == 1
    assert out["user_target"] == 600, "目标工期不是资源，不折算"
    assert notes and "12 栋" in notes[0]
    same, no_notes = scale_limits_per_building(dict(limits), TAN)
    assert same == limits and no_notes == [], "单栋项目不折算、不啰嗦"


# ================= 7. 排程：限额分摊 + 超编缩编 =================
def _leaf(tid, trade, quantity, duration, productivity, cap_labor):
    return {"id": tid, "name": tid, "quantity": quantity, "unit": "m3",
            "duration_days": duration, "work_type": trade,
            "norm_binding": {"task_id": tid, "mode": "labor",
                             "productivity_value": productivity,
                             "source_code": "TEST_KB", "match_type": "exact",
                             "labor_types": [trade]},
            "workface_capacity": {"max_labor": cap_labor, "unit_basis": "每施工段",
                                  "origin": "kb", "confidence": "LOW"}}


def _wbs(*rows):
    return {"phases": [{"phase": "测试", "work_packages": [
        {"id": "1.1", "name": "wp", "sub_packages": list(rows)}]}]}


def test_scheduler_scales_site_limit_to_single_building():
    """用户给"全场 24 人"，12 栋 → 单栋 2 人：限额按栋数分摊，工期 = 工日 ÷ 人数。

    ⚠️ **2026-09-21（C 组 C8-5）口径变更**：原先这里还有一句
    「设计班组按单栋人力分配」的告警 —— 它来自**已删除**的 `resolve_design_crews`
    （96 人预算 pro-rata 摊派 + 设计班组通道）。删它是**故意的**：C8-5 删掉了
    「按预算摊派班组」这条人数来源，人数只来自工作面容量 ∩ 用户同类限额。
    现在**唯一**说明单栋折算的文案是 `scale_limits_to_single_building` 的
    「全项目共 12 栋：…（单栋人工上限 2 人）——因为本计划按标准栋编制、各栋平行施工」。
    """
    wbs = _wbs(_leaf("1.1.1", "钢筋工", 20, 2, 1.0, cap_labor=10))
    out = compute_schedules(wbs, {"dependencies": []},
                            {"labor": {"peak_total": 24}}, TAN12)
    ok = out["schedule_versions"]["resource_ok"]
    row = ok["schedule"][0]
    assert row["crew"]["钢筋工"] == 2, "全场 24 人 ÷ 12 栋 = 单栋 2 人"
    assert row["ef"] - row["es"] == 10, "总工日守恒：20 人日 ÷ 2 人"
    ws = out["schedule_versions"]["warnings"]
    assert any("12 栋" in w for w in ws), "口径变化要有中文说明"
    assert any("单栋人工上限 2 人" in w for w in ws), "折算后的单栋限额要写明"


def test_scheduler_without_building_count_keeps_full_limit():
    """单栋项目：全场限额原样使用，不折算。

    ⚠️ **2026-09-21（C 组 C9 + C10）口径变更**：原先断言 `== 10`，因为旧实现里
    **工作面容量 `cap_labor` 是第一道闸门**（`班组 := min(工作面容量, 用户限额)`）。
    新链路把这两件事分开了（裁定 B/C9）：
      · **容量**只来自工作面容量口径 —— 现在是「段面积 ÷ MWI」（`capacity_source == "mwi"`）；
      · **用户限额**只来自用户**明确申报**的同类限额（`_source == "user"`），
        参与且仅参与唯一的 `min(汇总容量, 用户同类限额)`。
    本例层面积 = 215000 ÷ 1 栋 ÷ 38 层 ≈ 5658 m²，钢筋工 MWI = 12
    → 段容量 = Σ⌈段面积 ÷ 12⌉，远大于 24 → `min(段容量, 24) = 24`，
    即「单栋项目不折算、全场 24 人原样生效」—— 正是本用例的标题语义。
    """
    wbs = _wbs(_leaf("1.1.1", "钢筋工", 20, 2, 1.0, cap_labor=10))
    out = compute_schedules(wbs, {"dependencies": []},
                            {"labor": {"peak_total": 24}}, TAN)
    v = out["schedule_versions"]["resource_ok"]
    crew = v["schedule"][0]["crew"]
    assert crew["钢筋工"] == 24, "单栋不折算 → 全场 24 人原样生效（限额不是罪）"
    # 容量来源可追溯：这条走的是 MWI 新链路，不是旧的工作面容量闸门
    assert v["schedule"][0]["capacity_source"] == "mwi"
    assert v["schedule"][0]["_organization"]["user_cap"] == 24
    # 工日守恒：20 人日 ÷ 24 人 → 1 天
    assert v["schedule"][0]["ef"] - v["schedule"][0]["es"] == 1


def test_oversized_crew_is_shrunk_not_starved():
    """单班组规模超过"全项目同时在岗上限"时必须缩编，而不是永远找不到窗口。"""
    wbs = _wbs(_leaf("1.1.1", "钢筋工", 20, 2, 1.0, cap_labor=10))
    out = compute_schedules(wbs, {"dependencies": []},
                            {"labor": {"peak_total": 4}}, TAN)
    v = out["schedule_versions"]
    row = v["resource_ok"]["schedule"][0]
    assert row["crew"]["钢筋工"] == 4
    assert row["ef"] - row["es"] == 5, "20 人日 ÷ 4 人 = 5 天（总工日守恒）"
    for rec in v["resource_ok"]["daily_labor"]:
        assert rec["total"] <= 4
    assert v["theory_min"]["total_duration_days"] <= v["resource_ok"]["total_duration_days"]


# ================= 8. 编制口径要出现在计划元数据里 =================
def test_meta_exposes_caliber():
    meta = build_meta({"extracted_params": TAN12})
    assert meta["building_count"] == 12 and meta["floors"] == 38
    assert "12 栋" in meta["caliber_note"]
    meta1 = build_meta({"extracted_params": TAN})
    assert meta1["building_count"] == 1 and "单栋项目" in meta1["caliber_note"]
    meta2 = build_meta({"extracted_params": {"total_area": 215000}})
    assert "待用户确认" in meta2["caliber_note"], "层数取配置默认时必须显式标注"
