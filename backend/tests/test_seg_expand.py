# -*- coding: utf-8 -*-
"""一层一段（v2.2）专项回归 —— 竖向分段缺陷修正

背景：旧配置把「地上主体结构」等按 8 段 × 5 层切，生成的依赖是
「1-5层钢筋绑扎 → 1-5层铝模安装 → 1-5层混凝土浇筑」——要求 1~5 层钢筋全绑完
才支 1~5 层模板，实际做不到（模板未支、上层无作业面）。修正后竖向施工层 = 楼层。

覆盖：
  1. 地上主体结构：floors=38 → 38 段 × 1 层（一层一段）
  2. 地下室结构：floors_locked 生效 → 4 段 × 0.5 层，∑层数 == 2（项目 99 层也不变）
  3. 装饰装修：3 层一组（连续上移分部）
  4. 层数守恒：每种配置在 38 层 / 18 层下 ∑(end-start) == 有效层数
  5. suggest_zones()：单一 MSSA=500 m² 口径（方案 §4.1，B2 委派 segment_plan）；取不到面积返回 None
  6. 叶子 id 仍是纯数字点分（\\d+(\\.\\d+)+）
  7. 节拍叶子带 kb_activity_id（配置声明过的工序逐条比对，含混凝土类）
  8. clamp 上限放宽到 90：单段量大 → 工期 > 20（不再被 20 压死）
  9. 结构搭接仍然无环（Kahn），且跨段边 = 相邻楼层

运行：python -m pytest backend/tests/test_seg_expand.py -q
"""

import copy
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from pipeline import layer_engine as LE
from pipeline.nodes.beat_configs import (
    BASE_BEAT_CONFIGS,
    normalize_vertical_split,
    resolve_l4_id,
    segment_floors,
    standard_floor_area,
    suggest_zones,
    suggest_zones_from_params,
)
from pipeline.nodes.beat_node import BeatExpandNode

PARAMS_38 = {"floors": 38}
PARAMS_AREA = {"floors": 38, "total_area": 301354.26}     # 标准层 ≈ 7930 ㎡
ID_RE = re.compile(r"^\d+(\.\d+)+$")


def _leaves(ph):
    return [l for wp in ph["work_packages"] for l in wp["sub_packages"]]


def _flow_leaves(ph):
    """排除全楼平行专项（外檐）——它们不参与竖向流水。"""
    return [l for l in _leaves(ph) if not l.get("_parallel")]


def _segs(cfg, params):
    floors = LE._eff_floors(cfg, params)
    return floors, segment_floors(floors, int(cfg.get("segments") or 1),
                                  per=cfg.get("floors_per_segment"))


# ---------------- 1. 主体结构：一层一段 ----------------
def test_main_structure_one_floor_per_segment():
    cfg = BASE_BEAT_CONFIGS["地上主体结构"]
    assert cfg["floors_per_segment"] == 1, "主体结构必须一层一段"

    floors, segs = _segs(cfg, PARAMS_38)
    assert floors == 38.0
    assert len(segs) == 38, "38 层应切 38 段（段数由层数推导，不写死 8 段）"
    assert all(abs(b - a - 1.0) < 1e-9 for a, b in segs), "每段恰好 1 层"
    assert segs[0] == (1.0, 2.0) and segs[-1] == (38.0, 39.0)

    # 段数随项目层数走：18 层 → 18 段
    _, segs18 = _segs(cfg, {"floors": 18})
    assert len(segs18) == 18

    # 展开后的区位标签：只有「1-1层」这种单层区间，不再出现「1-5层」
    ph, _ = LE.expand_node(cfg, PARAMS_38)
    zone1 = [l["name"] for l in _flow_leaves(ph) if l["_zone"] == 1]
    assert any("1-1层" in n for n in zone1)
    assert not any("1-5层" in n for n in zone1)


def test_secondary_structure_one_floor_per_segment():
    cfg = BASE_BEAT_CONFIGS["二次结构与砌体"]
    assert cfg["floors_per_segment"] == 1
    floors, segs = _segs(cfg, PARAMS_38)
    assert len(segs) == 38 and all(abs(b - a - 1.0) < 1e-9 for a, b in segs)


# ---------------- 2. 地下室：floors_locked 仍然 4 段 × 0.5 层 ----------------
def test_basement_locked_four_half_floor_segments():
    cfg = BASE_BEAT_CONFIGS["地下室结构"]
    assert cfg.get("floors_locked") is True
    for params in ({"floors": 38}, {"floors": 99}):          # 项目层数不影响地下室
        floors, segs = _segs(cfg, params)
        assert floors == 2.0, "floors_locked 生效：地下室恒 2 层"
        assert len(segs) == 4
        assert all(abs(b - a - 0.5) < 1e-9 for a, b in segs), "底板/墙柱/顶板分层浇筑"
        assert abs(sum(b - a for a, b in segs) - 2.0) < 1e-9, "∑层数 == 2"


