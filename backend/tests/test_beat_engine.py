"""节拍引擎测试 — T-12 / v2.2（一层一段）

覆盖 layer_engine / beat_node 的确定性部分（全部用桩，不碰云端 LLM）：
  - 段切分：38 层 ÷ 1 层/段 → 38 段；装饰装修 3 层一组 → 13 段；地下室 2/0.5 → 4 半段；层数守恒
  - 节拍工期 clamp(ceil(单段量/日产能),[2,90])；代码算，覆盖 LLM 拍的 days
  - 叶子 id=p.z.s.k 全数字点分，normalize_wbs 往返幂等不重编号
  - 结构搭接无环 + CPM 正向工期 < 等长串行（流水 < 串行）
  - 量级守恒 ±20%
  - 集成：BeatExpandNode→DepsGenNode(merge)→CPM，覆盖全部节拍叶子 id

运行：python -m pytest backend/tests/test_beat_engine.py -v
"""

import copy
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from pipeline import layer_engine as LE
from pipeline.nodes.beat_configs import BASE_BEAT_CONFIGS, segment_floors
from pipeline.nodes.beat_node import BeatExpandNode
from pipeline.nodes.wbs_gen import normalize_wbs


PARAMS = {"building_type": "剪力墙住宅", "structure_type": "剪力墙",
          "area": 8000, "floors": 38, "total_area": 301354.26}


def _leaf_dicts(ph):
    return [l for wp in ph["work_packages"] for l in wp["sub_packages"]]


# ---------------- 段切分（竖向：结构类一层一段） ----------------
def test_segment_split_one_floor_per_segment():
    segs = segment_floors(38, 38, per=1)
    assert len(segs) == 38                      # 段数 = 层数，不写死
    assert [round(b - a, 1) for a, b in segs] == [1.0] * 38
    assert segs[0] == (1.0, 2.0)                # 1层
    assert segs[-1] == (38.0, 39.0)             # 38层
    assert sum(b - a for a, b in segs) == 38.0


def test_segment_split_decorate_group_of_three():
    segs = segment_floors(38, 13, per=3)        # 装饰装修：3 层一组
    assert len(segs) == 13
    assert [round(b - a, 1) for a, b in segs] == [3.0] * 12 + [2.0]
    assert segs[0] == (1.0, 4.0)                # 1-3层
    assert segs[-1] == (37.0, 39.0)             # 37-38层
    assert sum(b - a for a, b in segs) == 38.0


def test_segment_split_basement_half_floors():
    segs = segment_floors(2, 4, per=0.5)
    assert len(segs) == 4
    assert all(abs(b - a - 0.5) < 1e-9 for a, b in segs)
    assert sum(b - a for a, b in segs) == 2.0


def test_segment_split_equal_fallback_no_per():
    segs = segment_floors(38, 8)
    assert sum(b - a for a, b in segs) == 38.0
    assert len(segs) == 8


# ---------------- 节拍工期（代码算，clamp [2,90]） ----------------
def test_beat_duration_is_code_computed_and_clamped():
    cfg = BASE_BEAT_CONFIGS["地上主体结构"]
    ph, ids = LE.expand_node(cfg, PARAMS)
    leaves = _leaf_dicts(ph)
    assert leaves, "必须展开出叶子"
    # 全在 [2,90]（上限 90 只防异常，不再用 20 压平工程量差异）
    for l in leaves:
        assert 2 <= l["duration_days"] <= LE.CLAMP_MAX, l
    # 覆盖 LLM 拍的 days：cycle 项不该带固定工期，节拍全由引擎按 单段量/日产能 算
    for step in cfg["cycle"]:
        assert step.get("duration_days") is None, f"cycle 项不该有固定工期：{step}"


# ---------------- ID 幂等 ----------------
def test_leaf_ids_numeric_dotted_norm_invariant():
    cfg = BASE_BEAT_CONFIGS["地上主体结构"]
    ph, ids = LE.expand_node(cfg, PARAMS)
    for i in ids:
        parts = i.split(".")
        # 第 5 批（域 4）：叶子 id 从 4 段 `分部.分区.层段.工序` 变成
        # 5 段 `分部.L3工种号.L4工序号.分区.层段`（分区/层段原地保留）
        assert len(parts) == 5
        assert all(p.isdigit() for p in parts), i
    # normalize_wbs 往返不重编号
    import copy
    wbs = {"phases": [{"phase": cfg["node_name"], "work_packages": copy.deepcopy(ph["work_packages"])}]}
    out, warns = normalize_wbs(copy.deepcopy(wbs))
    assert out is not None
    before = {i for i in ids}
    after = [l for wp in out["phases"][0]["work_packages"] for l in wp["sub_packages"]]
    after_ids = {l["id"] for l in after}
    assert before == after_ids, "normalize_wbs 不得重编号节拍叶子"


