# -*- coding: utf-8 -*-
"""平行专项（`parallel_work`，节点 8 的外檐保温/外檐涂料）跨相搭接回归测试。

真实缺陷（本文件的由来）——`backend/plans/plan_sample3_after_allfix.json`：
  `8.2.1.1 外檐保温（全楼平行）` / `8.3.1.1 外檐涂料（全楼平行）`**前置与后续全空**，
  被 CPM 排到开工第 1 天（2026-06-01），9 份历史计划 9/9 复现。代码原因两段叠加：

  ① `layer_engine.expand_node` 把 `parallel_work` 的叶子挂在 `z = len(zones) + pi`
     （单分区 → 8.2.1.1 / 8.3.1.1，`_zone`=2/3），而 `structural_deps` 的搭接循环只遍历
     `range(1, len(zones)+1)` —— 平行专项叶子**结构上就拿不到任何搭接**；
  ② `beat_node.merge_beat_deps` 会删掉「两端都在节拍叶子上」的模型边（设计如此：节拍搭接
     由代码独占，有既有测试锁着）⇒ 模型即使写了 `8.1.x → 8.2.1.1` 也会被删。
     ⇒ 模型被删、代码不生成 = 永远无前置。

本文件钉住**修在 ① 的根因**（`structural_deps` 按配置生成，新增平行专项自动适用）：
  `平行专项第 1 条 ← 前阶段 lead_in 的挂接点`（与本阶段首条流水 8.1.1.1 **同一个来路、
  同一个 lag**）、`组内第 2 条 ← 第 1 条`（组内按 `parallel_work` 声明顺序相接，lag=0）；
  且不成环。期望值全部由**真配置 + 真函数**算出（不手搓 id/前置），真计划用例直接读存档计划。

⚠️ E5-a（2026-09-21）：跨相挂接点不再是"前阶段最后一片叶子 + floors_ahead×2 天"
（那等于没提前），而是 `floors_ahead=N` 时的「**前阶段第 (1+N) 层所在那一段的末工序**」、
lag=0。本文件里"同一来路、同 lag"的断言不受影响（两处都用同一个挂接点）。

⚠️ 裁定-1（同日）：跨相挂接**按分区各挂一条** —— Ⅱ区（以及 Ⅲ/Ⅳ… 区）的首段首工序
挂到前阶段**同分区**的领先挂接点（同 lag=0），与 `structural_deps` docstring 里
「第 2 个分区 = 独立起点，不依赖第 1 个区」一致；前阶段分区数不足时回落第 1 个分区的
挂接点（**任何分区都不许无前置**）。钉住它的用例见 `TestPerZoneLeadIn`。

⚠️ B2 / A7（同日）：分区口径换成 MSSA=500 m²（真计划参数 788.9 m²/层 → **2 个分区**，
旧四档是 1 个），且删除了预制「叠合板吊装」（地上主体结构每段工序数 5→4）——所以存档
里那些旧 id 会失效，`TestRealPlan` 只对"两端都还在重放后 WBS 里"的模型依赖做合并。

运行：python -m pytest backend/tests/test_parallel_lead_in.py -q -p no:cacheprovider
"""

import copy
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline import layer_engine as LE                      # noqa: E402
from pipeline.nodes import deps_gen as DG                     # noqa: E402
from pipeline.nodes.beat_configs import BASE_BEAT_CONFIGS     # noqa: E402
from pipeline.nodes.beat_node import (BEAT_PHASE_NAMES,       # noqa: E402
                                      _build_phase_leaf_map, _last_leaf_id,
                                      _zone_ladders, merge_beat_deps)

REAL_PLAN = BACKEND / "plans" / "plan_sample3_after_allfix.json"
PHASE = "装饰装修"
NODE_ID = BASE_BEAT_CONFIGS[PHASE]["node_id"]                 # "8"，不写死


# ==================== 真配置 / 真函数驱动（不碰 LLM） ====================
def _real_plan():
    return json.loads(REAL_PLAN.read_text(encoding="utf-8"))


def _real_params():
    """真计划的项目参数（单栋 18 层 / 14200 ㎡ → 标准层 788.9 ㎡ → **2 个分区**）。"""
    return copy.deepcopy(_real_plan()["meta"]["extracted_params"])


