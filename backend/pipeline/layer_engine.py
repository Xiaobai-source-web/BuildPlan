"""节拍型节点共享展开引擎 — 纯函数、无 LLM（T-12）

把「节拍配置」展开成分层分段流水叶子任务：
  - 遍历 分区 × 段；每段按 工序 循环产出叶子。id = `p.z.s.k`（阶段.分区.段.工序）全数字点分，
    区位（如「Ⅰ区 1层」）放进 name + location —— 全程兼容 normalize_wbs（不重编号）。
    **多个区**才带区名前缀（那时前缀是有效区分）；**只有 1 个区**时省略前缀（单栋单流水段下
    「Ⅰ区」零信息量，纯噪音），规则集中在 `_zone_prefix()` 一处。
  - 竖向段数由层数推导：段数 = ceil(有效层数 / floors_per_segment)。结构类分部一层一段
    （floors_per_segment=1）；装饰装修 3 层一组；地下室 0.5 层一段。
  - 平面分区数按标准层面积建议（beat_configs.suggest_zones）；面积取不到 → 用配置 zones。
  - duration_days = clamp(ceil(单段量 / 日产能), 2, 90)，节拍由代码算，不靠 LLM 拍。
  - 结构搭接（同段串行 / 跨段 / 跨相 / 平行专项 lead_in+组内串行）代码生成 → beat_deps
    （Dependency dict, FS+lag）。
"""

import math

from . import ratio_scope
from . import kb as _kb
from .nodes.beat_configs import (
    SOURCE_RATIO,
    apply_l4_order,
    assign_step_numbers,
    beat_productivity,
    candidate_work_types,
    derive_beat_quantities,
    segment_floors,
    stamp_kb_activities,
    suggest_zones_from_params,
)


# ---------------- 编号：第 5 批（域 4.1 / 4.1a / 4.2 / 4.2a）----------------
def _number_steps(node_cfg):
    """给该节点的**完整**工序清单（cycle + attach + parallel）分配 `_l3_no` / `_l4_no`。

    幂等（重复调用结果一致），就地写回 step dict。

    ★ 域 4.2a 的关键：编号发生在**完整清单**上，**过滤只决定"进不进树"**。
    `expand_node` 与 `structural_deps` 都必须先调本函数再过滤 —— 两边拿到同一份编号，
    叶子 id 与依赖边不会错位；某道工序量变 0 时，其余工序 `_l4_no` **逐位不变**。
    """
    phase = node_cfg.get("node_name")
    all_steps = [s for s in list(node_cfg.get("cycle") or [])
                 + list(node_cfg.get("attach_measures") or [])
                 + list(node_cfg.get("parallel_work") or [])
                 if isinstance(s, dict)]
    assign_step_numbers(phase, all_steps)
    return node_cfg


def prepare_node_cfg(node_cfg):
    """（幂等）把配置准备到"可展开 / 可算依赖"的状态。

    两件事，**顺序不能反**：
      ① `stamp_kb_activities`：把 "L3键 + L4名" 解析成 `kb_activity_id`（域 3.1）。
         **必须在 `derive_beat_quantities` 之前** —— 占比表（`Component_Ratio`）是按
         `kb_activity_id` 认领 L4 的，晚一步就会把本该走占比表的工序算成"基线默认"。
      ② `_number_steps`：在完整工序清单上分配 `_l3_no` / `_l4_no`（域 4.2 / 4.2a）。
    `expand_node` 与 `structural_deps` 各调一次，两边拿到同一份编号与同一份 kb 编号。
    返回**新对象**（`stamp_kb_activities` 深拷贝），调用方须用返回值。
    """
    cfg = stamp_kb_activities(node_cfg)
    cfg = apply_l4_order(cfg, cfg.get("l4_order"))
    return _number_steps(cfg)


def _step_seq(node_cfg):
    """完整工序序列（cycle + attach + parallel），保序；供"全局工序序号 `_step`"用。

    `_step` 用**完整清单**的 1-based 下标 ⇒ 对"量0出局"同样免疫
    （`beat_node._zone_ladders` 靠它取"每段最后一道工序"，序号漂移会让挂接点漂）。
    """
    return [s for s in list(node_cfg.get("cycle") or [])
            + list(node_cfg.get("attach_measures") or [])
            + list(node_cfg.get("parallel_work") or [])
            if isinstance(s, dict)]


def _l3_name(step):
    """L3 工种名（树的第 2 层节点名）。**来自知识库 `L3_Work_Type`**，代码里不再写工种名表。"""
    wid = step.get("work_type_id")
    if wid:
        try:
            nm = _kb.work_type_name(wid)
            if nm:
                return nm
        except Exception:
            pass
    return step.get("work_type") or wid or "未分类工种"


def _l3_ok(step, allowed):
    """该 step 的 L4 所属 L3 是否 ∈ 本分部候选集（域 4.1b 硬约束）。"""
    wid = step.get("work_type_id")
    if not wid:
        return False
    return wid in allowed


def _leaf_id(node_id, step, z, s):
    """叶子 id（第 5 批，**5 位**）：`分部号 . L3工种号 . L4工序号 . 分区 . 层段`。

    位点含义（`docs/第5批_域3域4_任务书.md` §4.1）：
      `5.2.3.1.2` = 分部 5「地上主体结构」· 第 2 个 L3「模板工程」·
      该工种内第 3 道 L4「顶板模板」· Ⅰ 区 · 第 2 层段。
    `z` / `s` 沿用「分区下标 / 层段下标」的既有语义（与区名文字无关）。
    """
    return "%s.%s.%s.%s.%s" % (node_id, step.get("_l3_no"), step.get("_l4_no"), z, s)