# ---------------- 结构依赖无环 + 流水 < 串行 ----------------
def _beat_subnet_cpm(phase, floors=38):
    cfg = BASE_BEAT_CONFIGS[phase]
    ph, ids = LE.expand_node(cfg, {"floors": floors})
    deps = LE.structural_deps(cfg, params={"floors": floors})
    wbs = {"phases": [{"phase": phase, "work_packages": ph["work_packages"]}]}
    durs = {l["id"]: l["duration_days"] for l in _leaf_dicts(ph)}
    return wbs, durs, deps, ids


def test_structural_deps_acyclic_and_flow_beats_serial():
    for ph in BASE_BEAT_CONFIGS:
        wbs, durs, deps, ids = _beat_subnet_cpm(ph)
        # 无环（Kahn）
        indeg = {i: 0 for i in durs}
        adj = {i: [] for i in durs}
        for d in deps:
            if d["successor"] in indeg and d["predecessor"] in adj:
                adj[d["predecessor"]].append(d["successor"]); indeg[d["successor"]] += 1
        q = [i for i, v in indeg.items() if v == 0]; cnt = 0
        while q:
            u = q.pop(); cnt += 1
            for v in adj[u]:
                indeg[v] -= 1
                if indeg[v] == 0: q.append(v)
        assert cnt == len(durs), f"{ph} 依赖有环"
        # CPM 正向总工期 < 简单串行加总 ≈ 流水效率
        from pipeline.nodes import cpm
        result = cpm.calculate_cpm(wbs, {"dependencies": deps})
        serial_refl = sum(durs.values())
        assert 0 < result["total_duration_days"] < serial_refl, f"{ph} 流水未缩短工期"


# ---------------- 量级守恒 ±20% ----------------
def test_quantity_conservation_all_phases():
    for ph in BASE_BEAT_CONFIGS:
        cfg = BASE_BEAT_CONFIGS[ph]
        floors = 2.0 if cfg.get("floors_locked") else float(cfg.get("floors"))
        z, s = cfg.get("zones") or ["Ⅰ区"], int(cfg.get("segments") or 1)
        f = cfg.get("floors_per_segment") or (floors / s)
        nseg = len(segment_floors(floors, s, per=f))
        ph_dict, _ = LE.expand_node(cfg, {"floors": 38})   # 项目38层：主体类跟38，地下室被锁=2
        for step in cfg.get("cycle") or []:
            theory = float(step["qty_per_floor"]) * LE._eff_floors(cfg, {"floors": 38}) * len(z)
            found = LE.total_quantity_by_step(ph_dict["work_packages"], step["name"])
            assert theory > 0 and 0.8 * theory <= found <= 1.2 * theory, \
                f"{ph}/{step['name']} 量级偏差理论{theory:.0f}实际{found:.0f}"


# ---------------- 单区省略区名前缀（「只有一个区，为什么还要叫一区？」） ----------------
def _one_zone_params(floors=38):
    """单栋 + 标准层 471.49 m²（≤ MSSA 500 → suggest_zones 建议 1 个平面分区）。"""
    return {"floors": floors, "total_area": 471.49 * floors, "building_count": 1}