def _phase_skeleton():
    """按真计划的阶段次序搭空架子（`_build_phase_leaf_map` 按**位置**编号，
    节点 8 的 `lead_in.from_node="6"` 必须落在第 6 个阶段上，次序不能自己发明）。"""
    return [{"phase": ph["phase"], "work_packages": []}
            for ph in _real_plan()["wbs"]["phases"]]


def _drive_beat_phases(phases, params):
    """按 `beat_node.run` 的口径依次展开节拍阶段，走真配置 + 真展开/搭接函数。

    返回 `(deps, beat_leaf_ids, {phase: phase_dict})`；`deps` 是各阶段
    `structural_deps` 的累加（`beat_deps` 的来路）。
    """
    deps, ids_all, dicts = [], [], {}
    for ph in phases:
        name = ph.get("phase")
        if name not in BEAT_PHASE_NAMES:
            continue
        cfg = BASE_BEAT_CONFIGS[name]
        phase_dict, ids = LE.expand_node(cfg, params)
        ph["work_packages"] = phase_dict["work_packages"]   # 先落子树，后面的相才解析得到
        phase_leaf_map = _build_phase_leaf_map(phases)
        deps.extend(LE.structural_deps(cfg, phase_map=phase_leaf_map, params=params))
        ids_all.extend(ids)
        dicts[name] = phase_dict
    return deps, ids_all, dicts


def _leaves(phase_dict):
    return [l for wp in phase_dict["work_packages"] for l in wp.get("sub_packages") or []]


def _parallel_ids(phase_dict):
    """平行专项叶子 id，**按 `parallel_work` 声明顺序**（= 施工先后）。

    ⚠️ 第 5 批（域 4.1c）后树的第 2 层由「分区」改成「L3 工种」，叶子被收进各自的
    工种包，`work_packages` 的产出顺序 = **L3 工种号顺序** —— 而装饰装修的两个平行
    专项分属不同工种（外檐涂料 painting l3=4、外檐保温 insulation l3=6），
    所以 `expand_node` 的产出顺序**不再等于** `parallel_work` 声明顺序。
    这里按名字匹配回声明顺序，让"声明顺序 = 施工顺序"这条语义仍然被测到。
    """
    cfg_par = BASE_BEAT_CONFIGS[PHASE]["parallel_work"]
    leaves = [l for l in _leaves(phase_dict) if l.get("_parallel")]
    out = []
    for item in cfg_par:
        got = [l["id"] for l in leaves if l["name"].startswith(item["name"])]
        assert len(got) == 1, (item["name"], got)
        out.append(got[0])
    return out


def _assert_parallel_zone_shape(par, n_zones):
    """平行专项叶子 id 的形状：`{node}.{l3}.{l4}.{n_zones+1+pi}.1`（pi 从 0 起，声明顺序）。

    第 5 批后叶子 id 是 5 段（域 4）；这里只钉**分区号**这一段的语义
    （平行专项是独立分区，从 `分区数+1` 起、与 `parallel_work` 声明顺序一一对应），
    l3/l4 由配置与库决定，不手搓。
    """
    for pi, pid in enumerate(par):
        parts = pid.split(".")
        assert len(parts) == 5, pid
        assert parts[0] == NODE_ID, pid
        assert parts[3] == str(n_zones + 1 + pi), (pid, n_zones, pi)
        assert parts[4] == "1", pid


def _lead_succ(node_id, z):
    """某节拍阶段第 z 分区的**首段首工序**叶子 id：`{node}.1.1.{z}.1`。

    每个节拍分部的第 1 道工序都属第 1 个 L3 工种、是该工种内第 1 道（钢筋绑扎 / 内墙抹灰…），
    所以 l3=1、l4=1、层段=1（第 5 批 id = `分部.L3工种号.L4工序号.分区.层段`）。
    用真配置驱动时以 `_first_flow_id` 为准；搭合成 `phase_map` 时用它预测 id。
    """
    return "%s.1.1.%d.1" % (str(node_id), z)


def _first_flow_id(phase_dict):
    """本阶段第 1 条流水叶子 = 分区 1 / 首段 / 首工序（`lid(1,1,1)`）。"""
    got = [l["id"] for l in _leaves(phase_dict)
           if not l.get("_parallel") and (l.get("_zone"), l.get("_segment"), l.get("_step")) == (1, 1, 1)]
    assert len(got) == 1, got
    return got[0]