def active_steps(cycle, attach=(), params=None):
    """该阶段的**真会进树**的工序（滤掉「量0出局」的那些），保序。

    B5 的第③步「量0出局」在节拍侧的执行点。判据是
    `ratio_scope.step_ratio_status(...) == "missing"`（**唯一实现**）——
    「工种有用户总量，但 `Component_Ratio` 里没有该 L4 的行 / 占比≈0」→ 不进树。
    ⚠️ `expand_node`（用已推导的 step）与 `structural_deps`（用原始 cfg 的 step）
    必须得到**同一份**过滤结果，否则叶子 id 的 `k` 序号与依赖边会错位 —— 所以
    两边都调这个函数，而不是各自读 `_ratio_excluded`（原始 cfg 上没有那个键）。

    工种没有用户总量（占比表无发言权）→ **不**过滤，继续走既有系数路径。
    """
    steps = [s for s in list(cycle or []) + list(attach or []) if isinstance(s, dict)]
    if not params:
        return steps
    return [s for s in steps
            if ratio_scope.step_ratio_status(params, s, steps)["status"] != "missing"]

# 节拍工期域：下限防 0 天，上限**只用于防异常**（量纲/产能配错导致的天文数字），
# 不是用来表达工期的。原先的 20 天上限在「一层一段」下会把立面/装饰等单段量大的
# 工序全压成 20 天，掩盖工程量差异（如 3 层一组抹灰 25800m² ÷ 480 m²/d ≈ 54 天）。
CLAMP_MIN = 2
CLAMP_MAX = 90

# 平面分区名（配置 zones 不够时补全，最多 4 个）。**只用于补全分区自身**，
# 是否写进 name/location 由 `_zone_prefix()` 决定（单区不写）。
_ZONE_NAMES = ["Ⅰ区", "Ⅱ区", "Ⅲ区", "Ⅳ区"]


def _zone_prefix(zone, zones):
    """区名前缀：**只有 1 个区时返回 ""**（其余返回 "区名 "）。

    单一平面流水段（如单栋小面积住宅，标准层 471 ㎡ → 建议 1 个区）下，
    「Ⅰ区 1-0.5层 钢筋绑扎」里的「Ⅰ区」不带任何信息量，只占看板字宽。
    `zones` 为 None（调用方未告知分区数）时**保留旧行为**（带前缀），
    免得漏传参数的调用点悄悄改名。
    """
    if zones is not None and len(zones) <= 1:
        return ""
    return f"{zone} "


def _clamp_beat(days):
    return max(CLAMP_MIN, min(CLAMP_MAX, int(math.ceil(days))))


def _fmt_range(start, last):
    """闭区间展示：段 (start, end_excl) → "start-(last=end-1)层"。"""
    def _f(v):
        return str(int(v)) if float(v) == int(v) else str(round(v, 1))
    return f"{_f(start)}-{_f(last)}层"


def _fmt_num(v):
    """数字展示：整数不带小数点（27.0→27），否则留两位（26.67）。"""
    f = float(v)
    return str(int(f)) if f == int(f) else str(round(f, 2))


def _fmt_span(layers):
    """层数展示：0.5 / 1 / 3。"""
    return _fmt_num(layers)


def _eff_floors(node_cfg, params):
    """有效层数：配置若锁定 floors（如地下室恒=2层）用配置；
    否则**优先项目参数 floors**（层数应来自项目参数），缺省才用配置里的 38 兜底。"""
    if node_cfg.get("floors_locked"):
        return max(1.0, float(node_cfg.get("floors") or 1))
    if isinstance(params, dict):
        v = params.get("floors")
        if v is not None:
            try:
                return max(1.0, float(v))
            except (TypeError, ValueError):
                pass
    return max(1.0, float(node_cfg.get("floors") or 1))


def _eff_zones(node_cfg, params):
    """有效平面分区：优先按标准层面积建议的平面段数。

    面积推不出来（缺 total_area / floors）→ 沿用配置 zones ——「平面段数取不到面积时
    用默认值，属 AI 默认，须让用户可改」。配置分区名不够时补 Ⅲ/Ⅳ区。
    （分区名是否展示给用户看，由 `_zone_prefix()` 按「单区省略」决定。）
    """
    # 缺省分区名（配置没写且面积也推不出来时用）。注意：**单区时这个名字不会进
    # name/location**（见 _zone_prefix），所以「只有一个区却叫Ⅰ区」不会漏到看板上。
    zones = list(node_cfg.get("zones") or ["Ⅰ区"])
    n = suggest_zones_from_params(params)
    if not n:
        return zones
    if n <= len(zones):
        return zones[:n]
    for nm in _ZONE_NAMES:
        if len(zones) >= n:
            break
        if nm not in zones:
            zones.append(nm)
    while len(zones) < n:                     # 极端兜底：仍不足则按序号补
        zones.append(f"{len(zones) + 1}区")
    return zones


def _effective_zones_count(node_cfg, params):
    return len(_eff_zones(node_cfg, params))