# ---------------- 3. 装饰装修：3 层一组 ----------------
def test_decoration_three_floors_per_segment():
    cfg = BASE_BEAT_CONFIGS["装饰装修"]
    assert cfg["floors_per_segment"] == 3, "连续上移分部允许 3 层一组"
    floors, segs = _segs(cfg, PARAMS_38)
    assert floors == 38.0
    assert len(segs) == 13, "38 层 ÷ 3 = 13 段（12×3 + 2）"
    assert [round(b - a, 1) for a, b in segs] == [3.0] * 12 + [2.0]
    ph, _ = LE.expand_node(cfg, PARAMS_38)
    zone1 = [l["name"] for l in _flow_leaves(ph) if l["_zone"] == 1]
    assert any("1-3层" in n for n in zone1)


# ---------------- 4. 层数守恒（38 层 / 18 层） ----------------
@pytest.mark.parametrize("floors", [38, 18])
def test_floor_conservation_all_configs(floors):
    params = {"floors": floors}
    for name, cfg in BASE_BEAT_CONFIGS.items():
        eff, segs = _segs(cfg, params)
        total = sum(b - a for a, b in segs)
        assert abs(total - eff) < 1e-9, (name, total, eff)

        # 展开后按「段高 = 单段量 / 单层量」复核一次，确保叶子真的铺满。
        # v2.3：单层量由项目参数推算（leaf["_qty_per_floor"]，已 round 到两位小数），
        # 不再等于配置里写死的 qty_per_floor —— 用叶子上记录的实际单层量做分母才对得上。
        # ⚠️ 容差为什么是 3%：叶子量 = round(单层量 × 层数)，**0.5 层一段**时每段的取整
        # 误差最大 = 0.5 ÷ 单层量，地下室钢筋 27.5→28 即 0.5/55 ≈ 0.9%/段，4 段累计 ≈ 1.8%
        # （一层一段的取整误差 ≤ 1/单层量，占比极小）。这是"半层段"本身的分辨率下限，
        # 不是铺不满：真正的缺陷（段数错、单层量用错、量纲错）偏差都在几十个百分点，
        # 3% 一样拦得住。
        ph, ids = LE.expand_node(cfg, params)
        heights = {}
        for l in _flow_leaves(ph):
            if l.get("_step") == 1 and l["_zone"] == 1:
                heights[l["_segment"]] = l["quantity"] / float(l["_qty_per_floor"])
        assert len(heights) == len(segs), (name, len(heights), len(segs))
        assert abs(sum(heights.values()) - eff) < 0.03 * eff, (name, sum(heights.values()), eff)


# ---------------- 5. suggest_zones：MSSA=500 m² 单一口径 + 取不到面积 → None ----------------
# B2（2026-09-21）：旧的四档经验阈值（<800→1 / 800~1500→2 / 1500~2500→3 / >2500→4）
# **已废止**，改为委派 `pipeline/segment_plan.py` 的 §4.1 规则：
#     n = ceil(层面积 ÷ 500)；先满后余；余量 < 500/3 ≈ 166.67 时弃用 MSSA、段数减一均匀切。
def test_suggest_zones_mssa_single_value():
    assert suggest_zones(500) == 1        # ≤ MSSA → 1 段（余量判定不适用）
    assert suggest_zones(1200) == 3       # ceil(2.4)=3，余量 200 ≥ 166.67 → 先满后余
    assert suggest_zones(2000) == 4       # ceil(4)=4，余量恰为 500
    assert suggest_zones(4000) == 8       # ceil(8)=8，段数不设上限（旧口径封顶 4）


def test_suggest_zones_boundaries():
    # 旧阈值 800/1500/2500 不再有特殊含义；这里钉的是新口径的几处边界
    assert suggest_zones(800) == 2        # ceil(1.6)=2，余量 300
    assert suggest_zones(1500) == 3       # ceil(3)=3，余量 500
    assert suggest_zones(2500) == 5       # ceil(5)=5，余量 500
    assert suggest_zones(2500.5) == 5     # ceil(5.001)=6，余量 0.5 < 166.67 → 弃用 MSSA，5 段均匀切