def _pred_map(deps):
    out = {}
    for d in deps:
        out.setdefault(d["successor"], set()).add(d["predecessor"])
    return out


def _lag_map(deps):
    return {(d["predecessor"], d["successor"]): d.get("lag_days") for d in deps}


def _assert_parallel_lead_in(dicts, deps):
    """公共断言：平行专项有来路、与首条流水同来路同 lag、组内按声明顺序相接。"""
    pd = dicts[PHASE]
    flow_first = _first_flow_id(pd)
    par = _parallel_ids(pd)
    cfg_parallel = BASE_BEAT_CONFIGS[PHASE]["parallel_work"]

    # 期望值来自真配置（数量、顺序、名称），不是手搓
    assert len(par) == len(cfg_parallel) >= 2, (par, cfg_parallel)
    names = [l["name"] for l in _leaves(pd) if l.get("_parallel")]
    # 第 5 批后树按 L3 工种包组织，产出顺序 ≠ `parallel_work` 声明顺序 ⇒ 逐条按名字找
    assert len(names) == len(cfg_parallel), (names, cfg_parallel)
    for cfg_item in cfg_parallel:
        matched = [n for n in names if n.startswith(cfg_item["name"])]
        assert len(matched) == 1, (cfg_item["name"], names)

    preds, lags = _pred_map(deps), _lag_map(deps)

    # 对照组：内装首条本来就有跨相来路（修复前唯一有来路的那条）
    assert preds.get(flow_first), "对照组失效：内装首条没有跨相搭接"
    # 缺陷点：平行专项第 1 条修复前**完全无前置**（会被排到开工第 1 天）
    assert preds.get(par[0]), "平行专项第 1 条必须有跨相来路（修复前为空）"
    assert preds[par[0]] == preds[flow_first], \
        "平行专项应与同阶段首条流水同一个 lead_in 来路：%s vs %s" % (preds[par[0]], preds[flow_first])
    entry = next(iter(preds[par[0]]))
    assert lags[(entry, par[0])] == lags[(entry, flow_first)], \
        "同一来路必须同 lag（E5-a 后 N>0 时挂接点自带层位、lag=0）"
    # 平行专项是独立分区，不该反过来依赖本阶段流水
    assert not any(p == flow_first for p in preds[par[0]]), preds[par[0]]

    # 组内顺序：parallel_work 声明顺序即施工先后（保温 → 涂料）
    for a, b in zip(par, par[1:]):
        assert preds.get(b) == {a}, "组内第 %s 条应接在前一条之后：%s" % (b, preds.get(b))
        assert lags[(a, b)] == 0, "组内工序相接 lag 应为 0"
    return par, flow_first, preds