def expand_node(node_cfg, params=None):
    """把一个节拍节点配置展开为一个实体阶段子树。

    v2.3：**展开之前**先按项目参数推算单层量（beat_configs.derive_beat_quantities）——
    配置里写死的 qty_per_floor 只在参数缺失时作为基线兜底，避免「2 层地下室的钢筋
    比 38 层主体还多 5 倍」这类自相矛盾的数字被直接铺成叶子。
    每个叶子额外带 `_qty_source`（参数推算/基线默认）与 `_qty_formula`（中文公式），
    供前端展示「这个量是怎么来的」（可溯源）。

    返回
    ----
    (phase_dict, beat_leaf_ids)
      - phase_dict: {phase, work_packages}，按分区组包
      - beat_leaf_ids: 该阶段所有节拍叶子 id 列表
    元组结构保持不变（既有调用方无需改动）；推算说明经叶子的 _qty_source / _qty_formula
    外露，不另开返回值。
    """
    node_cfg = prepare_node_cfg(node_cfg)
    node_cfg, _qty_note = derive_beat_quantities(node_cfg, params)
    phase = node_cfg["node_name"]
    node_id = str(node_cfg.get("node_id") or "0")
    zones = _eff_zones(node_cfg, params)
    floors = _eff_floors(node_cfg, params)
    segments = int(node_cfg.get("segments") or 1)
    cycle = node_cfg.get("cycle") or []
    attach = node_cfg.get("attach_measures") or []
    parallel = node_cfg.get("parallel_work") or []
    qty_detail = (_qty_note or {}).get("detail") or {}

    segs = segment_floors(floors, segments, per=node_cfg.get("floors_per_segment"))
    steps_all = [s for s in list(cycle) + list(attach) if isinstance(s, dict)]
    # ---- B5 第③步：量0出局的工序不进树（判据见 active_steps）----
    # ★ 注意：`steps` 只决定**进不进树**；`_l3_no` / `_l4_no` 早在 `prepare_node_cfg`
    #   里就在**完整清单**上编好了 ⇒ 某道工序量变 0 不会让其它工序编号漂移（域 4.2a）。
    steps = active_steps(cycle, attach, params)
    dropped = [s for s in steps_all if s not in steps]
    steps_per_seg = len(steps)               # 每段完整工序（主+挂靠措施，已滤出局者）

    # 全局工序序号（1 起，**完整清单**口径，含被过滤掉的）—— `_step` 用它
    seq_index = {}
    for _i, _s in enumerate(_step_seq(node_cfg), 1):
        seq_index[id(_s)] = _i

    # ---- B4：②层量 ③段量（唯一量链路的接线点）----
    # `B4Distribution` 一次性把「该阶段的逐层面积 + 各平面段面积权重 + 占比表索引」备好；
    # 未命中占比表的工序返回 None，叶子退回既有「单层量 × 层数」口径（来源如实标注）。
    b4 = ratio_scope.B4Distribution(params, phase, floors, zones, segs, steps_all)

    allowed_wt = candidate_work_types(phase)
    work_packages = []
    beat_leaf_ids = []
    wp_by_l3 = {}                      # l3_no -> work_package dict（同工种共用一个包）

    def _wp_for(l3_no, step):
        wp = wp_by_l3.get(l3_no)
        if wp is None:
            wp = {"id": f"{node_id}.{l3_no}",
                  "name": _l3_name(step),
                  "sub_packages": []}
            wp_by_l3[l3_no] = wp
            work_packages.append(wp)
        return wp

    # 树：分部(L1) → **L3 工种** → 叶子(L4 实例 × 分区 × 层段)（域 4.1c）。
    # 分区/层段不再是树的一层，它们退进叶子自身的 id 后两位与 `_zone`/`_segment` 字段
    # ——「阶段」这一层同时被废弃（域 3.4），4 个阶段名不再是查节点的途径。
    for step in steps:
        _wp_for(step.get("_l3_no"), step)          # 先按工序顺序建包（保证包序 = 工种序）
    for step in steps:
        wp = wp_by_l3[step.get("_l3_no")]
        for z, zone in enumerate(zones, 1):
            for s, (start, end) in enumerate(segs, 1):
                layers = end - start          # 半开区间层数
                leaf = _make_leaf(node_id, z, s, seq_index.get(id(step), 0), zone,
                                  start, end, layers, step,
                                  qty_detail.get(step.get("name")), zones=zones,
                                  b4=b4, allowed_wt=allowed_wt)
                wp["sub_packages"].append(leaf)
                beat_leaf_ids.append(leaf["id"])

    # 外檐平行（节点8）：独立分区，与内装互不锁死
    for pi, p in enumerate(parallel, 1):
        pz = len(zones) + pi
        daily = beat_productivity(p.get("resource", "普工"))
        dur = _clamp_beat(p["qty_total"] / daily / 4.0)   # 4 个班组并行
        leaf = {
            "id": _leaf_id(node_id, p, pz, 1),
            "name": f"{p['name']}（全楼平行）",
            "location": "全楼",
            "duration_days": dur,
            "quantity": p["qty_total"],
            "unit": p.get("unit", "m²"),
            "work_type": p.get("work_type", "土建临建"),
            "_beat": True, "_zone": pz, "_segment": 1,
            "_step": seq_index.get(id(p), 0), "_parallel": True,
            # ---- 域 3.5：**不展开的一次性/全楼平行活动不需要楼层范围**（明确没有）----
            "floor_range": None,
            "floors": 0.0,
            "layer_expandable": False,
            "_qty_source": p.get("_qty_source"),
            "_qty_formula": p.get("_qty_formula") or "",
            # 同 _make_leaf：恒存在；外檐专项是全楼总量、不参与单层量级自检，故默认 False。
            "_qty_suspect": bool(p.get("_qty_suspect")),
            "_qty_suspect_reason": p.get("_qty_suspect_reason") or "",
        }
        _stamp_layer_fields(leaf, p, allowed_wt)
        if p.get("kb_activity_id"):          # v2.1：把节拍工序挂上 KB 编号，供定额锚定用
            leaf["kb_activity_id"] = p["kb_activity_id"]
        pz_name = zones[-1] if zones else ""
        payload = ratio_scope.task_capacity_payload(pz_name, 0.0,
                                                    p.get("resource") or "")
        leaf["segment_id"] = payload["segment_id"]
        leaf["segment_area"] = payload["segment_area"]
        leaf["capacity_fixed"] = payload["capacity_fixed"]
        leaf["capacity_mobile"] = payload["capacity_mobile"]
        leaf["capacity_source"] = payload["capacity_source"]
        leaf["segment_capacity"] = payload
        _wp_for(p.get("_l3_no"), p)["sub_packages"].append(leaf)
        beat_leaf_ids.append(leaf["id"])

    # 空包不进树（normalize_wbs 会因「工作包缺 sub_packages」整棵树判废）
    work_packages = [wp for wp in work_packages if wp.get("sub_packages")]

    _excl, _seen_excl = [], set()
    for _e in list(b4.excluded) + [
            {"step": s.get("name"),
             "activity_id": (s.get("_ratio_excluded") or {}).get("activity_id"),
             "kind": "abnormal_absent",
             "reason": (s.get("_ratio_excluded") or {}).get("reason") or "量0出局"}
            for s in dropped]:
        _key = (_e.get("step"), _e.get("activity_id"))
        if _key in _seen_excl:
            continue
        _seen_excl.add(_key)
        _excl.append(_e)
    return {
        "phase": phase,
        "work_packages": work_packages,
        # ---- B4/B5 留痕（绝不静默；beat_node 会把它们带进 beat_subtrees）----
        "ratio_degradations": list(b4.degradations),
        "ratio_exclusions": _excl,
        "ratio_steps": {
            "total": len(steps_all), "kept": len(steps),
            "ratio_driven": sum(1 for s in steps
                                if ratio_scope.step_ratio_status(
                                    params, s, steps_all)["status"] == "ratio"),
        },
    }, beat_leaf_ids