def test_single_zone_drops_zone_prefix_multi_zone_keeps_it():
    """**单区省略前缀 / 多区保留前缀** —— 本次改动的唯一契约。

    单栋项目只有一个平面流水段时，「Ⅰ区 1-1层 钢筋绑扎」里的「Ⅰ区」零信息量（纯噪音），
    名字里不该出现；有 2 个区时前缀是**有效区分**，必须原样保留。
    """
    cfg = BASE_BEAT_CONFIGS["地上主体结构"]

    # ① 单区（面积推得出来 → 1 个区）
    one = _one_zone_params()
    assert LE._effective_zones_count(cfg, one) == 1
    ph1, _ = LE.expand_node(copy.deepcopy(cfg), one)
    # 第 5 批（域 4.1c）：树的第 2 层已由「分区」改成「L3 工种」，一个工种一个工作包
    assert [wp["name"] for wp in ph1["work_packages"]] == [
        "钢筋工程", "模板工程", "混凝土工程", "架子工程"]
    leaves1 = _leaf_dicts(ph1)
    assert leaves1
    for l in leaves1:
        assert "Ⅰ区" not in l["name"], l["name"]
        assert "Ⅰ区" not in l["location"], l["location"]
        # 去前缀后仍是「起-止层」形式（唯一的 location 消费方按这个正则取楼层）
        assert re.match(r"^\d+(?:\.\d+)?-\d+(?:\.\d+)?层$", l["location"]), l["location"]
        assert l["name"].startswith(l["location"] + " "), l["name"]

    # ② 多区（面积取不到 → 沿用配置 zones=[Ⅰ区, Ⅱ区]）
    two = {"floors": 38}
    assert LE._effective_zones_count(cfg, two) == 2
    ph2, _ = LE.expand_node(copy.deepcopy(cfg), two)
    # 工作包换成 L3 工种节点后，分区不再是包名，而落在叶子自身（`_zone` 与 id 第 4 段）
    assert [wp["name"] for wp in ph2["work_packages"]] == [
        "钢筋工程", "模板工程", "混凝土工程", "架子工程"]
    leaves2 = _leaf_dicts(ph2)
    for z, zone in ((1, "Ⅰ区"), (2, "Ⅱ区")):
        got = [l for l in leaves2 if l.get("_zone") == z]
        assert got, zone
        for l in got:
            assert l["name"].startswith(zone + " "), l["name"]
            assert l["location"].startswith(zone + " "), l["location"]
            assert l["id"].split(".")[3] == str(z), l["id"]


def test_single_zone_dropped_prefix_still_parses_floor():
    """**去掉「Ⅰ区 」前缀后 location 必须仍能被消费方解析出楼层**（本次最易踩的坑）。

    唯一消费方 `quantity.floor_bucket` 只从 location/name 里正则取 "1-0.5层" / "16-20层"，
    与区名前缀无关；本用例把这条契约钉死（含地下室的 0.5 层写法）。
    """
    from pipeline import quantity as Q

    cfg = BASE_BEAT_CONFIGS["地下室结构"]            # 0.5 层一段 → 产出 "1-0.5层"
    one = _one_zone_params(floors=2)
    assert LE._effective_zones_count(cfg, one) == 1
    ph, _ = LE.expand_node(copy.deepcopy(cfg), one)
    flow = [l for l in _leaf_dicts(ph) if not l.get("_parallel")]
    assert flow
    for l in flow:
        assert "Ⅰ区" not in l["location"], l["location"]
        assert Q.floor_bucket(l, Q.FLOOR_PER_5) == "第 1-5 层", (
            "%s 的 location 去前缀后解析不出楼层：%r" % (l["id"], l["location"]))
        assert Q.floor_bucket(l, Q.FLOOR_PER_FLOOR).startswith("第 "), l["location"]


# ---------------- 单层量来源（v2.3）----------------
def test_baseline_quantities_kept_when_quantity_params_missing():
    """**向后兼容**：没有 total_area/total_concrete/total_rebar → 仍是配置写死的基线量，
    并明确标注来源「基线默认」（缺参数绝不猜）。"""
    for name, cfg in BASE_BEAT_CONFIGS.items():
        ph, _ = LE.expand_node(cfg, {"floors": 38})
        for step in cfg.get("cycle") or []:
            leaves = [l for wp in ph["work_packages"] for l in wp["sub_packages"]
                      if l.get("_step_name") == step["name"]]
            assert leaves, (name, step["name"])
            for l in leaves:
                assert l["_qty_source"] == "基线默认", (name, step["name"])
                # 一层一段/半层一段：单段量 = 写死单层量 × 段内层数
                assert l["_qty_per_floor"] == float(step["qty_per_floor"]), (name, step["name"])