# ==================== ① 单分区 / 多分区：平行专项有来路 ====================
class TestParallelLeadIn:
    def test_单分区时平行专项与内装首条同来路(self):
        """单分区现场（400 m²/层 ≤ MSSA 500 → 1 个分区）：平行专项仍有跨相来路。"""
        params = {"floors": 18, "total_area": 400 * 18, "building_count": 1}
        assert LE._effective_zones_count(BASE_BEAT_CONFIGS[PHASE], params) == 1, \
            "本用例前提是单分区（400 m²/层 ≤ MSSA 500）"
        phases = _phase_skeleton()
        deps, _, dicts = _drive_beat_phases(phases, params)
        par, flow_first, preds = _assert_parallel_lead_in(dicts, deps)

        # 单分区 → 平行专项落在独立分区 2/3（区号从 zones 数推导，不写死）
        _assert_parallel_zone_shape(par, 1)
        assert flow_first == _lead_succ(NODE_ID, 1)
        # 来路就是节点 6（二次结构与砌体）的领先段末工序，与 8.1.1.1.1 完全一致
        assert preds[par[0]] == preds[flow_first]

    def test_真计划参数下平行专项与内装首条同来路(self):
        """真计划参数（14200 m² / 18 层 → 标准层 788.9 m²）。

        ⚠️ B2（2026-09-21，分区口径换 MSSA=500 m²）后这里给出 **2 个分区**
        （旧四档口径给 1 个）→ 平行专项的分区号随之变为 3/4。
        这不是缺陷：分区数由面积口径决定，本用例钉的是"同一来路同 lag"不变。
        """
        params = _real_params()
        n_zones = LE._effective_zones_count(BASE_BEAT_CONFIGS[PHASE], params)
        assert n_zones == 2, "788.9 m² → ceil(788.9/500)=2、余量 288.9 ≥ 166.67 → 2 段"
        phases = _phase_skeleton()
        deps, _, dicts = _drive_beat_phases(phases, params)
        par, flow_first, preds = _assert_parallel_lead_in(dicts, deps)

        _assert_parallel_zone_shape(par, n_zones)
        assert flow_first == _lead_succ(NODE_ID, 1)
        assert preds[par[0]] == preds[flow_first]

    def test_多分区时平行专项同样有来路(self):
        """4 个分区时平行专项挂在 z=5/6，搭接规则不变。"""
        params = {"floors": 20, "total_area": 100000}       # 标准层 5000 ㎡ → 4 个分区
        n_zones = LE._effective_zones_count(BASE_BEAT_CONFIGS[PHASE], params)
        assert n_zones >= 2, "本用例前提是多分区，实际 %s" % n_zones
        phases = _phase_skeleton()
        deps, _, dicts = _drive_beat_phases(phases, params)
        par, flow_first, preds = _assert_parallel_lead_in(dicts, deps)

        _assert_parallel_zone_shape(par, n_zones)
        assert flow_first == _lead_succ(NODE_ID, 1)

    def test_平行搭接不成环且端点都是真叶子(self):
        """结构搭接自洽：无环、每条边端点都在本阶段的叶子集合里。"""
        for params in (_real_params(), {"floors": 20, "total_area": 100000}, {}):
            phases = _phase_skeleton()
            deps, ids, _ = _drive_beat_phases(phases, params)
            assert not DG.has_cycle(deps, ids), "平行搭接不得成环（params=%s）" % params
            for d in deps:
                assert d["predecessor"] in set(ids), d
                assert d["successor"] in set(ids), d

    def test_组内顺序按配置声明_保温在涂料之前(self):
        """声明顺序 = 施工顺序：`parallel_work[0]`（外檐保温）→ `[1]`（外檐涂料）。"""
        cfg = BASE_BEAT_CONFIGS[PHASE]
        assert [p["name"] for p in cfg["parallel_work"]] == ["外檐保温", "外檐涂料"], \
            "配置声明顺序变了就要重新想清楚谁先谁后（本用例的语义基础）"
        phases = _phase_skeleton()
        deps, _, dicts = _drive_beat_phases(phases, _real_params())
        par = _parallel_ids(dicts[PHASE])
        lags = _lag_map(deps)
        assert (par[0], par[1]) in lags, "外檐保温 → 外檐涂料 必须显式相接"
        assert (par[1], par[0]) not in lags, "反向边不许存在"
        assert lags[(par[0], par[1])] == 0

    def test_from_node指回本阶段时组内链不成环(self):
        """畸形配置的兜底：`lead_in.from_node` 指向本阶段（target = 自己的收尾叶子）时，
        入口不能是这条链自己的下游（否则 入口→组内第1条→…→入口 立刻成环）。"""
        cfg = copy.deepcopy(BASE_BEAT_CONFIGS[PHASE])
        cfg["lead_in"] = {"from_node": NODE_ID, "floors_ahead": 3}
        phases = _phase_skeleton()
        # 手搓这一步：只展开本阶段（其余阶段留空），phase_map 里本阶段已有叶子
        phase_dict, ids = LE.expand_node(cfg, _real_params())
        phases[int(NODE_ID) - 1]["work_packages"] = phase_dict["work_packages"]
        deps = LE.structural_deps(cfg, phase_map=_build_phase_leaf_map(phases),
                                  params=_real_params())
        assert not DG.has_cycle(deps, ids), \
            "畸形 lead_in 不得让平行组内链成环：%s" % [d for d in deps if ".2.1.1" in d["successor"]]