def _stamp_layer_fields(leaf, step, allowed_wt=None):
    """把「树第 2/3 层」的身份与**结构化楼层范围**写到叶子上（域 3.5 / 4.1c）。

    写三个东西：
      · `l3_no` / `l4_no` / `l3_work_type_id` / `l3_candidate_ok` —— 树第 2 层（L3 工种）
        与第 3 层（L4 工序）的身份，`l3_candidate_ok=False` 即**违反 4.1b 硬约束**
        （L4 所属 L3 不在该分部的候选集里），在产物里可见，不是只写在文档里。
      · `work_type` 仍是 L3 的中文名（既有消费方不变）；新增 `l3_work_type_id` 是**键**，
        刻意不复用 `work_type_id` 这个名字 —— 叶子上已有一个同名键在 `_ratio_source` 里，
        另开一个顶层键才不会混淆。
      · `floor_range` / `floors` / `layer_expandable`：**楼层范围的结构化字段**。
        旧产物里 379 个叶子**一个都没有**这个字段，楼层范围只以文本躺在 `location` 里
        （"37-38层"/"全楼"），域 7.11 与看板 8.4 都拿不到。这里补上：
          - 可分层实体工程 → `floor_range = {start, end, end_inclusive, floors, label}`，
            `layer_expandable = True`；
          - 不展开的一次性/全楼平行活动 → `floor_range = None`、`floors = 0`、
            `layer_expandable = False`（**明确没有**，而不是留个空壳）。
    """
    leaf["l3_no"] = step.get("_l3_no")
    leaf["l4_no"] = step.get("_l4_no")
    leaf["l3_work_type_id"] = step.get("work_type_id")
    leaf["l3_candidate_ok"] = _l3_ok(step, allowed_wt or [])
    return leaf