def test_derived_quantities_used_when_quantity_params_present():
    """给了项目参数 → 单层量按参数/占比表推算，并如实标来源（含中文公式）。"""
    params = {"floors": 38, "total_area": 215000, "total_concrete": 82000,
              "total_rebar": 12800}
    for name, cfg in BASE_BEAT_CONFIGS.items():
        ph, _ = LE.expand_node(cfg, params)
        by_step = {}
        for wp in ph["work_packages"]:
            for l in wp["sub_packages"]:
                by_step.setdefault(l.get("_step_name"), []).append(l)
        # 至少有一道工序是参数推算，且每条推算出来的量都带中文公式
        derived = [n for n, ls in by_step.items() if ls[0]["_qty_source"] == "参数推算"]
        assert derived, name
        for n in derived:
            assert by_step[n][0]["_qty_formula"], (name, n)
        # 来源只能是三态之一（占比表拆分 / 参数推算 / 基线默认）
        for n, ls in by_step.items():
            assert ls[0]["_qty_source"] in ("占比表拆分", "参数推算", "基线默认"), (name, n)

    # 占比表可用时：主体钢筋的总量 = `Component_Ratio` 给出的 L4 总量
    # （**旧口径** `total_rebar × REBAR_RATIO[阶段]` 已随阶段比例表退役，
    #   不再按阶段摊，而是按「结构类型 × 工种」组内的构件占比拆）
    p = dict(params)
    p["structure_type"] = "frame_shear"
    l4_total = 12800 * 0.207                       # REBAR_NEW_SLAB 在 frame_shear|rebar 组内 20.7%
    p["l4_quantities"] = {"REBAR_NEW_SLAB": l4_total}
    p["_component_ratio"] = {"structure_type_id": "frame_shear", "l4_index": {
        "REBAR_NEW_SLAB": {"structure_type_id": "frame_shear",
                           "activity_id": "REBAR_NEW_SLAB", "work_type_id": "rebar",
                           "ratio_percent": 20.7, "quantity": l4_total, "unit": "t",
                           "confidence": "LOW", "review_state": "pending", "notes": ""}}}
    ph, _ = LE.expand_node(BASE_BEAT_CONFIGS["地上主体结构"], p)
    got = sum(l["quantity"] for wp in ph["work_packages"]
              for l in wp["sub_packages"] if l.get("_step_name") == "钢筋绑扎")
    assert abs(got - l4_total) / l4_total < 0.02, (
        "主体钢筋总量 %.1f t 与占比表 L4 总量 %.1f t 偏差超 2%%" % (got, l4_total))


# ---------------- 校验器 ----------------
def test_common_validate_passes_all_configs():
    for ph in BASE_BEAT_CONFIGS:
        cfg = BASE_BEAT_CONFIGS[ph]
        ph_dict, _ = LE.expand_node(cfg, {"floors": 38})
        errs = LE.common_validate(cfg, ph_dict, {"floors": 38})
        assert errs == [], f"{ph}: {errs}"


# ---------------- 集成：BeatExpandNode → deps merge → CPM ----------------
def test_integration_full_pipeline():
    from pipeline.nodes import deps_gen, cpm
    from pipeline.nodes.beat_configs import BEAT_PHASE_NAMES
    names = ["施工准备", "地基处理与桩基", "基坑支护与土方", "地下室结构", "地上主体结构",
             "二次结构与砌体", "机电安装", "装饰装修", "室外工程", "竣工验收"]
    phases = []
    for i, nm in enumerate(names, 1):
        sub = [{"id": f"{i}.1.1", "name": nm, "duration_days": 3, "quantity": 10,
                "unit": "项", "work_type": "土建"}]
        if nm not in BEAT_PHASE_NAMES:
            sub.append({"id": f"{i}.1.2", "name": nm + "b", "duration_days": 3,
                        "quantity": 10, "unit": "项", "work_type": "土建"})
        phases.append({"phase": nm, "work_packages": [{"id": f"{i}.1", "name": nm, "sub_packages": sub}]})
    ctx = {"wbs": {"phases": phases}, "extracted_params": dict(PARAMS)}

    node = BeatExpandNode(refine=False)
    node._emit = lambda e, d: None
    node.run(ctx)

    beat_ids = set(ctx["beat_leaf_ids"])
    leaves = deps_gen.collect_leaf_ids(ctx["wbs"])
    assert beat_ids <= set(leaves), "节拍叶子必须全部进入 wbs"

    # 注入即时抛错的桩 LLM → 强制 deps 走「顺序链 + 并节拍搭接」路径，不碰网络
    class _NoLLM:
        def chat_json(self, *a, **k): raise RuntimeError("no network in tests")
    dn = deps_gen.DepsGenNode(llm=_NoLLM())
    dn._emit = lambda e, d: None
    ctx.update(dn.run(ctx))
    deps = ctx["dependencies"]["dependencies"]
    assert not deps_gen.has_cycle(deps, leaves), "合并后依赖不得有环"
    # 每条依赖端点都是真实叶子
    for d in deps:
        assert d["predecessor"] in set(leaves) and d["successor"] in set(leaves)

    cpn = cpm.CPMNode(); cpn._emit = lambda e, d: None
    ctx.update(cpn.run(ctx))
    total = ctx["cpm_result"]["total_duration_days"]
    assert 0 < total, "CPM 必须给出正向总工期"
    # 每个节拍叶子都被依赖覆盖（除起点），即不会游离
    seen = set()
    for d in deps:
        seen.add(d["predecessor"]); seen.add(d["successor"])
    # 平行专项叶子（外檐保温/涂料）是全楼独立并行，本不参与结构搭接 → 排除再断言覆盖
    # （厂房外檐与内装互为平行支路，属设计约束，见 BASE_BEAT_CONFIGS.装饰装修.parallel_work）
    parallel_ids = {l["id"] for ph in ctx["wbs"]["phases"]
                    for wp in ph.get("work_packages", [])
                    for l in wp.get("sub_packages", []) if l.get("_parallel")}
    flow_ids = beat_ids - parallel_ids
    missing = flow_ids - seen
    assert not missing, f"节拍流水叶子未进入依赖图：{sorted(missing)[:5]}"


