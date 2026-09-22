# -*- coding: utf-8 -*-
"""B3/B4/B5 接线层：`Component_Ratio`（知识库新表）→ 各构件工程量。

口径（用户 2026-09-21 明确裁定，见 `docs/修改项总清单_20260921.md:272` 第 15/17/18/19 条）
------------------------------------------------------------------------------------------
**新表是唯一真源**：把「工种总量」拆到各构件（L4）**只认** `Component_Ratio`
（分组 = 结构类型 × 工种 L3，组内 ∑ = 100）。旧的按施工阶段比例表
（`beat_configs.CONCRETE_RATIO` / `REBAR_RATIO` / `SECONDARY_CONCRETE_RATIO`）
**已从计算路径移除**，本模块**不会**回退去读它们。

唯一量链路
----------
```
① L4 总量 = params["total_<工种>"] × ratio_percent ÷ 100
② L4 层量 = L4 总量 × (该层面积 ÷ Σ各层面积)     ← segment_capacity.layer_distribution
③ L4 段量 = L4 层量 × (该段面积 ÷ 该层面积)     ← segment_capacity.segment_distribution
```
`②③` 是既有纯函数（`pipeline/segment_capacity.py:874` / `:894`，此前**源码级调用方 0 处**）；
本模块是**接线**：把 `params["floor_areas"]`（此前**计算侧 0 消费**）真正喂进去。

只读纪律
--------
本模块对 KB **只读**（`kb._query_all`，与 `kb_scope._all_l3_rows` 同一用法）：
不建表、不写库、不改 `component_ratio.py`（只读调用它的四条闭合校验）。

缺失降级（**绝不静默、绝不退旧表**）
----------------------------------
每一步降级都写进 `trace["degradations"]`，并随 `kb_scope["component_ratio"]` 进产物：
缺层面积 → 回退均摊并标注；占比表缺行 / 占比 0 → 报缺 + 量 0 出局；量纲不一致 → 不硬套，保留系数路径。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import kb
from . import segment_plan
from . import segment_capacity

__all__ = [
    "GROUP_TOTAL_PARAMS",
    "PILE_TOTAL_PARAM",
    "RATIO_ZERO_TOL",
    "QTY_ZERO_TOL",
    "SOURCE_COMPONENT_RATIO",
    "RATIO_CTX_KEY",
    "STRUCTURE_ID_KEY",
    # 【域 6｜桩基 = 基础】基础类型 → L4 绑定（代码内常量映射，不建表）
    "FOUNDATION_ANCHORS",
    "FOUNDATION_TYPE_MAP",
    "FOUNDATION_L4_BINDING",
    "PILE_TYPE_TARGETS",
    "GENERIC_PILE_TARGET",
    "FOUNDATION_KIND_PILE",
    "FOUNDATION_KIND_UNKNOWN",
    "CONSERVATION_MIGRATE",
    "CONSERVATION_COEXIST",
    "CONSERVATION_INPLACE",
    "BINDING_FROM_RATIO",
    "BINDING_FROM_USER_PARAM",
    "BINDING_NONE",
    "WARN_FOUNDATION_UNRESOLVED",
    "WARN_PILE_TYPE_UNRESOLVED",
    "foundation_type_of",
    "foundation_l4_targets",
    "foundation_binding_warnings",
    "pile_target_of",
    "work_type_totals",
    "ratio_rows_for_structure",
    "l4_l3_map",
    "l4_unit_map",
    "mapping_levels",
    "build",
    "l4_index_of",
    "l4_quantities_of",
    "run_closure_checks",
    "is_zero_quantity",
    "step_ratio_status",
    "exempt_activity_ids",
    "B4Distribution",
    "task_capacity_payload",
]

#: 工种(L3) → 项目参数键（**参与占比表拆分的活跃工种**）。
#: 「照抄」`kb_scope._quantity_strengthened_l3` 的既有映射里**在占比表内有分组**的那几项
#: （两处口径必须一致，否则「强化为 REQUIRED 的工种」与「被拆分的工种」会错位）。
#:
#: 【域 6 · 6.4】**`pile_foundation` 已从本表移除** —— 这是「桩基不是独立项，它就是基础」
#: 在代码语义上的落点，理由三条：
#:   ① **占比表里没有桩基分组**：`Component_Ratio` 实测 97 行只有 concrete 39 /
#:      rebar 34 / formwork 24 三个工种，`pile_foundation` **0 行**。把它留在这里，
#:      等于让「占比表拆分」去拆一个表里根本没有的工种 —— 实测后果就是**桩基 33 个 L4
#:      全部被判「异常缺行」→ 量 0 出局 → 不进树**（拿 `total_pile` 只会拆出 0）。
#:   ② **桩基的量就是「基础」栏的量**：口径总表第 10 条「基础 = 桩基（当项目基础类型为
#:      桩基）」+ 6.2「若基础类型是桩基，则"基础"栏的量就是桩基量」⇒ 桩量由
#:      `_bind_foundation_column` 从「基础」栏（`FOUNDATION_ANCHORS`）改投过去，
#:      **不**从 `total_pile` 单独拆一个工种分组。`total_pile` 仍然是**用户给的
#:      项目级桩总量**，被改投逻辑当作「直接量」消费（见 `_pile_total_of`），
#:      但**不再**给桩基 L4 写「表里没有该行」的假异常。
#:   ③ 6.4 的「占比项清单 = 基础、梁、板、柱、墙、楼梯…」在**占比表侧本来就成立**
#:      （97 行全是这些构件）；这里只是把「工种拆分清单」也收口到同一口径。
GROUP_TOTAL_PARAMS: Dict[str, str] = {
    "concrete": "total_concrete",
    "rebar": "total_rebar",
    "formwork": "total_formwork",
    "masonry": "total_masonry",
    "earthwork": "total_earthwork",
}

#: 用户给的**项目级桩总量**参数键（`total_pile`，**不预设单位**）。
#: 它不是占比表里的工种（见 `GROUP_TOTAL_PARAMS` 上方 ①），而是「桩」这件事的
#: 直接量来源；由 `_bind_foundation_column` 在桩基项目里消费。
PILE_TOTAL_PARAM: str = "total_pile"

#: 占比视同 0 的阈值（**百分点**）。
#: 取 `component_ratio.RATIO_TOLERANCE`（= 0.01）同一数值：低于表格自身守恒分辨率的占比
#: 无法与 0 区分。表里最小非零占比实测 1.6% → 阈值比最小真实值低 160 倍，不会误杀。
RATIO_ZERO_TOL: float = 0.01

#: 量视同 0 的阈值（**绝对量**，单位随工序）。
#: 为什么必须改：`kb_scope._is_zero` 原实现只认 `float(q) == 0.0`（精确零），
#: 而占比是浮点 → `总量 × ratio/100` 几乎不可能落到精确 0.0，于是「量0出局」在生产路径
#: 形同虚设。1e-6 比最小真实量（1.6% × 总量，≥ 0.016）低 4 个数量级、
#: 比浮点噪声（~1e-12）高 6 个数量级，两侧都留足余量。
QTY_ZERO_TOL: float = 1e-6

#: 叶子 `_qty_source` 的第四态：本工序的量按占比表拆分得到。
SOURCE_COMPONENT_RATIO: str = "占比表拆分"

#: `ctx["extracted_params"]` 里放占比索引/留痕的键。
RATIO_CTX_KEY: str = "_component_ratio"
#: `kb_scope` 已解析的结构类型 id（下游不必再踩 `resolve_structure_type` 的子串缺陷）。
STRUCTURE_ID_KEY: str = "_structure_type_id"
#: **占比表显式豁免集合**的注入键（用户 2026-09-21 裁定「路线 2」）。
#: 形状：`["ACT_ID", ...]` 或 `{"activity_ids": [...], "basis": {ACT_ID: 理由}}`。
#: 语义：这些 L4 **明确不参与**占比拆分（互斥做法族 / 工序条目），
#: 其量应来自**条件维择一**或**派生**（由别的模块负责），**不是**占比。
EXEMPT_CTX_KEY: str = "component_ratio_exempt"
#: 豁免集合的 KB 载体表名（数据侧尚未落库时不存在 → 视为空集，不报错）。
EXEMPT_TABLE: str = "Component_Ratio_Exempt"

#: 地下室类阶段名（层集合取 `floor_areas` 的负数层号）。
BASEMENT_PHASES = ("地下室结构",)


# ======================================================================
# 【域 6｜桩基 = 基础】基础类型 → L4 绑定（**代码内常量映射，不建表、不改占比表**）
# ======================================================================
# 用户裁决（原话，逐字保留）：
#   ① 「5.1占比表不应该是通用的吗，对不同项目，你这里改了，下个项目的基础类型变了
#      怎么办？**不改名**，应该判断出来该项目是什么类型基础，然后所有该项目相关的
#      基础工程量**都走占比表中的基础那一栏**。钢筋和模板也按这个方向处理。」
#   ② 「基础类型从参数中提取，应该新加一个键为基础类型……不同基础类型对应着可能有
#      不同的工序，一定要写清楚在 LLM 提示词里，按实际情况选取。」
#   ③ 总清单 `# 4. 口径总表` 第 10 条：「**基础 = 桩基**（当项目基础类型为桩基）；
#      占比表"基础"栏是**通用栏目**，不因项目改表」。
#   ④ A2 用户裁定：**不新建映射表** —— 基础类型 → L4 的绑定用**代码里的常量映射**
#      实现，**不许在 kb.db 里加表**。
#
# ⇒ 因此本模块**不改** `Component_Ratio` 的表结构与 `ratio_percent` 值（保持通用）：
#   「基础」栏永远是 `CONC_NEW_FOUND`/`REBAR_NEW_FOUND`/`FORM_NEW_FOUND` 三条
#   构件行（这就是「不改名」的含义）。对**桩基项目**，这三条行的量按下面的常量映射
#   改投到 `pile_foundation` 那把 L4 上 —— 于是「桩基不是独立项，它就是基础」
#   在**代码语义**上落地：桩基的量不是从 `total_pile` 单独拆出来的一个工种，
#   而是「占比表基础栏的量」换了个落点。
#
# 溯源（依据，改这张表必须同步改这一段）：
#   · 桩型 → L4 的对应，取自 `L4_Activity_Dictionary` 里 `work_type_id='pile_foundation'`
#     的 33 条实测活动名（广东省定额 A.1.3 桩基础工程，见 `L3_Work_Type.description`）；
#   · 每个 L4 的**量纲**取该表 `unit` 列实测值（m / m³ / t / 个 / 根 / 见表）；
#   · 基础类型词表取 `extractor._FOUNDATION_TYPES`（封闭表，长名优先）
#     + `prompts/extract_params.txt` 里明示的自由写法（桩基 / 预应力管桩 / 钻孔灌注桩 …）。
#   ⚠️ 这里是**唯一**的基础类型 → L4 绑定真源；`kb_scope` 只消费、不另造一套。

#: 「基础」栏（占比表里代表**基础/桩基**的构件行）→ 各工种的**通用**构件行 ID。
#: **不含桩基专用 L4**：桩基落点由下面的 `PILE_TYPE_TARGETS` 按基础类型决定。
#: 这张表是「占比表不改名」的**代码侧声明**：柱子/梁/板不会因为基础类型变了就换 ID，
#: 「基础」栏也一样 —— 变的是**量的落点**，不是栏目名。
FOUNDATION_ANCHORS: Dict[str, Tuple[str, ...]] = {
    "concrete": ("CONC_NEW_FOUND",),
    "rebar": ("REBAR_NEW_FOUND",),
    "formwork": ("FORM_NEW_FOUND",),
}

#: 桩型关键词 → **承接「基础」栏量的主工序 L4**（`pile_foundation` 组内）。
#: **长关键词优先**（`_pile_keyword_of` 按长度倒序匹配），所以
#: 「预应力管桩」不会被更短的「管桩」抢先（两者指向同一个 L4，此处只是次序纪律）。
#: 判据是**子串**：用户写「预应力高强混凝土管桩」「PHC 管桩」都能命中「管桩」。
PILE_TYPE_TARGETS: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    # ---- 预制桩：打（锤击）/ 压（静压），计量单位 m（实测 `unit` 列）----
    (("预应力管桩", "预制管桩", "管桩", "PHC", "预应力混凝土管桩"), "GD_A13_打管桩"),
    (("预制方桩", "方桩"), "GD_A13_打方桩"),
    (("钢管桩",), "GD_A13_打钢管桩"),
    # ---- 灌注桩：成孔，计量单位 m³ ----
    (("钻孔灌注桩", "钻孔桩", "灌注桩", "泥浆护壁"), "GD_A13_钻孔成孔"),
    (("冲孔桩", "冲孔灌注桩", "冲击成孔"), "GD_A13_冲孔成孔"),
    (("旋挖桩", "旋挖"), "GD_A13_旋挖成孔"),
    (("沉管灌注桩", "沉管桩", "夯扩桩"), "GD_A13_沉管灌注成孔"),
    (("CFG桩", "CFG"), "GD_A13_CFG桩成孔"),
    (("砂石桩", "碎石桩"), "GD_A13_砂石灌注桩"),
    (("微型桩",), "GD_A13_钻孔灌注微型桩"),
    (("圆木桩",), "GD_A13_打圆木桩"),
)

#: 桩型认不出来时的兜底主工序（`GENERIC_PILE_TARGET`）。
#: 为什么兜底到**钻孔灌注桩成孔**：它在 33 条桩基 L4 里是覆盖面最广的成孔工序
#: （`unit='m³'`，与「基础」栏的混凝土口径同量纲），且是 `Structure_Type_L4_Mapping`
#: 下 7 种结构类型全部 OPTIONAL 的通用项。**兜底必定留痕**（`pile_type_unresolved`），
#: 绝不静默。
GENERIC_PILE_TARGET: str = "GD_A13_钻孔成孔"

#: 基础类型 → 桩型关键词（**代码内常量映射，不建表**）。
#: 顺序即优先级；`_foundation_kind_of` 用「子串包含」判定，长名必须排在短名之前。
#: `combined=True` 表示**桩与现浇基础并存**（桩筏 / 桩承台）——此时
#: 「基础」栏的混凝土量**仍然是真的基础混凝土量**（筏板 / 承台），
#: 桩量另算 ⇒ 守恒模式为 `coexist`（两条都留）；纯桩基则为 `migrate`（原落点归零）。
FOUNDATION_TYPE_MAP: Tuple[Tuple[Tuple[str, ...], str, bool], ...] = (
    # 桩 + 现浇基础并存
    (("桩筏基础", "桩承台基础"), "pile", True),
    # 纯桩基（预制 / 灌注 / 各类桩型都归这一档，具体 L4 由 `_pile_keyword_of` 再分）
    (("桩基础", "桩基", "管桩", "方桩", "钢管桩", "灌注桩", "钻孔桩", "冲孔桩",
      "旋挖桩", "沉管桩", "微型桩", "CFG桩", "砂石桩", "圆木桩", "预应力管桩",
      "预制桩", "夯扩桩"), "pile", False),
    # 非桩：基础形式逐一登记（**它们的「基础」栏 L4 本来就是锚点行**，不需要换落点；
    #   登记进来是为了让「6.1 提取到了基础类型」可溯源、可回显，并让
    #   「基础类型是桩基 ⇒ 才改投桩基 L4」这条判据是**白名单**而不是黑名单）。
    (("筏板基础", "筏形基础", "满堂基础"), "mat", False),
    (("独立柱基", "独立基础", "杯口基础"), "spread", False),
    (("条形基础", "带形基础"), "strip", False),
    (("箱形基础", "箱型基础"), "box", False),
)

#: 非桩基础形式的 L4 绑定：**保持「基础」栏锚点行**（占比表不改名 ⇒ 落点也不改名）。
#: 这张表显式写出来，是为了让「基础类型 → 落点 L4」这件事**处处可查**，
#: 而不是靠 `FOUNDATION_ANCHORS.get(wt)` 的隐式缺省。
FOUNDATION_L4_BINDING: Dict[str, Tuple[str, ...]] = {
    "mat": FOUNDATION_ANCHORS["concrete"] + FOUNDATION_ANCHORS["rebar"]
           + FOUNDATION_ANCHORS["formwork"],
    "spread": FOUNDATION_ANCHORS["concrete"] + FOUNDATION_ANCHORS["rebar"]
              + FOUNDATION_ANCHORS["formwork"],
    "strip": FOUNDATION_ANCHORS["concrete"] + FOUNDATION_ANCHORS["rebar"]
             + FOUNDATION_ANCHORS["formwork"],
    "box": FOUNDATION_ANCHORS["concrete"] + FOUNDATION_ANCHORS["rebar"]
           + FOUNDATION_ANCHORS["formwork"],
}

#: 基础类型是**桩基**时，「基础」栏的量落到桩基 L4（`_pile_keyword_of` 再细分）。
FOUNDATION_KIND_PILE: str = "pile"
#: 基础类型认不出来时的档（**不静默**：留痕 + `foundation_type_unresolved`）。
FOUNDATION_KIND_UNKNOWN: str = "unknown"

#: 守恒模式（写进 trace / index，供下游与交付物解释「量为什么搬了家」）。
CONSERVATION_MIGRATE: str = "migrate"      # 纯桩基：原落点归零，量**搬家**到桩基 L4
CONSERVATION_COEXIST: str = "coexist"      # 桩 + 现浇基础并存：两条都留（量**不守恒**，如实标注）
CONSERVATION_INPLACE: str = "in_place"     # 非桩基础：落点不变（占比表原样）

#: `index[aid]["foundation_binding"]["status"]` 的取值（封闭三态）。
BINDING_FROM_RATIO: str = "ratio_table"    # 量来自占比表「基础」栏
BINDING_FROM_USER_PARAM: str = "user_param"  # 量来自用户给的项目级总量（`total_pile`）
BINDING_NONE: str = "none"                 # 未参与绑定

#: 兜底认不出基础类型时的**显式标注**（进 `kb_scope` 警告，绝不静默按某一种基础形式编）。
WARN_FOUNDATION_UNRESOLVED: str = (
    "未能从基础类型参数里认出基础形式（原值「%s」）——「基础」栏的量继续落在通用基础构件上，"
    "未改投任何桩基工序；请人工确认基础形式。"
)
#: 认不出桩型时的显式标注（兜底到通用成孔工序）。
WARN_PILE_TYPE_UNRESOLVED: str = (
    "基础类型判定为桩基，但认不出桩型（原值「%s」）——「基础」栏的量兜底落到通用成孔工序 %s，"
    "请人工确认桩型。"
)


def foundation_binding_warnings(ratio_info: Any) -> List[str]:
    """`build()` 的产物 → 给用户看的**基础类型绑定**警告（0~2 条，`kb_scope` 直接用）。

    只报两类「人必须知道」的事（其余留痕在 `trace["foundation_binding"]` 与
    `trace["degradations"]` 里，不在这里刷屏）：
      · 基础类型**认不出**（`foundation_type_unresolved`）—— 参数门本该拦住缺参，
        但值是自由文本，认不出时必须让人看见「没有按任何桩型/基础形式改投」；
      · 桩基但**桩型认不出**（`pile_type_unresolved`）—— 量兜底落到了通用成孔工序。

    输出里**不含内部键名**（用户实测投诉过「不要刻意使用英文和专业术语」）。
    """
    trace = (ratio_info or {}).get("trace") if isinstance(ratio_info, Mapping) else None
    fb = (trace or {}).get("foundation_binding") if isinstance(trace, Mapping) else None
    if not isinstance(fb, Mapping):
        return []
    out: List[str] = []
    codes = {_txt(d.get("code")) for d in ((trace or {}).get("degradations") or [])
             if isinstance(d, Mapping)}
    if "foundation_type_unresolved" in codes:
        out.append(WARN_FOUNDATION_UNRESOLVED % _txt(fb.get("foundation_type")))
    if "pile_type_unresolved" in codes:
        out.append(WARN_PILE_TYPE_UNRESOLVED % (_txt(fb.get("foundation_type")),
                                                _txt(fb.get("pile_target"))))
    return out


def foundation_type_of(params: Any) -> str:
    """从项目参数里取基础类型（**只读、不猜**）。

    `foundation_type` 自第 2 批起是 `REQUIRED_KEYS` + `ABSOLUTE_KEYS` 成员
    （`nodes/boundary.py`：提取不到即中断、试算也绕不过），所以生产路径上这里必有值；
    本函数**不做缺失兜底**，取不到就返回空串，由调用方留痕 —— 报错是参数门的职责，
    不是本模块的（本模块不许把缺参悄悄变成某一种基础形式）。
    """
    if not isinstance(params, Mapping):
        return ""
    return _txt(params.get("foundation_type")).strip()


def _foundation_kind_of(foundation_type: Any) -> Tuple[str, bool]:
    """基础类型原文 → `(档, 是否桩与现浇基础并存)`。

    判据是**子串包含**（长名优先，见 `FOUNDATION_TYPE_MAP` 的排序纪律）；
    认不出 → `(FOUNDATION_KIND_UNKNOWN, False)`（**不猜**，由调用方标注）。
    """
    text = _txt(foundation_type).strip()
    if not text:
        return FOUNDATION_KIND_UNKNOWN, False
    for keywords, kind, combined in FOUNDATION_TYPE_MAP:
        for kw in keywords:
            if kw and kw in text:
                return kind, combined
    return FOUNDATION_KIND_UNKNOWN, False


def _pile_keyword_of(foundation_type: Any) -> str:
    """基础类型原文 → 命中的**桩型关键词**（长度倒序，长名优先）；认不出 → `""`。"""
    text = _txt(foundation_type).strip()
    if not text:
        return ""
    hits = []
    for keywords, _aid in PILE_TYPE_TARGETS:
        for kw in keywords:
            if kw and kw in text:
                hits.append(kw)
    if not hits:
        return ""
    return sorted(hits, key=lambda s: (-len(s), s))[0]


def pile_target_of(foundation_type: Any) -> Tuple[str, bool]:
    """基础类型原文 → `(承接「基础」栏量的桩基主工序 L4, 是否命中具体桩型)`。

    认不出桩型 → `(GENERIC_PILE_TARGET, False)`，调用方据此发 `pile_type_unresolved`。
    """
    kw = _pile_keyword_of(foundation_type)
    if not kw:
        return GENERIC_PILE_TARGET, False
    for keywords, aid in PILE_TYPE_TARGETS:
        if kw in keywords:
            return aid, True
    return GENERIC_PILE_TARGET, False


def foundation_l4_targets(foundation_type: Any) -> Tuple[str, Tuple[str, ...], str, bool]:
    """基础类型原文 → `(档, 承接 L4 元组, 守恒模式, 是否命中具体桩型)`。

    这是「基础类型 → L4」的**唯一入口**（下游只许走它，不许自己拼 L4 名单）：
      · 桩基（纯）→ `(单一桩型 L4,)`，守恒模式 `migrate`（「基础」栏的量搬家到桩基 L4）；
      · 桩筏 / 桩承台 → `(单一桩型 L4,)`，模式 `coexist`（筏板/承台**和**桩都要）；
      · 非桩（筏板/独立/条形/箱形）→ `FOUNDATION_L4_BINDING[档]`，模式 `in_place`；
      · 认不出 → `((), in_place, False)`，调用方发 `foundation_type_unresolved`。
    """
    kind, combined = _foundation_kind_of(foundation_type)
    if kind == FOUNDATION_KIND_PILE:
        aid, resolved = pile_target_of(foundation_type)
        mode = CONSERVATION_COEXIST if combined else CONSERVATION_MIGRATE
        return kind, (aid,), mode, resolved
    if kind == FOUNDATION_KIND_UNKNOWN:
        return kind, (), CONSERVATION_INPLACE, False
    return kind, tuple(FOUNDATION_L4_BINDING.get(kind, ())), CONSERVATION_INPLACE, False


def _pile_members(l3: Mapping[str, Any]) -> List[str]:
    """KB 里 `work_type_id='pile_foundation'` 的全部 L4（排序，可复现）。"""
    return sorted(aid for aid, wt in l3.items() if wt == "pile_foundation")


# ======================================================================
# 小工具
# ======================================================================


def _num(value: Any) -> Optional[float]:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _txt(value: Any) -> str:
    return "" if value is None else str(value)


def _unit_norm(unit: Any) -> str:
    """单位归一：`m2` / `M²` / 方块平米(U+33A1) → `m²`。"""
    text = _txt(unit).strip().replace("\u33a1", "m²")
    text = text.replace("m2", "m²").replace("M²", "m²")
    return text.lower()


def _pos(value: Any) -> Optional[float]:
    f = _num(value)
    return f if (f is not None and f > 0) else None


# ======================================================================
# 只读 KB 读取（**不写库**）
# ======================================================================

_ROWS_CACHE: Dict[str, List[Dict[str, Any]]] = {}


def ratio_rows_for_structure(structure_type_id: str) -> List[Dict[str, Any]]:
    """某结构类型下的 `Component_Ratio` 全量行（只读；表缺失/查询失败 → `[]`）。

    返回每行 `{activity_id, ratio_percent, source_code, confidence, review_state, notes}`。
    """
    sid = _txt(structure_type_id).strip()
    if not sid:
        return []
    key = "ratio|" + sid
    hit = _ROWS_CACHE.get(key)
    if hit is not None:
        return [dict(r) for r in hit]
    rows = kb._query_all(  # noqa: SLF001 — 只读查询；kb 内部函数不抛异常
        "SELECT activity_id, ratio_percent, source_code, confidence, review_state, notes "
        "FROM Component_Ratio WHERE structure_type_id = ? ORDER BY activity_id", (sid,))
    out = [{
        "activity_id": _txt(r[0]),
        "ratio_percent": _num(r[1]),
        "source_code": _txt(r[2]),
        "confidence": _txt(r[3]),
        "review_state": _txt(r[4]),
        "notes": _txt(r[5]),
    } for r in rows if r and r[0]]
    _ROWS_CACHE[key] = out
    return [dict(r) for r in out]


def l4_l3_map() -> Dict[str, str]:
    """`{activity_id: work_type_id}` —— V1 分组键的工种来源（`L4_Activity_Dictionary`）。"""
    key = "l4_to_l3"
    hit = _ROWS_CACHE.get(key)
    if hit is not None:
        return dict(hit)
    rows = kb._query_all(  # noqa: SLF001
        "SELECT activity_id, work_type_id FROM L4_Activity_Dictionary")
    out = {_txt(r[0]): _txt(r[1]) for r in rows if r and r[0]}
    _ROWS_CACHE[key] = out
    return dict(out)


def l4_unit_map() -> Dict[str, str]:
    """`{activity_id: unit}` —— 量纲核对用（占比表按工种给的总量必须与 L4 单位同量纲）。"""
    key = "l4_unit"
    hit = _ROWS_CACHE.get(key)
    if hit is not None:
        return dict(hit)
    rows = kb._query_all(  # noqa: SLF001
        "SELECT activity_id, unit FROM L4_Activity_Dictionary")
    out = {_txt(r[0]): _txt(r[1]) for r in rows if r and r[0]}
    _ROWS_CACHE[key] = out
    return dict(out)


def mapping_levels(structure_type_id: str) -> Dict[str, str]:
    """`{activity_id: applicability_level}`（V2/V3 用；只读）。"""
    sid = _txt(structure_type_id).strip()
    if not sid:
        return {}
    rows = kb._query_all(  # noqa: SLF001
        "SELECT activity_id, applicability_level FROM Structure_Type_L4_Mapping "
        "WHERE structure_type_id = ?", (sid,))
    return {_txt(r[0]): _txt(r[1]) for r in rows if r and r[0]}


def clear_cache() -> None:
    """清进程内只读缓存（测试用；生产一次运行里库不会变）。"""
    _ROWS_CACHE.clear()


def exempt_activity_ids(params: Any) -> Dict[str, Any]:
    """**占比表显式豁免集合**（用户 2026-09-21 裁定「路线 2」）。

    语义：这些 L4 **明确不参与**占比拆分 ——
      · **互斥做法族**（砖墙/砌块墙/石墙/空斗墙/ALC…）→ 条件维择一，每族只留一个代表项拿占比；
      · **工序条目**（安装/拆除/勾缝/地胎膜/花饰块组砌/阳台栏板安装…）→ 量应**派生**自其主体。
    ⇒ 它们**不按占比取量**，也**不是**"异常缺行"。

    取数优先级（都不改库，只读）：
      1. `params["component_ratio_exempt"]`（数据/上游注入的权威集合）；
      2. KB 表 `Component_Ratio_Exempt`（数据侧尚未落库时不存在 → 空集，**不报错**）。

    返回 `{"ids": set, "basis": {aid: 理由}, "source": "params"|"kb"|"none",
           "available": bool}`。
    `available=False` 表示**豁免集合当前不可得** —— 此时"既不在表里也不在豁免里"
    会被判为异常缺行，这是**保守但会误报**的口径，已在 trace 里留痕（见 BLOCKERS）。
    """
    ids: set = set()
    basis: Dict[str, str] = {}
    source = "none"
    raw = (params or {}).get(EXEMPT_CTX_KEY) if isinstance(params, Mapping) else None
    if isinstance(raw, Mapping):
        for aid in (raw.get("activity_ids") or []):
            ids.add(_txt(aid))
        for aid, why in (raw.get("basis") or {}).items():
            basis[_txt(aid)] = _txt(why)
        source = "params" if ids or basis else "none"
    elif isinstance(raw, (list, tuple, set)):
        for aid in raw:
            ids.add(_txt(aid))
        source = "params" if ids else "none"
    if not ids:
        cols = [r[1] for r in kb._query_all("PRAGMA table_info(%s)" % EXEMPT_TABLE)]  # noqa: SLF001
        if cols:
            sel = ", ".join(cols)
            for r in kb._query_all("SELECT %s FROM %s" % (sel, EXEMPT_TABLE)):  # noqa: SLF001
                row = dict(zip(cols, r))
                aid = _txt(row.get("activity_id") or row.get("activity_ids")).strip()
                if not aid:
                    continue
                ids.add(aid)
                for k in ("basis", "reason", "exempt_basis", "note", "notes"):
                    if row.get(k):
                        basis[aid] = _txt(row.get(k))
                        break
            if ids:
                source = "kb"
    return {"ids": ids, "basis": basis, "source": source, "available": bool(ids)}


# ======================================================================
# ① L4 总量
# ======================================================================


def work_type_totals(params: Any) -> Dict[str, Dict[str, Any]]:
    """「活跃工种」= 用户给了该工种的 `total_*` 且 > 0 → `{work_type_id: {param, value}}`。

    只在这张表里的工种才参与占比表拆分；没给总量的工种**不产出** `l4_quantities` 键，
    继续走既有系数路径（没有分母就没有可拆的东西），并在留痕里如实写明。
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(params, Mapping):
        return out
    for wt, key in GROUP_TOTAL_PARAMS.items():
        v = _pos(params.get(key))
        if v is None:
            continue
        out[wt] = {"param": key, "value": v}
    return out


