# -*- coding: utf-8 -*-
"""域 5 · 补量的**纯函数层**（不起 Pipeline、不联网、不读库——除 `l4_unit_of` 一处惰性只读）。

设计依据：`docs\\域5_补量与冻结_实现设计.md`（§3 的 5.1–5.7、§6、§7、§8.3、§9.1、§14.1 裁决）。
分层与 `ratio_scope.py` / `segment_capacity.py` / `segment_plan.py` 一致：
**这里只放可离线单测的纯函数**，节点（`nodes\\quantity_agent.py`）只做编排与 ctx 进出。

三条硬边界（与设计逐字一致）：
  1. `backend\\pipeline\\kb_units.py` 是**冻结接口**，本文件只调它的公开函数，一个字都不改。
  2. **不 import 别的节点的私有符号**（`norm_bind._MAGNITUDE_BANDS` / `norm_bind.iter_leaves`）——
     私有常量与三行遍历各复制一份 + 「同源」注释（父代理裁决 #4 的既有做法：
     `ratio_scope.GROUP_TOTAL_PARAMS` ↔ `kb_scope._quantity_strengthened_l3`）。
     `backend\\tests\\test_quantity_agent.py` 里有两条**钉住同源**的断言（常量相等 / 遍历结果相等）。
  3. 换算不出来**绝不 1:1 兜底**：`convert_to_target` 返回 `evidence="unresolved"` + 中文 hint，
     由调用方判 `unit_unresolved` 并如实报缺。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import kb_units
# 同源常量（复制 + 注释，不 import —— 见文件头第 2 条）：
#: 与 `ratio_scope.QTY_ZERO_TOL` 同源（= 1e-6）。改一处必须同步改另一处；
#: `test_quantity_agent.py::test_zero_tol_same_as_ratio_scope` 钉着这条。
QTY_ZERO_TOL = 1e-6
#: 与 `ratio_scope.RATIO_CTX_KEY` 同源（= "_component_ratio"）。同上，有测试钉着。
RATIO_CTX_KEY = "_component_ratio"
#: 与 `ratio_scope.l4_index_of()` 读的同一个键（子键名）。
RATIO_L4_INDEX_KEY = "l4_index"

#: 每批喂给模型的 L4 条数（设计 §5.2 / §10）。
BATCH_SIZE = 25
#: 批级"再问一次"上限（**不含** `llm.py` 内部的瞬时重试；两层正交，不叠乘）。
BATCH_MAX_ATTEMPTS = 2

#: `coverage.l4[aid]["source"]` 的**唯一权威取值域**（设计 §7.2）。
SOURCE_USER = "user"
SOURCE_RATIO = "ratio"
SOURCE_TREE = "tree"
SOURCE_LLM = "llm"
SOURCE_NONE = "none"

#: `coverage.l4[aid]["status"]` 取值域（设计 §4.4）。
STATUS_QUANTIFIED = "quantified"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_UNSTATED_TREE = "unstated_tree"
STATUS_UNSTATED_ABSENT = "unstated_absent"
STATUS_UNIT_UNRESOLVED = "unit_unresolved"
STATUS_MODEL_UNAVAILABLE = "model_unavailable"

#: 叶子 `_qty_source` 的既有三态（复制 + 同源注释，不 import 节点模块）：
#: 与 `beat_configs.SOURCE_RATIO / SOURCE_PARAM / SOURCE_BASE` 逐字相同。
LEAF_SOURCE_RATIO = "占比表拆分"
LEAF_SOURCE_PARAM = "参数推算"
LEAF_SOURCE_BASE = "基线默认"

#: `provenance.quantity.origin` 映射（设计 §7.2；字段名固定 provenance）。
ORIGIN_OF_SOURCE = {
    SOURCE_USER: "user",
    SOURCE_RATIO: "kb",
    SOURCE_TREE: "default",
    SOURCE_LLM: "ai",
    SOURCE_NONE: "unknown",
}


# ======================================================================
# 0. 小工具
# ======================================================================
def is_finite_number(v: Any) -> bool:
    """有限数（nan / inf / 非数 / bool 都不算）。"""
    if v is None or v == "" or isinstance(v, bool):
        return False
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f and f not in (float("inf"), float("-inf"))


def as_text(v: Any) -> str:
    """安全取字符串（None → ""）。"""
    if v is None:
        return ""
    return str(v)


def iter_leaves(wbs: Any):
    """按 阶段 → 工作包 → 子包 三层顺序产出 `(leaf, 阶段名, 工作包名)`。

    ⚠️ 与 `norm_bind.iter_leaves`（`nodes\\norm_bind.py:254`）**同源**（三行遍历，没有任何分叉）。
    这里复制而不 import：`quantity_scope` 是纯函数层，不该为了三行遍历把 194 KB 的
    `norm_bind` 模块（及其全部节点依赖）拖进离线单测。`test_quantity_agent.py` 里有一条
    「两份遍历对同一 wbs 结果逐项相同」的钉住断言。
    """
    for phase in (wbs or {}).get("phases") or []:
        pname = phase.get("phase") or ""
        for wp in phase.get("work_packages") or []:
            for sub in wp.get("sub_packages") or []:
                yield sub, pname, (wp.get("name") or "")


def audit_leaves(wbs: Any) -> List[Dict[str, Any]]:
    """WBS 里全部叶子的对象列表（**顺序 = 树序**，不排序）。"""
    return [leaf for leaf, _p, _w in iter_leaves(wbs)]


def leaf_id(leaf: Mapping[str, Any]) -> str:
    """叶子 id（缺 → ""），只用于稳定排序。"""
    return as_text((leaf or {}).get("id"))


def leaf_qty(leaf: Mapping[str, Any]) -> float:
    """叶子量（非数 → 0.0）。"""
    return float((leaf or {}).get("quantity") or 0.0) if is_finite_number(
        (leaf or {}).get("quantity")) else 0.0


def leaf_qty_sum(leaves: Sequence[Mapping[str, Any]]) -> float:
    """叶子量之和 —— **一律 `math.fsum`**（与 `ratio_scope` / `segment_capacity` 同口径）。

    用 `sum` 会让浮点求和顺序影响结果，正是"重跑逐位一致"要防的那类差异。
    """
    return math.fsum(leaf_qty(l) for l in (leaves or []))


# ======================================================================
# 1. 闭集（防丢项的分母）与树内索引
# ======================================================================
def closed_l4_set(kb_scope: Any) -> Dict[str, Dict[str, Any]]:
    """**闭集** = ∪ `kb_scope["l4_candidates"].values()` 的 `activity_id`（设计 §2.2）。

    返回 `{activity_id: item}`，item 含节点与交付物都要用的字段：
      activity_id / activity_name / work_type_id / work_type_name / unit /
      applicability_level / production_mode / labor_type / structure_mapping_absent。

    规模**一律现算**（`len(closed_l4_set(kb_scope))`），绝不在测试或提示词里写死 408 ——
    闭集随建筑类型 / 结构类型变化（父代理裁决 #1 ★③）。
    取数顺序确定：`sorted(l4_candidates)`，同一 `activity_id` 出现在两个工种时**第一个胜**
    （不覆盖），保证同一份 `kb_scope` 永远得到同一份闭集。
    """
    scope = kb_scope if isinstance(kb_scope, Mapping) else {}
    wt_names: Dict[str, str] = {}
    for row in scope.get("l3_list") or []:
        if isinstance(row, Mapping) and row.get("work_type_id"):
            wt_names[as_text(row.get("work_type_id"))] = as_text(row.get("work_type_name"))

    cands = scope.get("l4_candidates")
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(cands, Mapping):
        return out
    for wt in sorted(cands.keys(), key=as_text):
        wtd = as_text(wt)
        items = cands.get(wt) or []
        if not isinstance(items, (list, tuple)):
            continue
        for it in items:
            if not isinstance(it, Mapping):
                continue
            aid = as_text(it.get("activity_id")).strip()
            if not aid or aid in out:
                continue
            out[aid] = {
                "activity_id": aid,
                "activity_name": as_text(it.get("activity_name")) or aid,
                "work_type_id": wtd,
                "work_type_name": wt_names.get(wtd) or wtd,
                "unit": as_text(it.get("unit")),
                "applicability_level": it.get("applicability_level"),
                "production_mode": as_text(it.get("production_mode")),
                "labor_type": as_text(it.get("labor_type")),
                "structure_mapping_absent": bool(it.get("structure_mapping_absent")),
            }
    return out


def in_tree_l4_index(wbs: Any) -> Dict[str, List[Dict[str, Any]]]:
    """`{activity_id: [leaf, …]}` —— 按 `leaf["kb_activity_id"]` 归集（设计 §5.1）。

    每个 L4 下的叶子**按 `id` 升序**（设计 §8.2 的顺序确定性要求）。
    """
    idx: Dict[str, List[Dict[str, Any]]] = {}
    for leaf, _p, _w in iter_leaves(wbs):
        if not isinstance(leaf, Mapping):
            continue
        aid = as_text(leaf.get("kb_activity_id")).strip()
        if not aid:
            continue
        idx.setdefault(aid, []).append(leaf)
    for aid in idx:
        idx[aid].sort(key=leaf_id)
    return idx


def in_tree_l4_set(wbs: Any) -> set:
    """树里出现过的 `kb_activity_id` 集合。"""
    return set(in_tree_l4_index(wbs).keys())


# ======================================================================
# 2. 用户逐 L4 量（唯一权威的覆盖通道）
# ======================================================================
def user_l4_quantities(params: Any) -> Dict[str, float]:
    """`extracted_params["l4_quantities"]` → `{activity_id: 有限数}`。

    两种形状都认，**与 `kb_scope._zero_quantity_activities` 的既有契约逐字一致**：
      · `{"ACT_ID": qty}`
      · `{"work_type_id": {"ACT_ID": qty}}`
    值不是有限数的条目**直接不进结果**（不猜、不用 0 充数）；`<= 0` 的条目由调用方按
    `QTY_ZERO_TOL` 判"不覆盖"并记 `ignored_user_ids`（设计 §7.1 第 1 行）。
    """
    raw = (params or {}).get("l4_quantities") if isinstance(params, Mapping) else None
    if not isinstance(raw, Mapping):
        return {}
    out: Dict[str, float] = {}
    for k, v in raw.items():
        if isinstance(v, Mapping):
            for aid, q in v.items():
                if is_finite_number(q):
                    out[as_text(aid).strip()] = float(q)
        elif is_finite_number(v):
            out[as_text(k).strip()] = float(v)
    return {k: v for k, v in out.items() if k}


def ignored_user_ids(params: Any) -> List[str]:
    """`l4_quantities` 里值 `<= QTY_ZERO_TOL`（于是**不覆盖**）的 `activity_id`（升序）。

    ⚠️ 这个键是**两用**的：`KBScopeNode.run` 会把 `ratio_scope.build` 的产物写进同一键
    （"量 0 出局"的 L4 也会写成 0.0），用户自己也能给。所以这里如实报"给了 0/负数、
    因此没覆盖"，**不**声称它一定是用户给的（口径见 `user_override_of`）。
    """
    return sorted(aid for aid, q in user_l4_quantities(params).items()
                  if abs(float(q)) <= QTY_ZERO_TOL)


def user_override_of(params: Any, aid: str) -> Optional[float]:
    """用户**显式覆盖**该 L4 的量（没有 → None）。

    为什么不能"只要 `l4_quantities` 里有这个键就算用户给的"：`KBScopeNode.run` 在
    「②占比表」那一跳把 `ratio_scope.build` 的结果**写进了同一个键**（设计 §2.1）——
    照字面判会把占比表拆出来的量全记成"用户指定"，`source` 与交付物一起错。

    判据（与设计 §7.1 第 2 行"或 `aid ∈ l4_quantities` 且该值来自 `ratio_scope.build`"配套）：
      · 该 L4 **不在**占比表 `l4_index` 里 → 是用户给的（占比表对它没有发言权）；
      · 在 `l4_index` 里、但 `l4_quantities` 的值与占比表的值**不同** → 用户覆盖了它；
      · 与占比表的值**相同**（浮点容差内）→ 就是占比表那一条，按 `ratio` 计（不是用户）。
    """
    q = user_l4_quantities(params).get(aid)
    if q is None or abs(float(q)) <= QTY_ZERO_TOL:
        return None
    idx_q = ratio_quantity_of(params, aid)
    if idx_q is not None and abs(float(q) - float(idx_q)) <= max(QTY_ZERO_TOL,
                                                                abs(float(idx_q)) * 1e-9):
        return None
    return float(q)


def ratio_l4_index(params: Any) -> Dict[str, Dict[str, Any]]:
    """`params["_component_ratio"]["l4_index"]`（无 → `{}`）—— 占比表拆分的**唯一数据库口径**。"""
    ctx = (params or {}).get(RATIO_CTX_KEY) if isinstance(params, Mapping) else None
    idx = (ctx or {}).get(RATIO_L4_INDEX_KEY) if isinstance(ctx, Mapping) else None
    return dict(idx) if isinstance(idx, Mapping) else {}


def ratio_quantity_of(params: Any, aid: str) -> Optional[float]:
    """占比表给该 L4 的量（`> QTY_ZERO_TOL` 才算；否则 None —— 量 0 出局无发言权）。"""
    row = ratio_l4_index(params).get(aid)
    if not isinstance(row, Mapping):
        return None
    q = row.get("quantity")
    if not is_finite_number(q):
        return None
    f = float(q)
    return f if f > QTY_ZERO_TOL else None


def ratio_formula_of(params: Any, aid: str) -> str:
    """占比表拆分的**中文公式**（把"这个数怎么来的"写成一句人话，供交付物逐行展示）。"""
    row = ratio_l4_index(params).get(aid)
    if not isinstance(row, Mapping):
        return ""
    unit = as_text(row.get("unit"))
    return ("占比表拆分：total_%s × %s%%（Component_Ratio %s|%s，结构 %s）%s"
            % (as_text(row.get("work_type_id")), row.get("ratio_percent"),
               as_text(row.get("structure_type_id")), aid,
               as_text(row.get("structure_type_id")),
               ("；单位 %s" % unit) if unit else ""))


# ======================================================================
# 3. 优先级判定（user > ratio > tree > llm）
# ======================================================================
def leaf_qty_source_set(leaves: Sequence[Mapping[str, Any]]) -> set:
    """该 L4 全部叶子的 `_qty_source` 取值集合（去掉空值）。"""
    return {as_text((l or {}).get("_qty_source")) for l in (leaves or [])
            if as_text((l or {}).get("_qty_source"))}


def fill_source_of(aid: str, params: Any, leaves: Sequence[Mapping[str, Any]] = (),
                   coverage_item: Any = None) -> Optional[str]:
    """该 L4 的**量来源**，优先级 `user > ratio > tree > llm`（设计 §7.1 判据表）。

    * `user`  —— `params["l4_quantities"][aid]` 是**用户显式覆盖**（见 `user_override_of`）；
    * `ratio` —— 占比表 `l4_index[aid]["quantity"] > QTY_ZERO_TOL`，**或**树里叶子的
                 `_qty_source == "占比表拆分"` 且叶子量和 > 容差；
    * `tree`  —— 叶子 `_qty_source ∈ {"参数推算","基线默认"}` 且叶子量和 > 容差；
    * `llm`   —— 上面三项都取不到（`coverage_item` 里带 `llm_raw` 时才算"模型给过"）。

    ⚠️ 这条顺序是**硬要求**：`tree` 必须排在 `llm` 之前，否则 LLM 会覆盖
    `STEP_SPECS` 那 11 道既有系数路径的量（设计 §11.1 冲突 #3 / R2），
    `test_baseline_qty.py` / `test_qty_derive.py` 会红。

    ⚠️ 调用方传入的 `leaves` **应当是"不是本节点上一轮产物"的那些叶子**
    （`leaf["_qty_frozen_by"] != "quantity_fill"`）—— 否则重跑时"上一轮补的量"
    会被当成"既有系数路径的量"，`_qty_provenance` 从 `llm` 退回 `tree`，重跑不再逐位一致。
    """
    q = user_override_of(params, aid)
    if q is not None:
        return SOURCE_USER
    if ratio_quantity_of(params, aid) is not None:
        return SOURCE_RATIO
    srcs = leaf_qty_source_set(leaves)
    s = leaf_qty_sum(leaves)
    if LEAF_SOURCE_RATIO in srcs and s > QTY_ZERO_TOL:
        return SOURCE_RATIO
    if (srcs & {LEAF_SOURCE_PARAM, LEAF_SOURCE_BASE}) and s > QTY_ZERO_TOL:
        return SOURCE_TREE
    if isinstance(coverage_item, Mapping) and coverage_item.get("llm_raw"):
        return SOURCE_LLM
    return None


# ======================================================================
# 4. 单位：目标单位（字典单位）+ 换算（只调 kb_units 公开函数）
# ======================================================================
def l4_dict_unit(activity_id: str) -> str:
    """`L4_Activity_Dictionary.unit`（**惰性**只读；取不到 → ""）。

    为什么不 import 到模块顶层：`ratio_scope` 会 `import kb`，而 `quantity_scope` 的其余
    函数全是纯函数、离线可测。惰性 import 与 `kb_scope._is_zero` 的既有做法一致。
    """
    try:
        from .ratio_scope import l4_unit_map       # 惰性：破"纯函数层"与"读库"的耦合
        return as_text((l4_unit_map() or {}).get(as_text(activity_id).strip()))
    except Exception:                              # noqa: BLE001 — 读不到就是"没有单位"
        return ""


def norm_units_of(activity_id: str) -> set:
    """该 L4 全部定额行的 `quantity_unit`（人工 + 机械），已归一；取不到 → 空集。

    只作**交叉校验**，不改任何值（设计 §5.3 规则 2）。
    """
    out = set()
    try:
        from . import kb                             # 惰性：同 l4_dict_unit
        for row in (kb.labor_norms(activity_id) or []):
            u = kb_units.normalize_unit((row or {}).get("quantity_unit"))
            if u:
                out.add(u)
        for row in (kb.equipment_norms(activity_id) or []):
            u = kb_units.normalize_unit((row or {}).get("quantity_unit"))
            if u:
                out.add(u)
    except Exception:                                # noqa: BLE001
        return set()
    return out


#: 字典里"这一格不是单位"的**占位写法**（实测唯一一例：`GD_A13_截凿桩头` 的
#: `L4_Activity_Dictionary.unit = "见表"`，见设计 §2.6 / §5.3 规则 1）。
#: `kb_units.normalize_unit` 对认不出的写法**原样返回**（不猜），而 `unit_family("见表")`
#: 会给出 `count:见表`（≠ "没有单位"）—— 所以这一档只能在这里显式点名：
#: 宁可按"取不到单位"如实报缺（量原样保留、不阻断），也不把一个占位符当成计量单位。
_UNIT_PLACEHOLDERS = ("见表", "见说明", "见备注", "无", "无单位", "-", "—", "/", "？", "?")


def norm_denominator_unit(activity_id: str,
                          dict_unit: Optional[str] = None,
                          norm_units: Optional[set] = None,
                          ) -> Tuple[str, str, str]:
    """该 L4 的**目标单位**（= 字典单位）+ 定额侧交叉校验证据。

    返回 `(unit, evidence, note)`，`evidence ∈ {"dict","dict+norm_agree","dict+norm_differs",
    "dict_only","unresolved"}`：

      · 字典单位取不到（空 / 占位符「见表」）→ `("", "unresolved", 中文说明)`，
        量原样保留、**不阻断**（设计 §5.3 规则 1）；
      · `U` 非空且 `target ∈ U` → `dict+norm_agree`（正常路径，实测 173/174）；
      · `U` 非空且 `target ∉ U` → `dict+norm_differs`，**逐条留痕**，量仍按 target 冻结
        （不替 KB 改口径；父代理裁决 #2 ★：这一档的条数必须进 summary 与交付物，
        不许静默退化）；
      · `U` 为空 → `dict_only`。

    `dict_unit` / `norm_units` 可注入（离线单测用）；不注入时惰性只读 KB。
    """
    raw = l4_dict_unit(activity_id) if dict_unit is None else as_text(dict_unit)
    target = kb_units.normalize_unit(raw)
    if not target or target in _UNIT_PLACEHOLDERS:
        return ("", "unresolved",
                "该 L4 在 L4_Activity_Dictionary 上没有可用单位（原文 %r），"
                "无法确定目标单位；量按原值保留" % (raw,))
    U = norm_units_of(activity_id) if norm_units is None else set(norm_units or ())
    if not U:
        return (target, "dict_only", "")
    if target in U:
        return (target, "dict+norm_agree", "")
    return (target, "dict+norm_differs",
            "字典单位「%s」不在该 L4 的定额分母单位集合 %s 里 —— 量仍按字典单位冻结，"
            "由下游 norm_bind 按既有 check_unit_pair 判 convertible/unusable"
            % (target, "、".join(sorted(U))))


#: 与 `norm_bind._MAGNITUDE_BANDS`（`nodes\\norm_bind.py:166`）**同源**（复制 + 注释，不 import）。
#: 父代理裁决 #4：(a) 复制 + 同源注释。**改一处必须同步改另一处**
#: （`test_quantity_agent.py::test_magnitude_bands_same_as_norm_bind` 钉着）。
#: 形态：族对 → (参数名, 下界, 上界, 单位, 说明)。只拦"离谱"，不做业务判断。
_MAGNITUDE_BANDS = {
    ("count:根", "length"): ("pile_length_m", 0.3, 200.0, "m", "单根桩长"),
    ("length", "count:根"): ("pile_length_m", 0.3, 200.0, "m", "单根桩长"),
    ("count:根", "volume"): ("volume_per_pile_m3", 0.001, 500.0, "m³", "单根体积"),
    ("volume", "count:根"): ("volume_per_pile_m3", 0.001, 500.0, "m³", "单根体积"),
    ("mass", "volume"): ("density_t_per_m3", 0.1, 25.0, "t/m³", "材料容重"),
    ("volume", "mass"): ("density_t_per_m3", 0.1, 25.0, "t/m³", "材料容重"),
    ("area", "volume"): ("thickness_m", 0.005, 1.5, "m", "构件厚度"),
    ("volume", "area"): ("thickness_m", 0.005, 1.5, "m", "构件厚度"),
    ("count:件", "mass"): ("unit_weight_kg_per_piece", 0.01, 200000.0, "kg", "单件重量"),
    ("mass", "count:件"): ("unit_weight_kg_per_piece", 0.01, 200000.0, "kg", "单件重量"),
}


def band_of(from_unit: str, to_unit: str):
    """跨族换算的合理带 `(param, lo, hi, unit, label)`；同族 / 不可换算 → None。

    与 `norm_bind._band_of` 同源（只读 `kb_units.unit_family`）。
    """
    fu, tu = kb_units.unit_family(from_unit), kb_units.unit_family(to_unit)
    if not fu or not tu or fu == tu:
        return None
    return _MAGNITUDE_BANDS.get((fu, tu))


def band_reject(from_unit: str, to_unit: str, ctx: Any) -> str:
    """D5 量级校验：**换算参数本身**是否离谱。离谱 → 中文拒绝说明，否则 ""。

    与 `norm_bind._band_reject` 同源。**不提供任何取值**，只判合理性。
    """
    band = band_of(from_unit, to_unit)
    if not band:
        return ""
    param, lo, hi, unit, label = band
    val = (ctx or {}).get(param)
    if not is_finite_number(val):
        return ""
    f = float(val)
    if f <= 0:
        return ""
    if f < lo or f > hi:
        return ("换算参数「%s」= %g %s 超出合理带 %g~%g %s（来源：工程量补量换算）"
                % (param, f, unit, lo, hi, unit))
    return ""


def convert_to_target(qty: Any, from_unit: str, target_unit: str, ctx: Any = None,
                      ) -> Tuple[float, str, str, str]:
    """把一个量换到目标单位。返回 `(quantity, method, evidence, note)`。

    `evidence` 取值：
      · `"dict"`       —— 同单位（或源单位缺失）：**原值返回，不重写、不四舍五入**
                          （父代理裁决 #5：避免撞 `test_unit_area_volume.py` 的
                          「工程量一个字都不许改」断言）；
      · `"ok"`         —— 换算成功，返回值已 `round(..., 6)`（与
                          `binding["basis_adjust"]["adjusted_quantity"]` 的 6 位口径一致）；
      · `"unresolved"` —— 目标单位缺失或换算不出来：**原值返回 + 中文 note**，
                          由调用方判 `unit_unresolved`；**绝不 1:1 兜底**；
      · `"rejected"`   —— 换算参数离谱：原值返回 + 中文 note（离谱即拒，如实报缺）。
    """
    q = float(qty) if is_finite_number(qty) else 0.0
    fu = kb_units.normalize_unit(from_unit)
    tu = kb_units.normalize_unit(target_unit)
    if not tu:
        return (q, "", "unresolved",
                "该 L4 在 L4_Activity_Dictionary 上没有单位，无法确定目标单位")
    if not fu or fu == tu:
        return (q, "同单位", "dict", "")
    conv = kb_units.convert(q, fu, tu, ctx or {})
    if conv is None:
        return (q, "", "unresolved", kb_units.next_step_hint(fu, tu, ctx or {}))
    bad = band_reject(fu, tu, ctx or {})
    if bad:
        return (q, "", "rejected", bad)
    val, method = conv
    return (round(float(val), 6), method, "ok", "")


# ======================================================================
# 5. 分批 / 分配到叶子 / 缺口判据
# ======================================================================
def plan_batches(items: Sequence[Any], size: int = BATCH_SIZE) -> List[List[Any]]:
    """把条目切成批次（切分只依赖入参顺序 —— 调用方传 `sorted(...)`）。"""
    step = max(1, int(size or BATCH_SIZE))
    seq = list(items or [])
    return [seq[i:i + step] for i in range(0, len(seq), step)]


def split_weights(leaves: Sequence[Mapping[str, Any]]) -> Tuple[List[float], bool]:
    """几何权重 `w_i = segment_area_i / Σ segment_area`，返回 `(weights, uniform)`。

    `Σ == 0`（几何取不到）→ 均匀 `1/n` 且 `uniform=True`，调用方据此留痕
    `{"code": "llm_qty_uniform_split"}`（设计 §8.3 第 3 条）。
    """
    seq = list(leaves or [])
    n = len(seq)
    if n <= 0:
        return ([], True)
    areas = []
    for l in seq:
        a = (l or {}).get("segment_area")
        areas.append(float(a) if is_finite_number(a) and float(a) > 0 else 0.0)
    total = math.fsum(areas)
    if total <= 0:
        return ([1.0 / n] * n, True)
    return ([a / total for a in areas], False)


def distribute_to_leaves(leaves: Sequence[Mapping[str, Any]], total: Any,
                         unit: str = "") -> Tuple[Dict[str, float], bool]:
    """把一个 L4 的总量按几何权重分到它的叶子上。

    返回 `(leaf_id → 量, uniform)`，量已 `round(..., 2)`（与 `layer_engine._make_leaf` 同口径）。

    ⚠️ **不保证 `Σ round(2) == total`**（浮点必然差几分），差额写进
    `coverage.l4[aid]["leaf_sum"]` 与 `summary.rounding_note`，**不做回填**——
    域 5.5 明令"覆盖后不做守恒回算"。只有一片叶子时 `= round(total, 2)`（最常见情形）。
    """
    seq = list(leaves or [])
    n = len(seq)
    if n <= 0:
        return ({}, False)
    tot = float(total) if is_finite_number(total) else 0.0
    if n == 1:
        return ({leaf_id(seq[0]): round(tot, 2)}, False)
    weights, uniform = split_weights(seq)
    out = {}
    for leaf, w in zip(seq, weights):
        out[leaf_id(leaf)] = round(tot * float(w), 2)
    return (out, uniform)


def coverage_gaps(coverage: Any) -> List[Dict[str, Any]]:
    """返回不满足「每个 L4 都有量」的条目。**空列表 = 可以进下一步**（设计 §5.2）。

    `status == "not_applicable"`（模型明确说"本项目没有"）**不算缺口** —— 它是表态，
    不是漏项。其余条目要求 `quantity` 是 `> 0` 的有限数。
    """
    cov = coverage if isinstance(coverage, Mapping) else {}
    bad = []
    for aid, it in sorted((cov.get("l4") or {}).items(), key=lambda kv: as_text(kv[0])):
        if not isinstance(it, Mapping):
            continue
        if it.get("status") == STATUS_NOT_APPLICABLE:
            continue
        q = it.get("quantity")
        if not is_finite_number(q) or not (float(q) > 0):
            bad.append(dict(it))
    return bad