def test_suggest_zones_none_when_area_unknown():
    assert suggest_zones(None) is None, "取不到面积必须返回 None，不瞎猜"
    assert suggest_zones(0) is None
    assert standard_floor_area(None) is None
    assert standard_floor_area({}) is None
    assert standard_floor_area({"total_area": 100000}) is None      # 缺 floors
    assert standard_floor_area({"floors": 20}) is None              # 缺 total_area
    assert suggest_zones_from_params({}) is None
    assert suggest_zones_from_params({"floors": 38}) is None


def test_suggest_zones_from_params_and_expand():
    params = {"floors": 20, "total_area": 100000}                   # 标准层 5000 m²
    assert standard_floor_area(params) == 5000.0
    assert suggest_zones_from_params(params) == 10                  # ceil(5000/500)=10
    assert suggest_zones_from_params(PARAMS_AREA) == 16             # 7930 m²/层 → 16 段

    cfg = BASE_BEAT_CONFIGS["地上主体结构"]
    # 有面积 → 用建议的平面段数（配置 2 区 → 建议 16 区；段数不设上限，裁定 9）
    assert LE._effective_zones_count(cfg, PARAMS_AREA) == 16
    ph, ids = LE.expand_node(cfg, PARAMS_AREA)
    assert {l["_zone"] for l in _flow_leaves(ph)} == set(range(1, 17))
    # 无面积 → 沿用配置 zones（AI 默认，须让用户可改）
    assert LE._effective_zones_count(cfg, PARAMS_38) == len(cfg["zones"])
    # 小面积 → 建议段数少于配置区数时只取前 N 区
    # （600 m²/层 → ceil(1.2)=2、余量 100 < 166.67 → 弃用 MSSA → 1 区）
    small = {"floors": 38, "total_area": 38 * 600}                  # 600 m²/层 → 1 区
    assert LE._effective_zones_count(cfg, small) == 1


# ---------------- 6. 叶子 id 仍是纯数字点分 ----------------
def test_leaf_ids_numeric_dotted():
    for name, cfg in BASE_BEAT_CONFIGS.items():
        ph, ids = LE.expand_node(cfg, PARAMS_38)
        assert ids, name
        for i in ids:
            assert ID_RE.match(i), (name, i)
            # 第 5 批（域 4）：`分部.L3工种号.L4工序号.分区.层段`（原 4 段的第 4 段工序号
            # 展开成 (l3,l4)，分区/层段原地保留）
            assert len(i.split(".")) == 5, (name, i)


# ---------------- 7. kb_activity_id 不丢 ----------------
def test_beat_leaves_keep_kb_activity_id():
    all_leaves = []
    for name, cfg in BASE_BEAT_CONFIGS.items():
        ph, ids = LE.expand_node(cfg, PARAMS_38)
        leaves = _leaves(ph)
        all_leaves.extend(leaves)
        # 第 5 批（域 3.1）：`kb_activity_id` 不再写死在配置里，改成「L3 键 + L4 中文名」，
        # 编号由运行时从知识库 `L4_Activity_Dictionary` 反查（本用例用同一个公开函数算期望值）
        declared = [s for s in (cfg.get("cycle") or []) + (cfg.get("attach_measures") or [])
                    if s.get("l4_name") or s.get("kb_activity_id")]
        assert declared, name
        for s in declared:
            got = [l for l in leaves if (l.get("_step_name") or "") == s["name"]]
            assert got, (name, s["name"])
            want = s.get("kb_activity_id") or resolve_l4_id(s.get("work_type_id"),
                                                            s.get("l4_name"))
            assert want, (name, s["name"], "该 L3 键 + L4 名在库里查不到")
            for l in got:
                assert l.get("kb_activity_id") == want, (name, s["name"],
                                                         l.get("kb_activity_id"), want)
        # 全楼平行专项（外檐保温/涂料）也带 KB 编号
        for p in cfg.get("parallel_work") or []:
            want = p.get("kb_activity_id") or resolve_l4_id(p.get("work_type_id"),
                                                            p.get("l4_name"))
            assert want, (name, p["name"])
            pl = [l for l in leaves if l.get("_parallel") and p["name"] in l["name"]]
            assert pl and all(l.get("kb_activity_id") == want for l in pl), name
    # 四类工序里至少混凝土类要有
    assert any((l.get("kb_activity_id") or "").startswith("CONC") for l in all_leaves)


# ---------------- 8. clamp 上限放宽到 90 ----------------
def test_clamp_upper_bound_relaxed_beyond_20():
    # 真实场景：装饰装修 3 层一组抹灰 8600×3 ÷ 480 ≈ 54 天 —— 旧上限 20 会把它压平
    cfg = BASE_BEAT_CONFIGS["装饰装修"]
    ph, _ = LE.expand_node(cfg, PARAMS_38)
    durs = [l["duration_days"] for l in _flow_leaves(ph)
            if (l.get("_step_name") or "") == "内墙抹灰"]
    assert durs and max(durs) > 20, durs
    assert max(durs) <= LE.CLAMP_MAX