def _make_leaf(node_id, z, s, step_no, zone, start, end, layers, step, qty_note=None,
               zones=None, b4=None, allowed_wt=None):
    """产出一个节拍叶子（带单层量来源与中文公式，供溯源展示）。

    `zones` = 该节点的有效分区列表；只用来决定**要不要写区名前缀**（见 `_zone_prefix`）。
    省略前缀对消费方无影响：唯一的 location 消费方是 `quantity.floor_bucket`（L274），
    它只从 `location`/`name` 里正则取 `\\d+-\\d+层` / `\\d+层`（"1-0.5层"、"16-20层"），
    与区名前缀无关；区号本身走结构化字段 `_zone`，不靠字符串解析。

    `b4` = `ratio_scope.B4Distribution`。**命中占比表时（唯一量链路）**：
      叶子量 = ③`L4 层量 × (该段面积 ÷ 该层面积)`（原 `per_floor × layers` 不再决定
      这些叶子的量），并附 §6 验收 #3 的四个段级字段与逐行 AI 估算标注。
      未命中（工种没有用户总量 / 单位不同量纲 / 未认领该 L4）→ 保持原式、来源如实标注。
    """
    per_floor = float(step.get("qty_per_floor") or 0)
    unit = step.get("unit", "项")
    got = b4.step_quantity(step, s, z) if b4 is not None else None
    if got is not None:
        qty = round(float(got["qty"]), 2)
        formula = got["formula"]
        source = SOURCE_RATIO
        ratio_info = got.get("ratio") or {}
    else:
        qty = round(per_floor * layers, 0)
        source = (qty_note or {}).get("source")
        # 层数折算必须写进算式。qty_note 里的公式是**单层量表**达式（以"/层"结尾），
        # 而本叶子的量 = 单层量 × 覆盖层数。此前直接把单层公式挂上去，于是出现
        # "值 27.0 t，算式却写 = 53.33 t/层" 这种**算式不等于值**（D2）——用户会以为算错了。
        formula = (qty_note or {}).get("formula") or ""
        ratio_info = {}
        if formula and layers and abs(float(layers) - 1.0) > 1e-9:
            formula = "%s ×%s层 = %s %s" % (formula, _fmt_span(layers),
                                            _fmt_num(qty), unit)
            # 单层量本身是四舍五入过的，乘完可能落不到整数上；差值可见时才标注，
            # 免得算式看起来"差一点"。
            if abs(per_floor * float(layers) - qty) > 1e-9:
                formula += "（取整）"
        elif formula and abs(per_floor - qty) > 1e-9:
            # 单层任务：量取整后仍可能与算式末尾的单层值差一点（22.46 → 22）。
            # 一并写出来，否则同样是"算式不等于值"。
            formula = "%s（取整为 %s %s）" % (formula, _fmt_num(qty), unit)
    if step.get("duration_days"):           # 措施项给固定节拍
        dur = int(step["duration_days"])
    else:
        daily = beat_productivity(step.get("resource", "普工"))
        dur = _clamp_beat(qty / daily)
    prefix = _zone_prefix(zone, zones)      # 单区 → ""（「Ⅰ区」在单栋项目里是噪音）
    floor_range = _fmt_range(start, end - 1.0)
    leaf = {
        "id": _leaf_id(node_id, step, z, s),
        "name": f"{prefix}{floor_range} {step['name']}",
        "location": f"{prefix}{floor_range}",
        "duration_days": dur,
        "quantity": qty,
        "unit": unit,
        "work_type": step.get("work_type", "土建临建"),
        "_beat": True, "_zone": z, "_segment": s, "_step": step_no,
        "_step_name": step["name"],
        # ---- 域 3.5：楼层范围结构化（旧产物 379/379 都没有这个字段）----
        "floor_range": {
            "start": float(start),          # 段首层（含）
            "end": float(end) - 1.0,        # 段末层（含，与 name/location 的文本一致）
            "end_inclusive": True,
            "floors": float(layers),        # 本段覆盖层数（半开区间差）
            "label": floor_range,
        },
        "floors": float(layers),
        "layer_expandable": True,           # 可分层实体工程 → 按层展开
        # 单层量来源（"占比表拆分"/"参数推算"/"基线默认"）+ 中文公式（怎么来的），供溯源展示
        "_qty_source": source,
        "_qty_formula": formula,
        "_qty_per_floor": (round(float(got["qty"]) / float(layers), 6)
                           if (got is not None and layers) else per_floor),
        # 量级自检标记（与 _qty_source/_qty_formula 同一条链路透传）：只有走「基线默认」
        # 且单位面积指标超物理上限时才为 True（见 beat_configs.SUSPECT_*）。**可疑不等于改数**
        # ——量照原样保留，标记交给上游/用户决定。无 qty_note 的场景（手工构造的叶子/测试）
        # 取 False/""，保证字段恒存在、消费方无需判 None。
        "_qty_suspect": (bool((qty_note or {}).get("suspect")) if got is None else False),
        "_qty_suspect_reason": (((qty_note or {}).get("suspect_reason") or "")
                                if got is None else ""),
    }
    if got is not None:
        # ---- 逐行 AI 估算标注透传（用户硬性要求：逐行可溯源）----
        leaf["_ratio_source"] = {
            "source": "Component_Ratio",
            "structure_type_id": ratio_info.get("structure_type_id"),
            "activity_id": ratio_info.get("activity_id"),
            "work_type_id": ratio_info.get("work_type_id"),
            "ratio_percent": ratio_info.get("ratio_percent"),
            "l4_total": got.get("l4_total"),
            "total_param": got.get("total_param"),
            "total_value": got.get("total_value"),
            "confidence": ratio_info.get("confidence") or "",
            "review_state": ratio_info.get("review_state") or "",
            "notes": ratio_info.get("notes") or "",
            "floor_area_source": got.get("floor_area_source") or "",
            "segment_rule": got.get("segment_rule") or "",
        }
    # ---- §6 验收 #3：段级容量字段（**每道工序都有**，与 W2-C 的 _organization 契约同口径）----
    # `segment_id` / `segment_area` 是几何事实（与量从哪来无关）；`capacity_fixed` /
    # `capacity_mobile` 由 MWI × 段面积算出，取的 resource 就是这道工序的班组。
    if b4 is not None:
        seg_id, seg_area = (b4.segment_geometry(s, z) if got is None
                            else (got["segment_id"], float(got["segment_area"])))
    else:
        seg_id = (zones or ["Ⅰ区"])[z - 1] if zones and 1 <= z <= len(zones) else ""
        seg_area = 0.0
    payload = ratio_scope.task_capacity_payload(seg_id, seg_area,
                                                step.get("resource") or "")
    leaf["segment_id"] = payload["segment_id"]
    leaf["segment_area"] = payload["segment_area"]
    leaf["capacity_fixed"] = payload["capacity_fixed"]
    leaf["capacity_mobile"] = payload["capacity_mobile"]
    leaf["capacity_source"] = payload["capacity_source"]
    leaf["segment_capacity"] = payload
    _stamp_layer_fields(leaf, step, allowed_wt)
    if step.get("kb_activity_id"):           # v2.1：节拍工序挂 KB 编号，供定额锚定用
        leaf["kb_activity_id"] = step["kb_activity_id"]
    # ⚠️ 裁定-2（2026-09-21）：**不再**写 `leaf["_crew_design"]`（节拍配置的设计班组）。
    # 它曾是"工期 = 工日数 ÷ 人数"里那个可谈的变量，但 C8 之后人数只认用户给的
    # `leaf["crew_design"]`（见 scheduler.py 的 C8 第 6 项），没有消费方 ⇒ 死键会误导
    # 后续读者以为自己还在参与摊派。配套的 `beat_crew_count(...)` 计算也一并删除。
    return leaf