# ==================== ①b 裁定-1：跨相搭接按分区各挂一条 ====================
class TestPerZoneLeadIn:
    """裁定-1（2026-09-21）：`floors_ahead` 的跨相挂接点**每个分区各挂一条**。

    修前：只有第 1 个分区（Ⅰ区）挂到 lead_in 的领先挂接点，Ⅱ区首段首工序**没有**
    结构来路 ⇒ 落到 `deps_gen.ensure_dependencies` 的孤儿补边上拿一条语义可疑的
    "上一阶段收尾"（实测 `8.2.1.1 ← 7.4.6` 机电安装尾）。这是**区间的处理不对称**。

    修后：Ⅱ区 ← 前阶段**同分区**第 (1+floors_ahead) 层所在段的末工序（与 Ⅰ区同来路、
    同 lag=0）；前阶段分区数不足 → 回落第 1 个分区的挂接点；**任何分区都不许无前置**。
    """

    def test_多分区时每个分区各挂一条_来路是同分区领先段(self):
        params = _real_params()
        phases = _phase_skeleton()
        deps, ids, dicts = _drive_beat_phases(phases, params)
        preds, lags = _pred_map(deps), _lag_map(deps)

        checked = 0
        for i, ph in enumerate(phases, 1):
            name = ph["phase"]
            if name not in BEAT_PHASE_NAMES:
                continue
            cfg = BASE_BEAT_CONFIGS[name]
            lead = cfg.get("lead_in") or {}
            from_node = lead.get("from_node")
            if not from_node:
                continue
            ahead = lead.get("floors_ahead") or 0
            n_zones = LE._effective_zones_count(cfg, params)
            assert n_zones >= 2, "本用例前提是多分区，实际 %s（%s）" % (n_zones, name)

            src = phases[int(from_node) - 1]
            if src.get("phase") not in BEAT_PHASE_NAMES:
                continue               # 前阶段不是节拍阶段：本用例的架子没给它叶子（另有用例覆盖）
            ladders = _zone_ladders(src)
            last = _last_leaf_id(src)
            assert last, "前阶段已被展开，末叶不该为空：%s" % src["phase"]
            for z in range(1, n_zones + 1):
                succ = _lead_succ(i, z)
                segs = ladders.get(z) or []
                if ahead > 0 and segs:
                    want = 1.0 + float(ahead)
                    seg = next((s for s in segs
                                if (s.get("end_floor") or 0) + 1e-9 >= want), segs[-1])
                    expect = seg["leaf"]
                else:
                    expect = last          # N=0 / 非节拍前阶段 → 前阶段末叶
                assert preds.get(succ) == {expect}, \
                    "分区 %d 首段首工序 %s 的来路应为同分区领先挂接点 %s：%s" % (
                        z, succ, expect, preds.get(succ))
                assert lags[(expect, succ)] == 0, "领先由挂接点层位表达，lag 必须为 0"
                checked += 1
        assert checked >= 6, checked      # 真参数下 4 个节拍阶段，其中 3 个带 lead_in

    def test_每条分区首段首工序都有跨相来路_无一无前置(self):
        """硬约束：**不许**让任何分区的首段首工序变成无前置（会被排到开工第 1 天）。

        只看**前阶段也是节拍阶段**的跨相搭接（前阶段非节拍时本架子没给它叶子，
        那条来路由真计划的模型依赖负责，不在本文件的合成架子里）。
        """
        params = _real_params()
        phases = _phase_skeleton()
        deps, ids, dicts = _drive_beat_phases(phases, params)
        preds = _pred_map(deps)
        missing, checked = [], 0
        for i, ph in enumerate(phases, 1):
            name = ph.get("phase")
            if name not in BEAT_PHASE_NAMES:
                continue
            cfg = BASE_BEAT_CONFIGS[name]
            from_node = (cfg.get("lead_in") or {}).get("from_node")
            if not from_node:
                continue               # 没有 lead_in 的阶段本来就不参与跨相搭接
            if phases[int(from_node) - 1].get("phase") not in BEAT_PHASE_NAMES:
                continue
            n_zones = LE._effective_zones_count(cfg, params)
            for z in range(1, n_zones + 1):
                succ = _lead_succ(i, z)
                if not preds.get(succ):
                    missing.append(succ)
                else:
                    checked += 1
        assert not missing, "这些分区首段首工序无跨相来路：%s" % missing
        assert checked >= 6, checked

    def test_前阶段分区数不足时回落第1分区挂接点(self):
        """前阶段只有 1 个分区（或只有第 1 分区的阶梯）→ 各分区都回落它的挂接点。"""
        cfg = BASE_BEAT_CONFIGS["装饰装修"]          # floors_ahead=3、多分区
        params = _real_params()
        phase_map = {"6": {"last": "6.9.9.9",
                           "zone_segments": {1: [{"segment": 1, "end_floor": 1.0, "leaf": "6.1.1.4"},
                                                 {"segment": 4, "end_floor": 4.0, "leaf": "6.1.4.4"},
                                                 {"segment": 18, "end_floor": 18.0, "leaf": "6.1.18.4"}]}}}
        deps = LE.structural_deps(cfg, phase_map=phase_map, params=params)
        preds = _pred_map(deps)
        assert preds[_lead_succ(NODE_ID, 1)] == {"6.1.4.4"}, preds[_lead_succ(NODE_ID, 1)]
        assert preds[_lead_succ(NODE_ID, 2)] == {"6.1.4.4"}, preds[_lead_succ(NODE_ID, 2)]
        assert "6.9.9.9" not in {p for s in preds.values() for p in s}, \
            "有第 1 分区阶梯时不该退到末叶（那会把 Ⅱ区无谓地拖到前阶段全部完工）"

    def test_非节拍前阶段时各分区都挂前阶段末叶(self):
        """前阶段根本不是节拍阶段（无任何分区阶梯）→ 各分区挂前阶段末叶（原串行语义）。"""
        cfg = BASE_BEAT_CONFIGS["二次结构与砌体"]      # floors_ahead=3
        params = _real_params()
        deps = LE.structural_deps(cfg, phase_map={"5": {"last": "5.3.2.1", "zone_segments": {}}},
                                  params=params)
        preds = _pred_map(deps)
        for z in (1, 2):
            assert preds[_lead_succ(6, z)] == {"5.3.2.1"}, preds.get(_lead_succ(6, z))

    def test_旧字符串形状只挂第1个分区且保留lag(self):
        """向后兼容：`phase_map` 还是裸字符串（旧调用方）→ 只挂第 1 个分区、lag=N×2。"""
        cfg = BASE_BEAT_CONFIGS["二次结构与砌体"]
        deps = LE.structural_deps(cfg, phase_map={"5": "5.2.18.5"}, params=_real_params())
        got = [d for d in deps if d["successor"] == _lead_succ(6, 1)]
        assert [(d["predecessor"], d["successor"], d["lag_days"]) for d in got] == \
            [("5.2.18.5", "6.1.1.1.1", 6)], got

    def test_from_node指回本阶段时所有分区都不接(self):
        """畸形配置（lead_in 指回自己）：每个分区的挂接点都落在本阶段内 → 整条作废、不成环。"""
        cfg = copy.deepcopy(BASE_BEAT_CONFIGS["装饰装修"])
        cfg["lead_in"] = {"from_node": NODE_ID, "floors_ahead": 3}
        phases = _phase_skeleton()
        phase_dict, ids = LE.expand_node(cfg, _real_params())
        phases[int(NODE_ID) - 1]["work_packages"] = phase_dict["work_packages"]
        deps = LE.structural_deps(cfg, phase_map=_build_phase_leaf_map(phases),
                                  params=_real_params())
        assert not DG.has_cycle(deps, ids), deps
        for z in (1, 2):
            assert not any(d["successor"] == _lead_succ(NODE_ID, z) and str(d["predecessor"]).startswith("8.")
                           for d in deps), deps