def run_closure_checks(ratio_rows: Sequence[Mapping[str, Any]],
                       mapping_rows: Sequence[Mapping[str, Any]],
                       l4_to_l3: Optional[Mapping[str, Any]] = None,
                       wbs_landed: Optional[Sequence[Any]] = None,
                       exempt_activity_ids: Optional[Sequence[Any]] = None
                       ) -> Dict[str, Any]:
    """只读调用 `component_ratio.run_all_checks` 的 V1–V4（+V5）部分并返回精简结果。

    **不改 `component_ratio.py`**（另一代理在改它的 V1/V3 分组与豁免口径）；本函数只是调用方。

    路线 2 的「显式豁免」清单**必须下传**：否则校验器会把"不参与类"的 L4 当成
    「REQUIRED 却占比为 0」而报 V3 硬违规（误报）。豁免清单同时受 V5
    （`V5_ratio_on_exempt`：豁免项不许再拿占比）守卫。
    旧版 `component_ratio.py` 没有这两个参数 → 自动退回不带豁免的调用（不抛异常）。
    """
    from . import component_ratio as CR
    kwargs = {"l4_to_l3": l4_to_l3}
    if exempt_activity_ids:
        kwargs["exempt_activity_ids"] = list(exempt_activity_ids)
    try:
        res = CR.check_ratio_v1_v4(ratio_rows, mapping_rows, wbs_landed, **kwargs)
    except TypeError:
        res = CR.check_ratio_v1_v4(ratio_rows, mapping_rows, wbs_landed, l4_to_l3=l4_to_l3)
    return {
        "check": res.get("check"),
        "all_green": bool(res.get("all_green")),
        "stats": res.get("stats") or {},
        "violations": [{"code": v.get("code"), "severity": v.get("severity"),
                        "message": v.get("message")} for v in (res.get("violations") or [])],
        "warnings": [{"code": v.get("code"), "message": v.get("message")}
                     for v in (res.get("violations") or [])
                     if v.get("severity") == "warning"],
    }