def _floors_ahead(lead_in):
    """`lead_in.floors_ahead`：领先层数。缺失 / 非法 / 负数 → 0（→ 串行语义）。"""
    if not isinstance(lead_in, dict):
        return 0
    try:
        return max(0, int(lead_in.get("floors_ahead") or 0))
    except (TypeError, ValueError):
        return 0


def _seg_leaf_for_floor(segs, floors_ahead):
    """某分区阶梯里「第 (1+floors_ahead) 层所在那一段」的末工序 id（取不到 → None）。

    阶梯按段号升序；取第一个「段末层 ≥ 1+floors_ahead」的段 —— 该段的末工序
    （`k = n_steps`，见 `beat_node._zone_ladders`）就是"前阶段这一步做完了"的挂接点。
    前阶段层数不足以领先 N 层 → 退到该阶段最后一段（不越界、不凭空造句）。
    层内仍是串行（取的是末工序，不是把同层工序拆开），领先只发生在**分部之间**。
    """
    if not segs:
        return None
    want = 1.0 + float(floors_ahead)
    for seg in segs:
        try:
            end_floor = float(seg.get("end_floor"))
        except (TypeError, ValueError):
            continue
        if end_floor + 1e-9 >= want:
            return seg.get("leaf")
    return segs[-1].get("leaf")


def _zone_lead_leaves(entry, floors_ahead, n_zones):
    """按**分区**解析跨相 lead_in 挂接点 → `[(分区号, 前置叶子 id, lag_days), ...]`。

    裁定-1（2026-09-21）：跨相搭接**每个分区各挂一条**，来路是「前阶段**同分区**第
    (1+N) 层所在那一段的末工序」，lag=0。这样 `structural_deps` docstring 里
    「第 2 个分区（Ⅱ区）首段首工序：独立起点，不依赖第 1 个区」才真正落到依赖边上 ——
    Ⅱ区不再落到孤儿补边（`deps_gen._candidate_predecessors` 规则 4/5）上，拿一条
    语义可疑的"上一阶段收尾"（实测：`8.2.1.1 ← 7.4.6` 机电安装尾）。

    `entry` = `phase_map[lead_in.from_node]`，两种形状都接受：

      · **dict**（`beat_node._build_phase_leaf_map` 的新形状：`last` + 各分区
        `zone_segments` 阶梯）：
          - `floors_ahead = N > 0` 且该分区有阶梯 → 「**前阶段第 (1+N) 层所在那一段的
            最后一道工序**」，lag=0（领先由挂接点的**层位**表达）；
          - `floors_ahead == 0` → 原串行语义：前阶段**最后一片叶子**，lag=0；
          - **前阶段分区数不足**（该分区在前阶段不存在：前阶段只有 1 个分区，或前阶段
            根本不是节拍阶段）→ 回落到**第 1 个分区**的同一挂接点；连第 1 个分区都没有
            阶梯（非节拍前阶段）→ 前阶段末叶 `last`。
            理由：前阶段"第 (1+N) 层整层完成"是**覆盖全平面**的事件，各分区同时具备
            工作面条件；这比"等前阶段末叶"更贴近现场，也**绝不**让任何分区无前置
            （无前置 = 被排到开工第 1 天，是更糟的结果）。
      · **str**（旧形状：该阶段最后叶子 id）→ 没有层位信息，退回旧口径：**只挂第 1 个
        分区**、lag = `floors_ahead × 2` 天（既有外部调用方语义完全不变）。
    """
    if entry is None:
        return []
    if isinstance(entry, str):
        try:
            ahead = max(0, int(floors_ahead or 0))
        except (TypeError, ValueError):
            ahead = 0
        return [(1, entry, ahead * 2)]
    if not isinstance(entry, dict):          # 形状不认识 → 不猜
        return []
    last = entry.get("last")
    ladders = entry.get("zone_segments") or {}
    n = int(n_zones or 1)
    if n < 1:
        n = 1

    def _ladder(zone):
        return ladders.get(zone) or ladders.get(str(zone)) or []

    def _point(zone):
        segs = _ladder(zone)
        if floors_ahead > 0 and segs:
            return _seg_leaf_for_floor(segs, floors_ahead)
        return last                    # N=0 / 无阶梯 → 前阶段末叶（原串行语义）

    ref = _point(1) or last            # 第 1 个分区的挂接点＝分区不足时的回落点
    out = []
    for z in range(1, n + 1):
        leaf = (_point(z) if _ladder(z) else ref) or ref or last
        if leaf:
            out.append((z, leaf, 0))
    return out