# ==================== ② 真计划复核（真 WBS + 真依赖 + 孤儿守卫） ====================
class TestRealPlan:
    """真计划 `plan_sample3_after_allfix.json`（历史产物：单分区 / 每段 5 道工序）：重放后
    平行专项必须已有前置，且**孤儿守卫不再为它们触发**（`3.3.1`/`3.3.2` 是另一条根因，
    仍由守卫负责）。重放用的分区数与工序数按**当前配置**算（B2/A7 后已变，见文件头）。"""

    @classmethod
    def setup_class(cls):
        raw = _real_plan()
        cls.params = copy.deepcopy(raw["meta"]["extracted_params"])
        cls.phases = copy.deepcopy(raw["wbs"]["phases"])
        cls.saved_parallel = [l["id"] for ph in cls.phases
                              for wp in ph.get("work_packages") or []
                              for l in wp.get("sub_packages") or []
                              if l.get("_parallel")]

    def _fixed_graph(self):
        """修后的节拍搭接 + 真计划原有的模型依赖，按 `merge_beat_deps` 合并（真口径）。

        ⚠️ A7 / B2 之后节拍叶子的 id 与分区数都变了（A7 删了预制「叠合板吊装」→ 每段
        工序数 5→4；B2 换 MSSA 口径 → 分区数 1→2），所以存档里指向**旧节拍叶子**的模型
        依赖已经失效。这里先按"两端都还在重放后 WBS 里"过滤，再做合并 —— 这样测的是
        「节拍搭接 + 仍有效的模型依赖」的真实合并结果，而不是让失效边把测试带偏。
        """
        deps, ids, dicts = _drive_beat_phases(self.phases, self.params)
        leaves = DG.collect_leaf_ids({"phases": self.phases})
        live = set(leaves)
        saved = [d for d in _real_plan()["dependencies"]
                 if str(d.get("predecessor")) in live and str(d.get("successor")) in live]
        merged = merge_beat_deps(saved, deps, ids, leaves)
        return merged, dicts, leaves

    def test_真计划里带_parallel_的就是这两条平行叶子(self):
        """真存档里带 `_parallel` 的正是这两条（历史产物：单分区）。

        重放后分区数由 B2 的新口径决定（本参数 → 2 区），所以平行叶子的**区号**相应
        后移（分区号 3/4，第 5 批后 id 是 5 段）——**数量与声明顺序**不变，这是本条要钉住的。
        """
        assert self.saved_parallel == ["8.2.1.1", "8.3.1.1"], self.saved_parallel
        _, dicts, _ = self._fixed_graph()
        par = _parallel_ids(dicts[PHASE])
        n_zones = LE._effective_zones_count(BASE_BEAT_CONFIGS[PHASE], self.params)
        _assert_parallel_zone_shape(par, n_zones)
        assert len(par) == len(self.saved_parallel)

    def test_真计划复核_平行专项前置非空且同来路(self):
        merged, dicts, leaves = self._fixed_graph()
        preds = _pred_map(merged)
        par = _parallel_ids(dicts[PHASE])
        flow_first = _first_flow_id(dicts[PHASE])

        assert not DG.has_cycle(merged, leaves), "合并真依赖后不得有环"
        assert preds.get(par[0]), "%s 外檐保温必须有前置（修复前为空 → 开工第 1 天）" % par[0]
        assert preds.get(par[1]) == {par[0]}, preds.get(par[1])
        assert preds[par[0]] == preds[flow_first], \
            "外檐专项必须与内装首条同一来路：%s vs %s" % (preds[par[0]], preds[flow_first])

    def test_守卫不再为平行专项触发(self):
        """根因修好后，结构依赖里已经有了 ⇒ 守卫不必再补、也不该重复报。"""
        merged, dicts, leaves = self._fixed_graph()
        par = _parallel_ids(dicts[PHASE])
        new, warns, applied = DG.ensure_dependencies(merged, {"phases": self.phases})
        blob = json.dumps(warns, ensure_ascii=False)

        for pid in par:
            assert pid not in blob, "守卫仍在为 %s 报/补：%s" % (pid, blob)
        assert not [a for a in applied if a["successor"] in tuple(par)], applied
        # 边界：3.3.1/3.3.2 回填类是**另一条根因**（不在本次范围），守卫仍要补、仍要点名
        assert [a for a in applied if a["successor"] == "3.3.1"], applied
        assert [a for a in applied if a["successor"] == "3.3.2"], applied
        assert "3.3.1" in blob and "3.3.2" in blob, warns
        assert not DG.has_cycle(new, leaves)
        # 后置：守卫补边不得反过来给平行专项塞第二条前置
        preds = _pred_map(new)
        assert preds[par[1]] == {par[0]}, preds[par[1]]


if __name__ == "__main__":
    import inspect

    classes = [v for k, v in sorted(globals().items())
               if k.startswith("Test") and inspect.isclass(v)]
    for fn in [v for k, v in sorted(globals().items())
               if k.startswith("test_") and inspect.isfunction(v)]:
        fn()
        print("  PASS  %s" % fn.__name__)
    for cls in classes:
        if hasattr(cls, "setup_class"):
            cls.setup_class()
        obj = cls()
        for name in sorted(dir(obj)):
            if name.startswith("test_"):
                getattr(obj, name)()
                print("  PASS  %s.%s" % (cls.__name__, name))
    print("全部 parallel_lead_in 用例通过 ✔")