def test_clamp_caps_only_pathological_quantity():
    big = copy.deepcopy(BASE_BEAT_CONFIGS["地上主体结构"])
    big["cycle"] = [dict(big["cycle"][1], qty_per_floor=10 ** 7)]   # 铝模 1000 万 m²/层
    big["attach_measures"] = []
    ph, _ = LE.expand_node(big, PARAMS_38)
    d = max(l["duration_days"] for l in _flow_leaves(ph))
    assert d > 20, "上限放宽后单段量大不再被 20 压死"
    assert d == LE.CLAMP_MAX, "上限只用来防异常，兜到 90"


# ---------------- 9. 结构搭接无环 + 跨段 = 相邻楼层 ----------------
def test_structural_deps_acyclic_and_adjacent_floors():
    for name, cfg in BASE_BEAT_CONFIGS.items():
        ph, ids = LE.expand_node(cfg, PARAMS_AREA)
        deps = LE.structural_deps(cfg, params=PARAMS_AREA)
        dur = {l["id"]: l["duration_days"] for l in _leaves(ph)}

        indeg = {i: 0 for i in dur}
        adj = {i: [] for i in dur}
        for d in deps:
            if d["predecessor"] in adj and d["successor"] in indeg:
                adj[d["predecessor"]].append(d["successor"])
                indeg[d["successor"]] += 1
        q = [i for i, v in indeg.items() if v == 0]
        cnt = 0
        while q:
            u = q.pop()
            cnt += 1
            for v in adj[u]:
                indeg[v] -= 1
                if indeg[v] == 0:
                    q.append(v)
        assert cnt == len(dur), name + " 依赖有环"

    # 同段工序串行：1层钢筋 → 1层模板；跨段 = 相邻楼层（1层 → 2层），不再是「1-5层 → 6-10层」
    # 第 5 批（域 4）后 id = `分部.L3工种号.L4工序号.分区.层段`：
    #   钢筋绑扎 = (分部5, l3=1, l4=1)、铝模安装 = (l3=2, l4=1) —— 都在 1 区 1 段
    deps = LE.structural_deps(BASE_BEAT_CONFIGS["地上主体结构"], params=PARAMS_AREA)
    edges = {(d["predecessor"], d["successor"]) for d in deps}
    assert ("5.1.1.1.1", "5.2.1.1.1") in edges       # 同段串行：钢筋 → 模板
    assert ("5.1.1.1.1", "5.1.1.1.2") in edges       # 跨段：1层 → 2层（同工序，层段+1）
    assert ("5.1.1.1", "5.1.2.2") not in edges      # 不同工序不跨段串行


# ---------------- 附：竖向口径归一（防 LLM 细化退回 5 层一段） ----------------
def test_normalize_vertical_split_blocks_old_config():
    main = copy.deepcopy(BASE_BEAT_CONFIGS["地上主体结构"])
    main["segments"], main["floors_per_segment"] = 8, 5          # 旧缺陷配置
    normalize_vertical_split("地上主体结构", main, floors=38)
    assert main["floors_per_segment"] == 1 and main["segments"] == 38

    dec = copy.deepcopy(BASE_BEAT_CONFIGS["装饰装修"])
    dec["segments"], dec["floors_per_segment"] = 38, 1
    normalize_vertical_split("装饰装修", dec, floors=38)
    assert dec["floors_per_segment"] == 3 and dec["segments"] == 13

    base = copy.deepcopy(BASE_BEAT_CONFIGS["地下室结构"])
    normalize_vertical_split("地下室结构", base, floors=38)
    assert base["floors_per_segment"] == 0.5 and base["segments"] == 4


def test_beat_node_refined_config_enforces_one_floor():
    raw = {"segments": 8, "floors_per_segment": 5.0,
           "cycle": copy.deepcopy(BASE_BEAT_CONFIGS["地上主体结构"]["cycle"])}
    base = copy.deepcopy(BASE_BEAT_CONFIGS["地上主体结构"])
    out = BeatExpandNode._refined_config(raw, base, "地上主体结构", PARAMS_38)
    assert out["floors_per_segment"] == 1 and out["segments"] == 38


if __name__ == "__main__":
    import inspect
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and inspect.isfunction(v)]
    for fn in fns:
        try:
            fn()
        except TypeError:
            fn(38)
            fn(18)
        print("  PASS  " + fn.__name__)
    print("全部 seg_expand 用例通过 ✔")