def structural_deps(node_cfg, phase_map=None, params=None):
    """生成节拍节点结构搭接。返回 Dependency dict 列表。

    规则（v2.2 语义不变，只是段数 = ceil(层数/每段层数) 变了）：
      - 跨段同工序：`(z,s,k) → (z,s+1,k)`（流水搭接）
      - 同段工序串行：`(z,s,k) → (z,s,k+1)`
      - **每个分区**的首段首工序 → 跨相各挂一条到前节点（lead_in.from_node）的
        **领先挂接点**（E5-a + 裁定-1）：
          · `floors_ahead = N > 0` → 挂到「**前阶段同分区第 (1+N) 层所在那一段的最后
            一道工序**」（层内仍串行：取的是该段 `k = n_steps` 的末工序），lag=0；
          · `floors_ahead` 缺省 / 0 → 保持原串行语义：前节点**最后一片叶子**，lag=0。
          · 前阶段分区数不足 → 回落第 1 个分区的挂接点；非节拍前阶段 → 前阶段末叶。
        解析见 `_zone_lead_leaves`。
        「领先 N 层」由**挂接点自身的层位**表达，不再用「前阶段末叶之后 + N×2 天」——
        旧的 `lag_days = floors_ahead×2` 挂在末叶上等于没提前（它就是本次要修的缺陷）。
      - **第 2 个分区**（多区时即 Ⅱ区）首段首工序：独立起点，**不依赖第 1 个区**
        （两区平行）—— 只依赖**前阶段同分区**的领先挂接点（裁定-1 落到边上）。
      - **平行专项**（`parallel_work`，如外檐保温/涂料）挂在 `z = len(zones)+pi`（见
        expand_node）：**第 1 条与「第 1 个分区的首段首工序」同一个来路**（同 lead_in
        前置叶子、同 lag），组内再按配置声明顺序相接（保温 → 涂料，lag=0）。
    注意：这里的「第几个区」是**下标**，与区名文字（Ⅰ区/Ⅱ区）无关 —— 单区时也只有一个下标 1。
    分区数必须与 expand_node 一致（同为 _eff_zones），否则端点会缺失。
    """
    node_cfg = prepare_node_cfg(node_cfg)
    node_id = str(node_cfg.get("node_id") or "0")
    zones = _eff_zones(node_cfg, params)
    floors = _eff_floors(node_cfg, params)
    segments = len(segment_floors(floors, int(node_cfg.get("segments") or 1),
                                  per=node_cfg.get("floors_per_segment")))
    cycle = node_cfg.get("cycle") or []
    attach = node_cfg.get("attach_measures") or []
    # ★ 必须与 `expand_node` 用**同一份过滤结果**（`active_steps` 是唯一实现），
    #   否则叶子的工序序号与依赖边会错位。编号（`_l3_no`/`_l4_no`）取**完整清单**口径，
    #   所以"某道工序量变 0"只改变**哪几条边存在**，不改变任何 id（域 4.2a）。
    steps = active_steps(cycle, attach, params)

    def lid(z, s, step):
        return _leaf_id(node_id, step, z, s)

    deps = []
    if steps:
        for z in range(1, len(zones) + 1):
            for s in range(1, segments + 1):
                for i, st in enumerate(steps):
                    if s < segments:
                        deps.append({"predecessor": lid(z, s, st),
                                     "successor": lid(z, s + 1, st),
                                     "type": "FS", "lag_days": 0})
                    if i + 1 < len(steps):
                        deps.append({"predecessor": lid(z, s, st),
                                     "successor": lid(z, s, steps[i + 1]),
                                     "type": "FS", "lag_days": 0})

    # 跨相 lead_in：**每个分区各挂一条** → 前节点「领先 floors_ahead 层」的挂接点。
    # 解析见 `_zone_lead_leaves`：N>0 挂「前阶段**同分区**第 (1+N) 层那一段的末工序」
    # （lag=0）；N=0（无 lead_in / 结构类）保持原串行语义（前阶段末叶，lag=0）；
    # 前阶段分区数不足 → 回落第 1 个分区的挂接点（绝不无前置）。
    lead_in = node_cfg.get("lead_in") or {}
    from_node = lead_in.get("from_node")
    entry = phase_map.get(from_node) if (from_node and phase_map) else None
    own_prefix = f"{node_id}."
    target, lag = None, 0
    # ⚠️ 第 7 批修复（交付包实测崩溃，2026-09-21）：**`steps` 可能为空**。
    # 当该阶段所有工序都"量0出局"（`active_steps` 把它们全滤掉）时，这个阶段
    # **根本不进树**（`expand_node` 也调 `active_steps`，一片叶子都没有）。
    # 上面 :650 的主循环有 `if steps:` 保护，**这里原先没有** ⇒ `steps[0]` 抛
    # `IndexError: list index out of range`，整条流水线在 `beat_build` 节点失败。
    # 用户实测（交付包 v3.1 跑 `项目样例\示例3_住宅楼_对比版.txt`）：
    #   第 9 / 27 步「✘节点失败于节点 beat_build: list index out of range」，
    #   随后「✘流程异常结束」，一份计划都出不来。
    # 口径：`steps` 为空 = 本阶段没有可挂接的叶子 ⇒ **跨相边与 parallel 边都不产生**。
    # 若照旧执行，`parallel` 段还会用 `lid(...)` 造出**指向不存在叶子**的依赖边。
    if steps:
        for z, leaf, lead_lag in _zone_lead_leaves(entry, _floors_ahead(lead_in),
                                                   len(zones)):
            # 畸形配置：`lead_in.from_node` 指回本阶段时，挂接点必然落在组内链自己的下游
            # （末叶或领先段都在本阶段内）→ 接上去立刻成环。整条来路作废（既有用例钉着）。
            if not leaf or str(leaf).startswith(own_prefix):
                continue
            if z == 1:
                target, lag = leaf, lead_lag
            deps.append({"predecessor": leaf, "successor": lid(z, 1, steps[0]),
                         "type": "FS", "lag_days": lead_lag})

    # 平行专项（parallel_work，如节点 8 的外檐保温/涂料）：
    # 它们的叶子挂在 `z = len(zones)+pi`（expand_node），**不在上面那个
    # `range(1, len(zones)+1)` 循环里** —— 靠分区/段循环结构上永远拿不到任何搭接；
    # 而上游 `beat_node.merge_beat_deps` 又会删掉「两端都在节拍叶子上」的模型边
    # （节拍搭接由代码独占，见该函数注释）。两条叠加 ⇒ 外檐专项**永远无前置**，
    # 被排到开工第 1 天（真计划 plan_sample3_after_allfix：8.2.1.1/8.3.1.1 前置+后续全空，
    # 且 9 份历史计划 9/9 复现）。所以搭接必须在这里、按配置生成：
    #   ① 第 1 条平行专项 ← 与本阶段首条流水**同一个来路**（lead_in 的前置叶子 + 同 lag）
    #      —— 外檐与内装同属这个节点，来路不一致就会出现「内装等二次结构、外檐却开工」
    #      的自相矛盾；
    #   ② 组内按 `parallel_work` 的声明顺序相接（外檐保温 → 外檐涂料，lag=0，同段工序串行
    #      的既有口径），声明顺序即施工先后。
    # 新增配置只要写进 `parallel_work` 就自动适用，无需在别处补边。
    parallel = node_cfg.get("parallel_work") or []
    if parallel and steps:
        # 入口不能是本阶段自己的叶子（已在上面用 own_prefix 拦掉）→ 这里直接用 target。
        # ⚠️ `steps` 为空时**整段跳过**：此时本阶段一片叶子都没有（`expand_node` 同样按
        # `active_steps` 过滤），照旧执行会造出指向不存在叶子的依赖边。
        prev, prev_lag = target, lag
        for pi in range(1, len(parallel) + 1):
            cur = lid(len(zones) + pi, 1, parallel[pi - 1])   # 与 expand_node 的 pz 同口径
            if prev:
                deps.append({"predecessor": prev, "successor": cur, "type": "FS",
                             "lag_days": prev_lag})
            prev, prev_lag = cur, 0

    return _dedupe_deps(deps)