def _base_quantity(total: float, pct: float, n_build: int) -> float:
    """① 的唯一算式：`L4 单栋量 = 工种总量 ÷ 栋数 × 占比 ÷ 100`（**不归一**）。

    ⚠️ 这里**故意不把占比归一化**（用户裁定：占比表照原样用，`group_sum_not_100`
    只如实记降级、不修正）。域 6 的改投也一样：搬的是**这个算式算出来的量**，
    不是「按桩型重新分配一遍占比」。
    """
    return float(total) / max(1, int(n_build)) * float(pct) / 100.0


def _dedupe_excluded(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """`excluded_zero_quantity` 去重（同一 `activity_id` 只留一条，信息量大的优先）。

    「基础」栏改投到桩基 L4 时，同一个桩基 L4 会先被主循环记一笔
    「表里没有该行 / 异常缺行」，再被改投逻辑记一笔「已改投到基础栏落点」——
    两条说的是同一件事，留后者（它说清了量去哪了），否则留痕会自相矛盾。
    """
    order = {"ratio_migrated_to_foundation": 0, "ratio_zero": 1, "abnormal_absent": 2}
    best: Dict[str, Dict[str, Any]] = {}
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        aid = _txt(r.get("activity_id"))
        cur = best.get(aid)
        if cur is None:
            best[aid] = r
            out.append(r)
            continue
        if order.get(_txt(r.get("kind")), 9) < order.get(_txt(cur.get("kind")), 9):
            out[out.index(cur)] = r
            best[aid] = r
    return out


def _migrate_anchors(*, wt: str, anchors: Sequence[str], quantities: Dict[str, float],
                     index: Dict[str, Dict[str, Any]], by_pair: Mapping[str, Any],
                     excluded_zero: List[Dict[str, Any]], exempt_ids: Any,
                     pile_aid: str, foundation_type: str, kind: str,
                     conservation: str) -> None:
    """纯桩基：把「基础」栏**锚点行**的量归零 + 留痕（量搬家了，不静默消失）。

    只对 `CONSERVATION_MIGRATE` 调用；`coexist`（桩筏/桩承台）下锚点行是**真的**
    筏板/承台混凝土量，必须原样保留。
    """
    for aid in anchors:
        if aid not in quantities:
            continue
        if aid in (exempt_ids or ()):               # 豁免项不参与占比取量，别动它
            continue
        old = quantities.get(aid)
        quantities[aid] = 0.0
        gi = index.get(aid)
        if isinstance(gi, dict):
            gi["quantity"] = 0.0
            gi["project_quantity"] = 0.0
            gi["foundation_binding"] = {
                "foundation_type": foundation_type, "kind": kind,
                "conservation": conservation, "status": "moved_to_pile",
                "target_l4": pile_aid,
                "note": "占比表「基础」栏的**通用落点**；本项目基础类型为桩基 ⇒ "
                        "量已按 6.2 改投到 %s（桩基），本行量归零" % pile_aid,
            }
        excluded_zero.append({
            "activity_id": aid, "work_type_id": wt,
            "ratio_percent": (by_pair.get(aid) or {}).get("ratio_percent"),
            "kind": "ratio_migrated_to_foundation",
            "moved_to": pile_aid, "moved_quantity": old,
            "reason": "基础类型=「%s」是桩基 ⇒ 占比表「基础」栏的量已按域 6.2 改投到桩基工序 %s"
                      "（原量 %r 归零，不重复计量）" % (foundation_type, pile_aid, old),
        })


def _pile_total_of(params: Any) -> Optional[float]:
    """用户给的**项目级桩总量** `params["total_pile"]`（> 0 才算；否则 `None`）。

    为什么不复用 `work_type_totals()`：`total_pile` **不是**占比表里的工种
    （见 `GROUP_TOTAL_PARAMS` 上方 ① —— 表里没有 `pile_foundation` 分组），
    它是「桩」的直接量，只在改投逻辑里消费，绝不参与「表里没有该行」的对账。
    """
    if not isinstance(params, Mapping):
        return None
    return _pos(params.get(PILE_TOTAL_PARAM))


def _charge_binder(
    *,
    foundation_type: str,
    target: str,
    kind: str,
    conservation: str,
    base_total: float,
    binder_pct: float,
    binder_work_type_id: str,
    n_build: int,
    units: Mapping[str, Any],
    levels: Mapping[str, Any],
    quantities: Dict[str, float],
    index: Dict[str, Dict[str, Any]],
    status: str,
    work_type_id: str,
    source_param: str = "",
) -> float:
    """把「基础」栏的一份量记到**实际基础类型的 L4**（`target`）上。

    返回记账用的**单价量**（单栋、单 L4 的估算量），供多工种汇总与留痕用。
    - `status == BINDING_FROM_USER_PARAM`：用户给了项目级总量（`total_pile`），
      该量**本身就是桩量** ⇒ 不再乘占比（否则等于给用户的数再打一次折），
      单价量 = `总量 ÷ 栋数`，`ratio_percent` 记 0（**没有**占比参与，如实标注）。
    - 否则：`量 = 工种总量 ÷ 栋数 × 占比 ÷ 100`（`_base_quantity`，与①同式）。
    """
    if status == BINDING_FROM_USER_PARAM:
        qty = float(base_total) / max(1, int(n_build))
        pct = 0.0
    else:
        qty = _base_quantity(base_total, binder_pct, n_build)
        pct = float(binder_pct)
    n_b = max(1, int(n_build))
    index[target] = {
        "structure_type_id": index.get(target, {}).get("structure_type_id", ""),
        "activity_id": target,
        "work_type_id": work_type_id,
        "ratio_percent": pct,
        "quantity": quantities.get(target, 0.0) + qty,
        "project_quantity": qty * n_b,
        "building_count": n_b,
        "unit": units.get(target, ""),
        "source_code": "",
        "confidence": "",
        "review_state": "",
        "notes": "由占比表「基础」栏按项目基础类型改投（source=%s）" % status,
        "applicability_level": levels.get(target, ""),
        "foundation_binding": {
            "foundation_type": foundation_type,
            "kind": kind,
            "conservation": conservation,
            "status": status,
            # 落点 L4 自己的工种（桩基）—— 与本行 `work_type_id` 同值，显式写出来便于阅读
            "target_work_type_id": work_type_id,
            # 量从哪个工种的「基础」栏来（concrete / rebar / formwork）
            "binder_work_type_id": binder_work_type_id,
            "binder_param": source_param,
            # 「基础」栏在该结构类型下的占比之和。`status=user_param` 时它**只作留痕**
            # （用户给的项目级总量没乘它，见本函数 docstring），不是算式的输入。
            "binder_ratio_percent": float(binder_pct),
            "binder_ratio_applied": status != BINDING_FROM_USER_PARAM,
            "note": "「基础」栏（占比表通用栏目，**未改名**）的量按**项目基础类型**落到本工序；"
                    "桩基项目下这就是桩量（口径总表第 10 条）。"
                    + ("本行量来自用户给的项目级总量，未参与占比折算。"
                       if status == BINDING_FROM_USER_PARAM else ""),
        },
    }
    quantities[target] = index[target]["quantity"]
    return qty


def _bind_foundation_column(*, foundation_type: str, totals: Mapping[str, Any],
                            by_pair: Mapping[str, Any], l3: Mapping[str, Any],
                            units: Mapping[str, Any], levels: Mapping[str, Any],
                            n_build: int, quantities: Dict[str, float],
                            index: Dict[str, Dict[str, Any]],
                            excluded_zero: List[Dict[str, Any]],
                            exempt_ids: Any,
                            pile_total: Optional[float],
                            degradations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """【域 6 · 6.2/6.3/6.4】按**项目基础类型**把「基础」栏的量落到实际基础类型的 L4。

    三条裁决在这里落地（原话见文件头「域 6｜桩基 = 基础」一节）：
      ① **占比表不改名**（保持通用）：锚点恒为 `FOUNDATION_ANCHORS` 那三条构件行，
         改的只是**量的落点**；
      ② **钢筋、模板同样处理**（6.3）：三个工种走**同一段逻辑**，不特判混凝土；
      ③ **桩基不是独立项**（6.4）：桩基项目下，桩量 = 「基础」栏的量（换落点），
         **不是**从 `total_pile` 单独拆出来的一个工种分组。

    落点与守恒模式（`foundation_l4_targets`，代码内常量映射、**不建表**）：
      · 纯桩基 → `migrate`：锚点行量归零 + 留痕「已改投」，量落到桩型对应的 L4；
      · 桩筏 / 桩承台 → `coexist`：锚点行**保留**（筏板/承台是真的现浇混凝土量），
        桩量另算 —— 这是**故意不守恒**的一档，`degradations` 里如实标注；
      · 非桩（筏板/独立/条形/箱形）→ `in_place`：落点不变，本函数不写任何量；
      · 认不出基础类型 → 不猜、不动量，只发 `foundation_type_unresolved`。

    ⚠️ **不报错**：`foundation_type` 缺失/认不出时「提取不到就报错」由参数门负责
    （`nodes/boundary.py` 的 `REQUIRED_KEYS` + `ABSOLUTE_KEYS`）；本模块是纯计算层，
    参数门没拦住时这里只**标注**并保持占比表原样，绝不擅自换一种基础形式。
    """
    info: Dict[str, Any] = {
        "foundation_type": _txt(foundation_type).strip(),
        "kind": FOUNDATION_KIND_UNKNOWN,
        "conservation": CONSERVATION_INPLACE,
        "target_l4": [],
        "applied": False,
        "pile_target": "",
        "pile_type_resolved": False,
        "unresolved_warning": "",
        "note": "「基础」栏 = 占比表里代表基础/桩基的通用构件行（CONC/REBAR/FORM_NEW_FOUND）；"
                "基础类型决定这些行的量落到哪个 L4（代码内常量映射，不改表、不加表）。",
    }
    if not info["foundation_type"]:
        return info

    kind, targets, conservation, resolved = foundation_l4_targets(info["foundation_type"])
    info.update({"kind": kind, "conservation": conservation,
                 "target_l4": list(targets), "pile_type_resolved": bool(resolved)})
    if kind == FOUNDATION_KIND_UNKNOWN:
        # 认不出基础形式：不动量、不猜，只标注（参数门本该拦住缺参，但值可能是自由文本）。
        info["unresolved_warning"] = WARN_FOUNDATION_UNRESOLVED % info["foundation_type"]
        degradations.append({
            "code": "foundation_type_unresolved",
            "foundation_type": info["foundation_type"],
            "message": info["unresolved_warning"],
        })
        return info
    if kind != FOUNDATION_KIND_PILE:
        # 非桩基础：占比表「基础」栏就是它的落点（落点不变 ⇒ 无需搬量），到此为止。
        info["applied"] = True
        info["note"] += " 本项目基础类型非桩基 ⇒ 落点不变（in_place），占比表原样使用。"
        return info

    pile_aid = targets[0] if targets else GENERIC_PILE_TARGET
    info["pile_target"] = pile_aid
    if not resolved:
        info["unresolved_warning"] = WARN_PILE_TYPE_UNRESOLVED % (
            info["foundation_type"], pile_aid)
        degradations.append({
            "code": "pile_type_unresolved",
            "foundation_type": info["foundation_type"],
            "fallback_l4": pile_aid,
            "message": info["unresolved_warning"],
        })

    # 逐工种（混凝土 / 钢筋 / 模板 —— 6.3「钢筋、模板同样处理」）搬量。
    # ⚠️ **同一目标 L4 只承接一次**：`pile_foundation` 的每个 L4 只属**一个**工种，
    # 落点也就只能有一个量纲。混凝土(m³)/钢筋(t)/模板(m²) 三份量若都往同一个 L4 上累加，
    # 会得到一个既不是 m³ 也不是 t 的**假数**（例如「打管桩」被加成 7589.8）。
    # 所以：第一份搬过去的量**认领**该 L4，其余工种如实记 `skipped_claimed` 降级，
    # 由下游（域 5 的「每个 L4 都要有量」）按该 L4 的定额单位补齐。
    charged: List[Dict[str, Any]] = []
    claimed_by: Dict[str, str] = {}
    for wt, anchors in sorted(FOUNDATION_ANCHORS.items()):
        meta = totals.get(wt)
        if not meta:
            continue                                    # 该工种没有用户总量 ⇒ 无从搬起
        binder_pct = 0.0
        for aid in anchors:
            row = by_pair.get(aid)
            pct = row.get("ratio_percent") if isinstance(row, Mapping) else None
            if pct is not None and float(pct) >= RATIO_ZERO_TOL:
                binder_pct += float(pct)
        if binder_pct <= 0.0:
            continue                                    # 「基础」栏在该结构类型下无占比
        if pile_aid in claimed_by:
            degradations.append({
                "code": "foundation_l4_already_claimed",
                "foundation_type": info["foundation_type"],
                "target_l4": pile_aid,
                "claimed_by_work_type": claimed_by[pile_aid],
                "skipped_work_type": wt,
                "skipped_ratio_percent": binder_pct,
                "message": "桩基工序 %s 已被工种 %s 的「基础」栏量认领（一个 L4 只接一个量纲）——"
                           "工种 %s 的「基础」栏量（占比 %s%%）未并入，交由下游按该 L4 的"
                           "定额单位补齐" % (pile_aid, claimed_by[pile_aid], wt, binder_pct),
            })
            charged.append({
                "work_type_id": wt, "binder_param": meta["param"],
                "binder_ratio_percent": binder_pct, "target_l4": pile_aid,
                "quantity": 0.0, "status": "skipped_claimed",
                "conservation": conservation, "anchors": list(anchors),
            })
            if conservation == CONSERVATION_MIGRATE:
                _migrate_anchors(wt=wt, anchors=anchors, quantities=quantities, index=index,
                                 by_pair=by_pair, excluded_zero=excluded_zero,
                                 exempt_ids=exempt_ids, pile_aid=pile_aid,
                                 foundation_type=info["foundation_type"], kind=kind,
                                 conservation=conservation)
            continue
        # 用户给了项目级总量（`total_pile`）⇒ 该量本身就是桩量，优先。
        use_param = (wt == "concrete" and pile_total is not None)
        status = BINDING_FROM_USER_PARAM if use_param else BINDING_FROM_RATIO
        source_total = float(pile_total) if use_param else float(meta["value"])
        qty = _charge_binder(
            foundation_type=info["foundation_type"], target=pile_aid, kind=kind,
            conservation=conservation, base_total=source_total, binder_pct=binder_pct,
            binder_work_type_id=wt,
            n_build=n_build, units=units, levels=levels, quantities=quantities,
            index=index, status=status, work_type_id="pile_foundation",
            source_param=("total_pile" if use_param else str(meta["param"])))
        claimed_by[pile_aid] = wt
        charged.append({
            "work_type_id": wt, "binder_param": meta["param"],
            "binder_ratio_percent": binder_pct, "target_l4": pile_aid,
            "quantity": qty, "status": status, "conservation": conservation,
            "anchors": list(anchors),
        })
        if conservation == CONSERVATION_MIGRATE:
            _migrate_anchors(wt=wt, anchors=anchors, quantities=quantities, index=index,
                             by_pair=by_pair, excluded_zero=excluded_zero,
                             exempt_ids=exempt_ids, pile_aid=pile_aid,
                             foundation_type=info["foundation_type"], kind=kind,
                             conservation=conservation)

    if conservation == CONSERVATION_COEXIST and charged:
        degradations.append({
            "code": "foundation_conservation_coexist",
            "foundation_type": info["foundation_type"],
            "pile_l4": pile_aid,
            "message": "基础类型「%s」= 桩 + 现浇基础**并存**：占比表「基础」栏（筏板/承台）与桩量"
                       "**同时保留**，合计大于原「基础」栏量 —— 这是如实标注的口径，不是算错"
                       % info["foundation_type"],
        })

    info["applied"] = bool(charged)
    info["charged"] = charged
    if charged:
        info["note"] += (" 命中桩基（%s）⇒ 「基础」栏 %s 的量改投到 %s（守恒模式 %s）。"
                         % (info["foundation_type"],
                            "、".join(c["work_type_id"] for c in charged), pile_aid, conservation))
    return info


def _buildings(params: Any) -> int:
    """项目栋数（与 `beat_configs.building_count` / `segment_plan` 同一口径）。

    多栋平行施工时 `total_*` 是**全项目**总量，而层面积/工期都是**单栋**口径
    （`per_building_params` 同此约定）⇒ 必须先把总量折成单栋，否则量会放大 N 倍。
    """
    if not isinstance(params, Mapping):
        return 1
    for key in ("building_count", "buildings", "building_num"):
        n = _num(params.get(key))
        if n is not None and n >= 1:
            return int(n)
    return 1


def build(params: Any, structure_type_id: str) -> Dict[str, Any]:
    """①把工种总量拆到 L4 → 写 `params["l4_quantities"]` 用的形状 + 逐行溯源索引 + 留痕。

    返回
    ----
    ```
    {
      "l4_quantities": {activity_id: 量},      # 仅活跃工种；表里没有 / 占比≈0 → 0.0
      "index": {activity_id: {ratio_percent, work_type_id, quantity, confidence,
                              review_state, notes, source_code, structure_type_id}},
      "trace": {...},                          # 进 kb_scope 留痕（含 degradations）
      "ratio_rows": [...], "mapping_rows": [...],
    }
    ```
    非活跃工种**不写键**（其 L4 不参与量0出局）。
    """
    params = params if isinstance(params, Mapping) else {}
    sid = _txt(structure_type_id).strip()
    totals = work_type_totals(params)
    n_build = _buildings(params)
    l3 = l4_l3_map()
    units = l4_unit_map()
    rows = ratio_rows_for_structure(sid)
    levels = mapping_levels(sid)

    by_pair: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        by_pair[r["activity_id"]] = r

    quantities: Dict[str, float] = {}
    index: Dict[str, Dict[str, Any]] = {}
    groups: Dict[str, Dict[str, Any]] = {}
    excluded_zero: List[Dict[str, Any]] = []
    degradations: List[Dict[str, Any]] = []
    exempt = exempt_activity_ids(params)
    exempt_seen: List[str] = []

    # 该结构类型下、各活跃工种在表里的行（用于组内 ∑ 与缺失行对账）
    for wt, meta in sorted(totals.items()):
        members = sorted(aid for aid, wid in l3.items() if wid == wt)
        rows_in_group = [r for r in rows if l3.get(r["activity_id"]) == wt]
        ssum = math.fsum(float(r["ratio_percent"] or 0.0) for r in rows_in_group)
        groups[wt] = {
            "param": meta["param"], "total": meta["value"],
            "table_rows": len(rows_in_group), "ratio_sum": ssum,
            "activity_ids_in_ratio_table": [r["activity_id"] for r in rows_in_group],
        }
        for aid in members:
            row = by_pair.get(aid)
            pct = row["ratio_percent"] if row is not None else None
            if pct is not None and float(pct) >= RATIO_ZERO_TOL:
                qty = float(meta["value"]) / n_build * float(pct) / 100.0
                quantities[aid] = qty
                index[aid] = {
                    "structure_type_id": sid,
                    "activity_id": aid,
                    "work_type_id": wt,
                    "ratio_percent": float(pct),
                    "quantity": qty,
                    "project_quantity": float(meta["value"]) * float(pct) / 100.0,
                    "building_count": n_build,
                    "unit": units.get(aid, ""),
                    "source_code": row.get("source_code") or "",
                    "confidence": row.get("confidence") or "",
                    "review_state": row.get("review_state") or "",
                    "notes": row.get("notes") or "",
                    "applicability_level": levels.get(aid, ""),
                }
                continue
            # ---- 路线 2（用户 2026-09-21 裁定）：显式豁免项 ≠ 异常 ----
            # 互斥做法族（条件维择一）/ 工序条目（量应派生自其主体）：
            # **不按占比取量**，也**不写 l4_quantities**（于是不会被"量0出局"误杀），
            # 量由别的模块按条件维/派生给出。这里只留痕，不发明派生规则。
            if aid in exempt["ids"]:
                exempt_seen.append(aid)
                continue
            quantities[aid] = 0.0
            if row is None:
                excluded_zero.append({
                    "activity_id": aid, "work_type_id": wt, "ratio_percent": None,
                    "kind": "abnormal_absent",
                    "reason": "既不在 Component_Ratio、也不在豁免集合里 → 异常缺行，如实报缺，"
                              "量 0 不进 WBS（不静默按占比给量）",
                })
            else:
                excluded_zero.append({
                    "activity_id": aid, "work_type_id": wt,
                    "ratio_percent": pct,
                    "kind": "ratio_zero",
                    "reason": "占比 %s < 容差 %g 个百分点 → 量 0，不进 WBS"
                              % (pct, RATIO_ZERO_TOL),
                })

    # 用户给了总量的工种之外：哪些 L4 被排除在外（不含在 l4_quantities 里）
    inactive = sorted({wt for wt in l3.values() if wt and wt not in totals})
    if inactive:
        degradations.append({
            "code": "no_user_total",
            "work_types": inactive,
            "message": "以下工种没有用户给的 total_* 参数，不参与占比表拆分"
                       "（占比表无从拆起，继续走既有系数路径并如实标源）：%s"
                       % "、".join(inactive),
        })
    for wt, g in sorted(groups.items()):
        if abs(float(g["ratio_sum"]) - 100.0) > 0.01:
            degradations.append({
                "code": "group_sum_not_100",
                "work_type_id": wt,
                "message": "组 %s|%s 的占比合计 %.6f ≠ 100" % (sid, wt, g["ratio_sum"]),
            })

    # ---- 【域 6｜桩基 = 基础】「基础」栏的量按**项目基础类型**改投实际落点 ----
    # 时机：②占比表这一跳（本函数）**内部**完成，下游拿到 `l4_quantities` 时
    # 桩基的量已经在桩基 L4 上 —— 于是「桩基不是独立项、它就是基础」是**结构性的**，
    # 不依赖任何提示词或下游记得再转一次。
    foundation_binding = _bind_foundation_column(
        foundation_type=foundation_type_of(params),
        totals=totals,
        by_pair=by_pair,
        l3=l3,
        units=units,
        levels=levels,
        n_build=n_build,
        quantities=quantities,
        index=index,
        excluded_zero=excluded_zero,
        exempt_ids=exempt["ids"],
        pile_total=_pile_total_of(params),
        degradations=degradations,
    )

    # 同组叠加：若「基础」栏改投到了 `pile_foundation`，那把 L4 已被 `excluded_zero`
    # 记过一笔「表里没有该行」，与改投留痕重复 —— 这里按 activity_id 去重，
    # 保留信息量更大的那条（`ratio_migrated_to_foundation` 优先）。
    excluded_zero = _dedupe_excluded(excluded_zero)


    check = run_closure_checks(
        [{"structure_type_id": sid, "activity_id": r["activity_id"],
          "ratio_percent": r["ratio_percent"]} for r in rows],
        [{"structure_type_id": sid, "activity_id": aid, "applicability_level": lv}
         for aid, lv in sorted(levels.items())],
        l4_to_l3=l3,
        exempt_activity_ids=sorted(exempt["ids"]))

    trace = {
        "source": "Component_Ratio",
        "structure_type_id": sid,
        "active_work_types": {wt: dict(meta, group=groups[wt]) for wt, meta in totals.items()},
        "inactive_work_types": inactive,
        "groups": groups,
        "excluded_zero_quantity": excluded_zero,
        # ---- 路线 2（2026-09-21）：显式豁免项与"异常缺行"分开标注 ----
        "exempt": {
            "source": exempt["source"],
            "available": exempt["available"],
            "activity_ids": sorted(set(exempt_seen)),
            "basis": {a: exempt["basis"].get(a, "") for a in sorted(set(exempt_seen))},
            "note": "豁免项 = 互斥做法族（条件维择一）或工序条目（量应派生自其主体）："
                    "**不按占比取量**，也不写 l4_quantities。豁免集合当前来源见 source；"
                    "取不到时 available=false（异常缺行会保守地判为异常，见 BLOCKERS）。",
        },
        "abnormal_absent": [r for r in excluded_zero if r.get("kind") == "abnormal_absent"],
        "degradations": degradations,
        "v1_v4": check,
        # ---- 【域 6｜桩基 = 基础】基础类型 → 「基础」栏量的落点 ----
        # 逐项可查：项目基础类型、判出的档、落点 L4、守恒模式、每个工种搬了多少。
        # 「桩基不是独立项、它就是占比表的基础栏」这件事在产物里**看得见**。
        "foundation_binding": foundation_binding,
        "note": "Component_Ratio 是把工种总量拆到各构件的**唯一**依据；"
                "旧按施工阶段比例表（CONCRETE_RATIO/REBAR_RATIO）已退役。",
    }
    return {
        "l4_quantities": quantities,
        "index": index,
        "trace": trace,
        "ratio_rows": rows,
        "mapping_rows": levels,
    }


# ======================================================================
# 下游读取（节拍引擎 / 校验）
# ======================================================================


def l4_index_of(params: Any) -> Dict[str, Dict[str, Any]]:
    """`params["_component_ratio"]["l4_index"]`（无 → `{}`）。"""
    ctx = (params or {}).get(RATIO_CTX_KEY) if isinstance(params, Mapping) else None
    idx = (ctx or {}).get("l4_index") if isinstance(ctx, Mapping) else None
    return dict(idx) if isinstance(idx, Mapping) else {}


def l4_quantities_of(params: Any) -> Dict[str, float]:
    """`params["l4_quantities"]`（无 → `{}`）。"""
    raw = (params or {}).get("l4_quantities") if isinstance(params, Mapping) else None
    out: Dict[str, float] = {}
    if isinstance(raw, Mapping):
        for k, v in raw.items():
            f = _num(v)
            if f is not None:
                out[_txt(k)] = f
    return out


def is_zero_quantity(q: Any) -> bool:
    """`量 == 0`（容差判定）。非数 / 空 → False（不当作 0，避免误杀）。"""
    f = _num(q)
    if f is None:
        return False
    return abs(f) <= QTY_ZERO_TOL


def _index_work_types(index) -> set:
    """占比索引里**实际出现过**的工种集合。

    优先取每行的 `work_type_id`，缺失时用 `l4_l3_map()` 按 `activity_id` 兜底
    （真实 `build()` 产物两者都有，进程内替身可能只给 `work_type_id`）。
    """
    l3 = l4_l3_map()
    out = set()
    for aid, row in (index or {}).items():
        wt = _txt((row or {}).get("work_type_id")).strip() or l3.get(aid, "")
        if wt:
            out.add(wt)
    return out


def step_ratio_status(params: Any, step: Mapping[str, Any],
                      phase_steps: Optional[Sequence[Mapping[str, Any]]] = None
                      ) -> Dict[str, Any]:
    """一道节拍工序在占比表里的处境（**唯一判据，节拍配置层与展开层共用**）。

    返回 `{"status": ..., "activity_id", "work_type_id", "info", "reason"}`：

    * `"ratio"`    —— 占比表里有该 L4 且占比 > 容差，**且本工序是该 L4 的认领者**
                      → 量走 ①总量 ②层量 ③段量。
    * `"missing"`  —— 该 L4 所属工种**有用户总量**（活跃），但表里没有该行 / 占比≈0
                      → **量0出局**（报缺 + 不进 WBS）。绝不退回旧阶段比例表。
    * `"inactive"` —— 该 L4 所属工种没有用户总量（或结构类型未识别 / 占比表不可用）
                      → 占比表无发言权，工序继续走既有系数路径（如实标源）。

    「认领者」规则：一个 L4 只被**第一道单位与 L4 单位同量纲**的工序认领，
    防止「两道工序共用同一 L4」被算两遍（样例实测：`ALC墙板安装`(m²) 与
    `砌块墙`(m³) 都指向 `LDT724_砌块墙`(m³) —— 前者不认领，保留系数路径）。
    """
    aid = _txt((step or {}).get("kb_activity_id")).strip()
    base = {"status": "inactive", "activity_id": aid, "work_type_id": "",
            "info": None, "reason": ""}
    if not aid:
        base["reason"] = "工序没有 kb_activity_id，无法对应占比表的 L4"
        return base
    index = l4_index_of(params)
    if not index:
        base["reason"] = "占比表不可用（结构类型未识别 / Component_Ratio 无数据）"
        return base
    l3 = l4_l3_map()
    wt = l3.get(aid, "")
    totals = work_type_totals(params)
    if not wt or wt not in totals:
        base["work_type_id"] = wt
        base["reason"] = ("工种 %s 没有用户给的 %s，占比表无发言权"
                          % (wt or "<unknown>", GROUP_TOTAL_PARAMS.get(wt, "total_*")))
        return base
    base["work_type_id"] = wt
    # ---- ★ 占比表**根本不覆盖**这个工种 → 无发言权，**不是**"逐 L4 异常缺行" ----
    # 真缺陷（第 5 批实测）：`Component_Ratio` 只有 concrete / rebar / formwork 三族
    # （97 行，`activity_id LIKE 'LDT%'` = **0 行**），而 `GROUP_TOTAL_PARAMS` 里还有
    # `masonry` / `earthwork`。用户只要在输入里写「砌体：约3000立方米」，
    # `extract_by_regex` 就会抽出 `total_masonry` → masonry 变"活跃工种" →
    # 二次结构与砌体的**每一道**工序都被判 missing → **整个分部量 0 出局、从 WBS 消失**。
    # 土方同型（总纲已记为已知现象）。
    #
    # 为什么这不算"放过缺陷"：两种情形语义不同 ——
    #   · 表里**有**该工种的行、但没有**这一行** ⇒ 真异常缺行，必须报缺且不进 WBS
    #     （例：concrete 族的 `CONC_NEW_BEAM`，`test_component_ratio_source` 钉着）；
    #   · 表里**连该工种都没有** ⇒ 占比表**不覆盖该工种**，属 `inactive`
    #     （"占比表无发言权"）⇒ 工序保留既有系数路径并如实标源。
    # 用户给的总量**不会被静默吞掉**：`_collect_degradations` 对
    # 「`inactive` 且 `work_type_id ∈ totals`」会记一条 `step_not_ratio_driven` 留痕。
    if wt not in _index_work_types(index):
        base["reason"] = ("占比表里没有工种 %s 的任何一行（Component_Ratio 不覆盖该工种）"
                          "⇒ 占比表无发言权；工序保留既有系数路径并如实标源。"
                          "用户给的 %s 无法按占比拆到 L4，需人工确认"
                          % (wt, GROUP_TOTAL_PARAMS.get(wt, "total_*")))
        return base
    # ---- 认领者判定（同量纲 + 先到先得）----
    if phase_steps:
        units = l4_unit_map()
        lu = _unit_norm(units.get(aid))
        owner = None
        for s in phase_steps:
            if _txt((s or {}).get("kb_activity_id")).strip() != aid:
                continue
            su = _unit_norm((s or {}).get("unit"))
            if lu and su and lu != su:
                continue                     # 不同量纲：不认领（见 docstring）
            owner = _txt((s or {}).get("name"))
            break
        if owner is not None and owner != _txt((step or {}).get("name")):
            base["reason"] = ("L4 %s 的占比表量已归工序「%s」，本工序保留既有系数路径"
                              "（防重复计量）" % (aid, owner))
            return base
        su = _unit_norm((step or {}).get("unit"))
        if lu and su and lu != su:
            base["reason"] = ("工序单位 %s 与占比表 L4 %s 的单位 %s 不同量纲 → 不硬套"
                              % (su, aid, lu))
            return base
    info = index.get(aid)
    if info is None or float(info.get("quantity") or 0.0) <= QTY_ZERO_TOL:
        # ---- 路线 2：先判「显式豁免」，再判「异常缺行」----
        ex = exempt_activity_ids(params)
        if aid in ex["ids"]:
            base["status"] = "exempt"
            base["reason"] = ("该 L4 属占比表**显式豁免项**（不参与类：互斥做法族 → 条件维择一，"
                              "或工序条目 → 量派生自其主体）：不按占比取量，也不是异常缺行。"
                              "豁免来源=%s；理由=%s"
                              % (ex["source"], ex["basis"].get(aid) or "（未给理由）"))
            return base
        base["status"] = "missing"
        base["info"] = info
        if info is None:
            base["reason"] = ("既不在 Component_Ratio、也不在豁免集合里 → **异常缺行**"
                              "（豁免集合 available=%s / source=%s）"
                              % (ex["available"], ex["source"]))
        else:
            base["reason"] = ("占比 %s < 容差 %g 个百分点 → 量 0"
                              % (_fmt(info.get("ratio_percent")), RATIO_ZERO_TOL))
        return base
    base["status"] = "ratio"
    base["info"] = info
    return base



# ======================================================================
# ②③ 分布分解：一个阶段的「节拍叶子 → 段量」
# ======================================================================


class B4Distribution:
    """把一个阶段内、占比表驱动的工序量按 B4 公式分解到「层 × 平面施工段」。

    参数
    ----
    params       : `ctx["extracted_params"]`（需含 `l4_quantities` / `_component_ratio` / `floor_areas`）
    phase        : 阶段名（用于判断地下室/地上层集合）
    layers_count : 该阶段有效层数（`layer_engine._eff_floors`）
    zones        : 该阶段平面施工段名列表（`layer_engine._eff_zones`，长度 = 段数）
    segs         : 竖向分段区间 `[(start, end_excl), …]`（`beat_configs.segment_floors`）
    steps        : 该阶段全部工序（主循环 + 挂靠措施），用于「一个 L4 只被一道工序认领」
    """

    def __init__(self, params: Any, phase: str, layers_count: float,
                 zones: Sequence[Any], segs: Sequence[Tuple[float, float]],
                 steps: Sequence[Mapping[str, Any]]) -> None:
        self.params = params if isinstance(params, Mapping) else {}
        self.phase = _txt(phase)
        self.zones = [str(z) for z in (zones or [])] or ["Ⅰ区"]
        self.n_zones = len(self.zones)
        self.segs = [(float(a), float(b)) for a, b in (segs or [])]
        self.layers = int(round(float(layers_count or 0))) or 0
        self.is_basement = self.phase in BASEMENT_PHASES
        self.steps = [s for s in (steps or []) if isinstance(s, Mapping)]
        self.index = l4_index_of(self.params)
        self.quantities = l4_quantities_of(self.params)
        self.totals = work_type_totals(self.params)
        self.l3 = l4_l3_map()
        self.units = l4_unit_map()
        self.degradations: List[Dict[str, Any]] = []
        self.excluded: List[Dict[str, Any]] = []
        self.floor_keys: List[str] = []
        self.floor_area_source = ""
        self._areas = self._resolve_layer_areas()
        self._weights, self._seg_rule, self._seg_note = self._resolve_segment_weights()
        self._collect_degradations()

    # ---------------- 认领/量纲的留痕（判据在 step_ratio_status，唯一实现）----------------

    def _collect_degradations(self) -> None:
        """逐工序跑一遍 `step_ratio_status`，把降级与量0出局**集中留痕**（不新增判据）。"""
        for step in self.steps:
            st = step_ratio_status(self.params, step, self.steps)
            if st["status"] == "missing":
                self.excluded.append({
                    "activity_id": st["activity_id"],
                    "step": _txt(step.get("name")),
                    "work_type_id": st["work_type_id"],
                    "kind": ("ratio_zero"
                             if (st.get("info") is not None) else "abnormal_absent"),
                    "ratio_percent": (st.get("info") or {}).get("ratio_percent"),
                    "reason": st["reason"] + " → 量 0，不进 WBS",
                })
            elif st["status"] == "exempt":
                self.degradations.append({
                    "code": "exempt_no_ratio",
                    "phase": self.phase,
                    "activity_id": st["activity_id"],
                    "step": _txt(step.get("name")),
                    "message": st["reason"] + " → 本工序**保留既有系数路径**（不按占比取量、"
                               "也不被量0出局剔除）；其量应由条件维择一 / 派生给出（见 BLOCKERS）",
                })
            elif st["status"] == "inactive" and st["activity_id"]:
                if (st["work_type_id"] in self.totals
                        or "认领" in st["reason"] or "量纲" in st["reason"]):
                    self.degradations.append({
                        "code": "step_not_ratio_driven",
                        "phase": self.phase,
                        "activity_id": st["activity_id"],
                        "step": _txt(step.get("name")),
                        "message": st["reason"],
                    })

    # ---------------- 层面积 ----------------

    def _resolve_layer_areas(self) -> List[float]:
        """该阶段的逐层面积（层号 1..layers，顺序 = 施工顺序）。

        来源优先 `params["floor_areas"]`（`scope_inputs.build_floor_areas` 产物，经
        `expand_floor_areas` 展平）；缺逐层面积 → **回退均摊**（`standard_floor_area`）
        并**如实标注**（E-1/E-2）。两条路都取不到 → 返回 `[]`（调用方退回既有系数路径）。
        """
        if self.layers <= 0:
            return []
        std = _pos(segment_plan.standard_floor_area_from_params(self.params))
        built = self.params.get("floor_areas") if isinstance(self.params, Mapping) else None
        source = _txt((built or {}).get("source")) if isinstance(built, Mapping) else ""
        per: Dict[str, float] = {}
        skipped: List[str] = []
        if isinstance(built, Mapping):
            from . import scope_inputs          # 惰性：避免包级循环导入
            per, skipped = scope_inputs.expand_floor_areas(built)

        if per:
            keys: List[str] = []
            if self.is_basement:
                keys = sorted([k for k in per if k.startswith("-")],
                              key=lambda s: int(s))              # 最底层在前（底板→顶板）
            else:
                keys = sorted([k for k in per if not k.startswith("-")],
                              key=lambda s: int(s))
            keys = keys[:self.layers]
            areas: List[float] = []
            missing = 0
            for i in range(1, self.layers + 1):
                if i <= len(keys):
                    a = _pos(per.get(keys[i - 1]))
                    if a is None:
                        missing += 1
                        a = std
                else:
                    missing += 1
                    a = None
                if a is None:
                    return []
                areas.append(float(a))
            self.floor_keys = keys + ["<%d>" % i for i in range(len(keys) + 1, self.layers + 1)]
            if missing:
                self.degradations.append({
                    "code": "floor_area_partial",
                    "phase": self.phase,
                    "message": "逐层面积只覆盖 %d/%d 层，其余 %d 层按均摊 %.2f m² 补齐（已标注）"
                               % (len(keys), self.layers, missing, std or 0.0),
                })
            self.floor_area_source = "user"
            if skipped:
                self.degradations.append({
                    "code": "floor_area_named_layers_skipped",
                    "phase": self.phase,
                    "keys": skipped,
                    "message": "名称层无法对上层号，已跳过（不猜层号）：%s" % "、".join(skipped),
                })
            return areas

        # 没有逐层面积 → 回退均摊（**必须标注**）
        if std is None:
            self.floor_keys = []
            self.floor_area_source = "none"
            self.degradations.append({
                "code": "floor_area_unavailable",
                "phase": self.phase,
                "message": "既没有逐层面积、也算不出标准层面积（缺 total_area / floors）"
                           "→ 本阶段不做 B4 分解，退回既有系数路径并如实标源",
            })
            return []
        self.floor_keys = ([str(-1 - i) for i in range(self.layers - 1, -1, -1)]
                           if self.is_basement else [str(i) for i in range(1, self.layers + 1)])
        self.floor_area_source = source or "average_assumption"
        label = ("层面积均摊（AI 假设：标准层面积 %.2f m²）" % std
                 if self.floor_area_source == "average_assumption"
                 else "层面积按标准层面积 %.2f m² 取等（source=%s）"
                      % (std, self.floor_area_source or "unknown"))
        self.degradations.append({
            "code": "floor_area_average_assumption",
            "phase": self.phase,
            "message": "没有逐层面积，%s —— 已标注，未静默" % label,
            "source": self.floor_area_source,
            "standard_floor_area": std,
        })
        return [float(std)] * self.layers

    # ---------------- 段面积权重 ----------------

    def _resolve_segment_weights(self) -> Tuple[List[float], str, str]:
        """平面施工段面积权重（长度 = 段数，∑ = 1）。

        段面积来自 `segment_plan.compute_segment_areas_ex`（B2 的 MSSA=500 规则；
        用户 `segment_rule` 优先）。段数与 `zones` 长度对不上 → 退回均匀权重并标注
        （B4 公式二的分母是"该层面积"，权重之和恒为 1，不会因此造假）。
        """
        n = self.n_zones
        uniform = [1.0 / n] * n
        std = _pos(segment_plan.standard_floor_area_from_params(self.params))
        if std is None:
            self.degradations.append({
                "code": "segment_weights_uniform",
                "phase": self.phase,
                "message": "算不出标准层面积，段面积权重退回均匀 %d 段" % n,
            })
            return uniform, "uniform", "标准层面积不可用"
        rule = self.params.get("segment_rule") if isinstance(self.params, Mapping) else None
        for r, tag in ((rule, "user_rule"), (None, "mssa")):
            try:
                areas, name, note = segment_plan.compute_segment_areas_ex(std, r)
            except (ValueError, TypeError):
                continue
            if areas and len(areas) == n:
                tot = math.fsum(areas)
                if tot > 0:
                    return [float(a) / tot for a in areas], "%s/%s" % (tag, name), note
        self.degradations.append({
            "code": "segment_weights_uniform",
            "phase": self.phase,
            "message": "段数与工艺段数不一致（zones=%d，标准层面积 %.2f m² 推不出同数段）"
                       "→ 段面积权重退回均匀 %d 段" % (n, std, n),
        })
        return uniform, "uniform", "段数不一致"

    # ---------------- 段级几何（所有叶子都要有，不只是占比表驱动的）----------------

    def segment_geometry(self, seg_index: int, zone_index: int) -> Tuple[str, float]:
        """`(段号, 该段面积)` —— 叶子的几何字段（§6 验收 #3）。

        面积 = **本竖向段覆盖到的各层**在该平面段的面积之和
        （一层一段时即该段面积；装饰 3 层一组时是 3 层之和）。
        取不到层面积 → `("", 0.0)`（如实为空，不编数）。
        """
        seg_id = self.zones[zone_index - 1] if 1 <= zone_index <= self.n_zones else ""
        if not self._areas or not (1 <= seg_index <= len(self.segs)):
            return seg_id, 0.0
        start, end = self.segs[seg_index - 1]
        total = 0.0
        for i, area in enumerate(self._areas, 1):
            frac = max(0.0, min(end, float(i) + 1.0) - max(start, float(i)))
            if frac <= 0:
                continue
            total += float(area) * self._weights[zone_index - 1] * frac
        return seg_id, total

    # ---------------- 量0出局 ----------------

    def exclusion_of(self, step: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """该工序是否应「量 0 出局」（不入 WBS）。命中返回留痕 dict，否则 None。

        判据 = `step_ratio_status(...) == "missing"`（唯一实现，与 `beat_configs`
        及 `kb_scope._is_zero` 同源）。
        """
        st = step_ratio_status(self.params, step, self.steps)
        if st["status"] != "missing":
            return None
        info = st.get("info") or {}
        return {
            "activity_id": st["activity_id"],
            "step": _txt(step.get("name")),
            "work_type_id": st["work_type_id"],
            "quantity": 0.0,
            "ratio_percent": info.get("ratio_percent"),
            "reason": st["reason"] + " → 按占 B3 口径量 0 出局，不进树",
        }

    # ---------------- ②③ ----------------

    def step_quantity(self, step: Mapping[str, Any], seg_index: int,
                      zone_index: int) -> Optional[Dict[str, Any]]:
        """B4 公式 ②③：`(工序, 竖向段, 平面段) → 段量`。

        命中占比表 → 返回
        `{qty, formula, source, ratio, l4_total, layer_quantities, segment_areas}`；
        未命中（非活跃工种 / 单位不一致 / 未认领 / 取不到层面积）→ `None`（调用方走既有系数路径）。
        """
        aid = _txt(step.get("kb_activity_id")).strip()
        if not aid:
            return None
        st = step_ratio_status(self.params, step, self.steps)
        if st["status"] != "ratio":
            return None
        info = st.get("info") or {}
        if not self._areas:
            return None
        q_total = float(info.get("quantity") or 0.0)
        if q_total <= QTY_ZERO_TOL:
            return None
        if not (1 <= seg_index <= len(self.segs)) or not (1 <= zone_index <= self.n_zones):
            return None

        # ---- ② 层量：L4 总量 × (该层面积 ÷ Σ各层面积) ----
        layer_map = {i + 1: a for i, a in enumerate(self._areas)}
        layer_qty = segment_capacity.layer_distribution(q_total, layer_map)

        start, end = self.segs[seg_index - 1]
        unit = _txt(step.get("unit")) or _txt(info.get("unit")) or "项"
        total = 0.0
        detail: List[Dict[str, Any]] = []
        for i, area in enumerate(self._areas, 1):
            # 层号 i 的竖向占用区间 = [i, i+1) —— 与 `beat_configs.segment_floors` 的层标签
            # 同口径（首层标签 1.0）。竖向段与层的重叠长度就是「本叶覆盖该层的层数分数」，
            # 地下室 0.5 层一段时每片叶子恰好取到 0.5。
            frac = max(0.0, min(end, float(i) + 1.0) - max(start, float(i)))
            if frac <= 0:
                continue
            # ---- ③ 段量：层量 × (该段面积 ÷ 该层面积) ----
            seg_areas = [float(area) * w for w in self._weights]
            seg_qty = segment_capacity.segment_distribution(layer_qty[i], seg_areas)
            part = float(seg_qty[zone_index]) * frac
            total += part
            detail.append({
                "layer_index": i,
                "floor": (self.floor_keys[i - 1] if i - 1 < len(self.floor_keys) else str(i)),
                "layer_area": float(area),
                "layer_quantity": float(layer_qty[i]),
                "segment_area": float(seg_areas[zone_index - 1]),
                "segment_quantity": float(seg_qty[zone_index]),
                "overlap": frac,
                "quantity": part,
            })

        sum_area = math.fsum(self._areas)
        pct = float(info.get("ratio_percent") or 0.0)
        wt = _txt(info.get("work_type_id"))
        param_key = (self.totals.get(wt) or {}).get("param") or ("total_" + wt)
        param_val = (self.totals.get(wt) or {}).get("value")
        floor_labels = "、".join("%s层" % d["floor"] for d in detail) or "-"
        formula = ("占比表拆分：%s%s×%s%%（Component_Ratio %s|%s，%s）= %s%s（全楼，占 %s|%s 组）"
                   "；÷%s各层面积合计%.2f m² 得各层量"
                   "；%s段 = Σ(层量×段面积÷层面积) = %s %s"
                   % (param_key, unit, _fmt(pct), info.get("structure_type_id"), aid,
                      _review_note(info), _fmt(q_total), unit,
                      info.get("structure_type_id"), wt,
                      floor_labels, sum_area,
                      self.zones[zone_index - 1], _fmt(total), unit))
        return {
            "qty": total,
            "formula": formula,
            "source": SOURCE_COMPONENT_RATIO,
            "ratio": info,
            "l4_total": q_total,
            "total_param": param_key,
            "total_value": param_val,
            "layer_quantities": layer_qty,
            "detail": detail,
            "segment_id": self.zones[zone_index - 1],
            # 段面积 = 本叶子覆盖到的各层在该平面段的面积之和（与 segment_geometry 同口径）
            "segment_area": math.fsum(d["segment_area"] * d["overlap"] for d in detail),
            "floor_area_source": getattr(self, "floor_area_source", ""),
            "segment_rule": self._seg_rule,
        }

    # ---------------- 段级容量字段 ----------------

    def segment_payload(self, step: Mapping[str, Any], b4: Mapping[str, Any],
                        resource_name: str = "") -> Dict[str, Any]:
        """§6 验收 #3 的四个段级容量字段（与 W2-C `_organization` 契约同口径）。"""
        payload = task_capacity_payload(
            segment_id=_txt(b4.get("segment_id")),
            segment_area=float(b4.get("segment_area") or 0.0),
            resource_name=resource_name or _txt(step.get("resource")),
        )
        return payload


def _review_note(info: Mapping[str, Any]) -> str:
    conf = _txt(info.get("confidence")).strip()
    state = _txt(info.get("review_state")).strip()
    bits = []
    if conf:
        bits.append("confidence=%s" % conf)
    if state:
        bits.append("review_state=%s" % state)
    return ",".join(bits) or "confidence=-,review_state=-"


def _fmt(v: Any) -> str:
    f = _num(v)
    if f is None:
        return "-"
    return str(int(round(f))) if abs(f - round(f)) < 1e-9 else str(round(f, 2))


# ======================================================================
# 段级容量（MWI → capacity_fixed / capacity_mobile）
# ======================================================================

_MWI_CACHE: Dict[str, Dict[str, Any]] = {}


def _mwi_index() -> Dict[str, Any]:
    """`Resource_Workface_Index` → `{resource_name: MWIRow}`（只读，进程内缓存）。"""
    if "idx" in _MWI_CACHE:
        return _MWI_CACHE["idx"]
    cols = [r[1] for r in kb._query_all("PRAGMA table_info(Resource_Workface_Index)")]  # noqa: SLF001
    rows: List[Dict[str, Any]] = []
    if cols:
        sel = ", ".join(cols)
        for r in kb._query_all("SELECT %s FROM Resource_Workface_Index" % sel):  # noqa: SLF001
            rows.append(dict(zip(cols, r)))
    idx: Dict[str, Any] = {}
    try:
        idx = segment_capacity.build_mwi_index(rows)
    except Exception:  # noqa: BLE001 — 容量字段是附加信息，取不到不阻断主链路
        idx = {}
    _MWI_CACHE["idx"] = idx
    return idx


def task_capacity_payload(segment_id: str, segment_area: float,
                          resource_name: str = "") -> Dict[str, Any]:
    """按 MWI 算 `capacity_fixed` / `capacity_mobile`，经 `task_capacity_fields` 统一成型。

    字段契约与 W2-C 的 `_organization` 对齐：
      · 永远有 `segment_id` / `segment_area` / `capacity_fixed` / `capacity_mobile`；
      · 取不到 MWI → 计数记 0 且 `capacity_source="unresolved"`（**如实标注，不编数**）；
      · 取到 → `capacity_source="mwi"`，并附 `mwi` / `mwi_unit` / `resource_mobility`。
    """
    fixed = mobile = 0
    source = "unresolved"
    extra: Dict[str, Any] = {}
    row = _mwi_index().get(resource_name) if resource_name else None
    if row is not None and segment_area > 0:
        try:
            plan = segment_capacity.segment_capacity([segment_area], [segment_id], row)
            demand = list(plan.segment_demand or [0])
            n = int(demand[0]) if demand else 0
            if getattr(row, "resource_mobility", "") == segment_capacity.MOBILE:
                mobile = n
            else:
                fixed = n
            source = "mwi"
            extra = {"mwi": getattr(row, "mwi", None),
                     "mwi_unit": getattr(row, "mwi_unit", ""),
                     "resource_mobility": getattr(row, "resource_mobility", "")}
        except (ValueError, TypeError):
            source = "unresolved"
    out = segment_capacity.task_capacity_fields(
        segment_id, segment_area, fixed, mobile, resource_name=resource_name)
    out["capacity_source"] = source
    out.update(extra)
    return out