# ==================== 回归：全部工序「量0出局」的阶段 ====================
def test_全部工序量0出局的阶段不崩也不产生悬空边(monkeypatch):
    """第 7 批修复（**交付包实测崩溃**，2026-09-21）。

    现象：用户解压交付包后跑 `项目样例\\示例3_住宅楼_对比版.txt`，第 9/27 步
    「✘节点失败于节点 beat_build: list index out of range」，随后「✘流程异常结束」，
    一份计划都出不来。

    根因：某节拍阶段**所有工序都"量0出局"**（`active_steps` 把 `cycle` 全滤掉，
    典型触发是「工种有用户总量、但占比表 `Component_Ratio` 里没有该 L4 的行」）时
    `steps == []`。`layer_engine.structural_deps` 里跨相 lead_in 的挂接循环
    **没有** `if steps:` 保护（同函数上面 :650 的主循环是有保护的），
    于是 `steps[0]` 抛 `IndexError: list index out of range`。

    本用例把 `active_steps` 打桩成返回 `[]`（= 全部量0出局），钉住三件事：
      ① `expand_node` 不崩，且产出 **0 片叶子**；
      ② `structural_deps` 不崩，且**不产生任何依赖边**（尤其不许造出指向
         不存在叶子的悬空边 —— `parallel_work` 段原先会干这件事）；
      ③ 对照：`active_steps` 正常时**仍要**算出跨相 lead_in 边
         （防止"顺手把这个功能删掉"式修复）。
    """
    cfg = {
        "node_id": "6",
        "node_name": "二次结构与砌体",
        "cycle": [{"name": "砌块墙", "work_type_id": "masonry", "l4_name": "砌块墙"}],
        "segments": 1,
        "zones": ["Ⅰ区"],
        "lead_in": {"from_node": "5", "floors_ahead": 1},
    }
    params = {"building_type": "剪力墙住宅", "structure_type": "剪力墙",
              "floors": 18, "total_area": 15000}
    # 前阶段（node 5）有真实叶子 —— lead_in 的挂接点；形状与
    # `beat_node._build_phase_leaf_map` 一致。
    phase_map = {"5": {"last": "5.1.1.1.1", "zone_segments": {}}}

    # ---- ③ 对照（先跑）：正常 active_steps → 必须有跨相边 ----
    deps_normal = LE.structural_deps(copy.deepcopy(cfg), phase_map=phase_map,
                                     params=params)
    assert deps_normal, "对照失败：正常情况下 lead_in 必须产出跨相依赖边"

    # ---- ① / ② 全部工序量0出局 ----
    monkeypatch.setattr(LE, "active_steps", lambda *a, **k: [])

    pd, ids = LE.expand_node(copy.deepcopy(cfg), params)
    assert ids == [], "全部量0出局时不该有任何节拍叶子：%s" % ids
    n_leaf = sum(len(wp.get("sub_packages") or [])
                 for wp in pd.get("work_packages") or [])
    assert n_leaf == 0, "全部量0出局时不该产出叶子（实际 %d 条）" % n_leaf

    deps = LE.structural_deps(copy.deepcopy(cfg), phase_map=phase_map, params=params)
    assert deps == [], (
        "全部量0出局时不该产生依赖边（会指向不存在的叶子）：%s" % deps[:5])


if __name__ == "__main__":
    import inspect
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and inspect.isfunction(v)]
    for fn in fns:
        fn()
        print(f"  PASS  {fn.__name__}")
    print("全部 beat_engine 用例通过 ✔")