def _dedupe_deps(deps):
    seen = set()
    out = []
    for d in deps:
        key = (d["predecessor"], d["successor"], d["lag_days"])
        if key not in seen and d["predecessor"] != d["successor"]:
            seen.add(key)
            out.append(d)
    return out


# ---------------- 校验 ----------------
def total_quantity_by_step(work_packages, step_name):
    """统计某工序节拍叶子总量；供单层量级校验。"""
    return sum(l.get("quantity") or 0 for wp in work_packages for l in wp.get("sub_packages", [])
               if (l.get("_step_name") or "") == step_name)


def common_validate(node_cfg, phase_dict, params=None):
    """共用校验（配置级 + 展开后量级）。返回问题列表；空=通过。"""
    errors = []
    phase = node_cfg["node_name"]
    floors = _eff_floors(node_cfg, params)
    segments = int(node_cfg.get("segments") or 1)
    zones = _eff_zones(node_cfg, params)

    # 1. 层数守恒
    segs = segment_floors(floors, segments, per=node_cfg.get("floors_per_segment"))
    total = sum(end - start for start, end in segs)
    if total < floors - 1e-6:
        errors.append(f"{phase}: 段分层数不守恒 ∑{total:.1f}<{floors}")

    # 2. 单层量级 ±20%
    #    · 占比表驱动的工序（**B4-7 裁定**）：守恒基准改为 **Σ层量**。
    #      Σ层量 × Σ段权重 ≡ L4 总量（②按层面积加权、③按段面积加权，两次权重之和都是 1），
    #      所以理论值就是占比表给出的 **L4 总量** —— 这仍然是一次独立复核：
    #      它验证"展开出来的叶子确实把各层量都加到了 L4 总量上"。
    #    · 其余工序保持原式 `qty_per_floor × floors × 分区数`。
    #    · 「量0出局」的工序（occupancy=missing）本就不进树 → 不参与量级核对。
    wps = phase_dict.get("work_packages") or []
    steps_all = [s for s in list(node_cfg.get("cycle") or [])
                 + list(node_cfg.get("attach_measures") or []) if isinstance(s, dict)]
    for c in node_cfg.get("cycle") or []:
        st = ratio_scope.step_ratio_status(params, c, steps_all)
        if st["status"] == "missing":
            continue
        if st["status"] == "ratio":
            theory = float((st.get("info") or {}).get("quantity") or 0.0)
        else:
            theory = float(c.get("qty_per_floor") or 0) * floors * len(zones)
        found = total_quantity_by_step(wps, c["name"])
        if theory > 0 and (found < 0.8 * theory or found > 1.2 * theory):
            errors.append(f"{phase}: 工序「{c['name']}」量级偏差（理论{theory:.0f}，实际{found:.0f}）")

    # 3. 节拍域：duration 由 expand 时 clamp 保证 [2,90]（上限防异常，不表达工期）
    for wp in wps:
        for l in wp.get("sub_packages", []):
            d = l.get("duration_days")
            if d is not None and not (CLAMP_MIN <= d <= CLAMP_MAX):
                errors.append(f"{phase}: 节拍越界 {l.get('name')} d={d}")
    return errors