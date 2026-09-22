"""施工组织层 · 纯函数（不碰 IO，便于测试）。

**2026-09-21（C 组「资源与工期计算收敛」）后的唯一口径** —— 依据
`docs/资源与工期计算重构方案_v1.md`（12 条裁定 / 7 步链路 / 阶段 0-6）：

    资源只来自**工作面容量**；工期**只有一个公式**：
        工期 = ceil(需求量 ÷ 有效容量)

【0】层面积表 →【1】按 MSSA=500 ㎡ 切施工段 →【2】段容量 = ceil(段面积 ÷ MWI)
→【3】需求量 = 工程量 × 定额 →【4】有效容量 = min(汇总容量, 用户同类限额)
→【5】工期 = ceil(需求量 ÷ 有效容量) →【6】投入资源 = 有效容量

**本模块已删除**（C8 删除清单 3/4，见 `docs/落地清单_Wave23.md` 清单 1）：
  · `_retract_crew()` —— 节拍反算每面人数（C8 删除；节拍只作对比展示，不参与任何计算，裁定 C11）；
  · `plan_workfaces()` —— 节拍驱动的作业面规划（连同 `_duration` / `_levers` /
    `_result` / `_curve_detail` / `effective_crew_max` 一并删除）。

**不复制任何算法**：分段调 `segment_plan.compute_segment_areas_ex`，容量调
`segment_capacity.segment_capacity` / `.duration_days` / `.largest_remainder` /
`.parallel_batches` / `.task_capacity_fields`（C4/C5/C6 的唯一实现）。
本模块只做**串接 + 留痕（"为什么是 N 人 / N 台"）**，不做第二套算术。

数据靠**参数注入**：本模块不读数据库、不读文件，不 import `kb.py` / `scheduler`。
"""

from __future__ import annotations

import math

from . import org_defaults
from . import segment_capacity
from . import segment_plan

__all__ = [
    "MAX_USER_SEGMENTS",
    "ORG_SOURCE_WORKFACE_CAPACITY",
    "AREA_SCOPE_BUILDING",
    "AREA_SCOPE_NON_BUILDING",
    "NON_AREA_SCOPES",
    "face_area_of",
    "build_segment_table",
    "mobility_of",
    "plan_capacity_chain",
    "face_area_for_activity",
]

#: `_organization.source` 的取值（新链路只有这一个来源；字段名交给 W3-B 渲染）。
ORG_SOURCE_WORKFACE_CAPACITY = "workface_capacity"

#: **用户分段规则的软上限（段）** —— 父代理 2026-09-21 裁定，阈值 **200**。
#:
#: 为什么需要它：方案裁定 9 的「段数不设上限」针对的是"**不许为了凑工期去人为限制段数**"，
#: **不是**许可病态输入 —— 用户写「每段不超过 0.0001 m²」会在 `ceil(层面积 ÷ v)` 下切出
#: 10⁷ 段（内存/耗时直接炸掉），而段容量公式 `⌈段面积 ÷ MWI⌉` 在段面积只有几平米时
#: 恒为 1，语义上已无意义。真实项目的 `segment_plan.suggest_zones` 给的是 1~4 段
#: （层面积 788.9→2、1200→3、2000→4、4000→8、5656→11），故 200 是**极宽松的哨兵值**，
#: 只拦病态输入、不误伤正常计划。
#:
#: ⚠️ 超限一律走**「不猜」**路径：退回 MSSA=500 并在 `note` 里写明原因。
#: **绝不静默截断、绝不夹到 200 段** —— 夹了就是"按目标反算"，违反裁定。
#: ⚠️ 本上限**只约束"用户规则"通道**（`int` / `{"segment_count"}` / `{"mssa"}` /
#: 显式段面积列表长度），**不限制** `segment_plan` 自身的 `suggest_zones` / 内部计算。
MAX_USER_SEGMENTS = 200


# ======================================================================
# 域 7.11：**不展开的活动按施工面积开段** —— 面积口径的受控词表
# ======================================================================
# 依据 `docs/域7_资源层_实现设计.md` §7（判据表 A1–A6）+ §14.2 父代理裁决 2：
# **`measure_scope` 是面积的唯一真源**，本模块不发明第二套口径。
#
# ⚠️ 判据（父代理 2026-09-21 冻结，**不许自创**）：
#     第一判据 = `L4_Activity_Dictionary.is_l5_expandable == 0`
#     第二判据 = 树内叶子没有 `segment_id`
# `is_standalone_activity` 实测 493 行**全为 NULL**，**不可用**。
# ⚠️ `is_l5_expandable` 的读取不在本模块（纯函数、不碰 IO）：见
# `pipeline/nodes/resource.py::activity_l5_expandable()`（只读 KB + 进程级缓存）。

#: `measure_scope` = 建筑面积：计量对象就是建筑面积本身。
AREA_SCOPE_BUILDING = "建筑面积"

#: `measure_scope` 里的**其它面积口径词**（受控词表，实测 18 个非空值里的面积族）。
#: 这些口径的"施工面积"取**层面积合计**（`Σ floor_areas`），取不到才退 `total_area`。
AREA_SCOPE_NON_BUILDING = (
    "楼地面面积", "天棚面积", "模板接触面积", "防水面积",
    "保温面积", "外墙面积", "内墙抹灰面积", "风管展开面积",
)

#: **非面积口径**（体积 / 质量 / 自然单位 / 项 / 台数 / 长度 / 根数）：
#: 容量公式仍是 `⌈面积 ÷ MWI⌉`，但必须**注记**"非面积口径"，不许静默按面积算。
NON_AREA_SCOPES = (
    "体积", "质量", "自然单位", "项", "台数",
    "管道长度", "电缆长度", "桩根数",
)

#: 面积证据里"这个面积是从哪来的"的取值（**可溯源**，逐条写进 `basis`）。
_AREA_FROM_TOTAL = "total_area"
_AREA_FROM_FLOORS = "sum_floor_areas"
_AREA_FROM_SEGMENTS = "sum_segment_areas"


def _pos(value):
    """正数或 None（0 / 负数 / 非数 → None）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num) or num <= 0:
        return None
    return num


def _num_text(value):
    """"8" / "7.5"（整数不带小数点，便于交付物显示）；`None` → "—"。"""
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(num)) if num.is_integer() else ("%g" % num)


def face_area_of(work_type_l3):
    """该 L3 的"单面合理面积"（㎡/面）；取不到 → `FACE_AREA_DEFAULT`。

    ⚠️ 新链路**不用**它算容量（容量只来自 MWI 表）；保留只为兼容历史调用点。
    """
    key = str(work_type_l3 or "").strip().lower()
    return float(org_defaults.FACE_AREA_BY_L3.get(key, org_defaults.FACE_AREA_DEFAULT))


def mobility_of(row):
    """从 MWI 行读 `resource_mobility`；缺失/非法 → ``None``（**不猜**，调用方报缺）。

    行可以是 `segment_capacity.MWIRow` 或 dict。**绝不**在代码里写"资源名 → 型别"
    的映射表（方案裁定 4：AI 不补资源；映射表就是写死常量）。
    """
    if row is None:
        return None
    if isinstance(row, dict):
        raw = row.get("resource_mobility")
    else:
        raw = getattr(row, "resource_mobility", None)
    val = str(raw or "").strip().lower()
    return val if val in segment_capacity.MOBILITY_VALUES else None


def _too_many_segments(n, why):
    """用户规则推导出的段数是否超软上限；超了就返回**留痕说明**，否则 `None`。

    ⚠️ 调用方必须在**构造段面积列表之前**调用它 —— 否则 `[fa / n] * n` 会先把
    10⁷ 个元素物化出来，上限就白设了。
    """
    if n is None or int(n) <= MAX_USER_SEGMENTS:
        return None
    return ("%s，会切出 %d 段，超过软上限 %d，**疑似输入有误 → 不猜**，退回 MSSA=%s"
            % (why, int(n), MAX_USER_SEGMENTS, _num_text(segment_plan.MSSA)))


def _translate_mssa_cap(cap, floor_area):
    """把用户的「**每段不超过 v m²**」（`{"mssa": v}`）翻译成**均匀切 n 段**（父代理 2026-09-21 裁定）。

    **为什么必须在这里翻译**：`segment_plan.compute_segment_areas_ex` 的签名里有
    `mssa=` 参数，但它只在 `user_rule` 解析不出段面积时才用（`_normalize_user_rule`
    把 `{"mssa": v}` 当成"能解析"→ 返回段面积列表，那个 `_override` 被**丢弃**），
    所以 `{"mssa": v}` 这条用户规则**在任何链路上都不会生效**（W4-U 的 BLOCKER-5）。
    `segment_plan` 是 P4 的冻结模块（形状不扩），故在本模块翻译掉。

    ⚠️ **不猜**：`v` 不是正数 / 取不到层面积 → 返回 `(None, 原因)`，调用方退回 MSSA=500
    并在 `note` 里留痕。`v >= 层面积` → `ceil(...) = 1` 段（整层一段，合法）。
    段数 `n = ceil(层面积 ÷ v)` 保证**每段实际面积 = 层面积 ÷ n ≤ v**。
    **段数不设上限**（方案裁定 9）—— 本函数不额外加封顶，见报告 BLOCKERS。
    """
    fa = _pos(floor_area)
    limit = _pos(cap)
    if fa is None or limit is None:
        return None, (
            "用户给的「每段面积上限」%s 不可用（需为正数，且必须能取到层面积）"
            "→ **不猜**，退回 MSSA=%s"
            % ("%r" % (cap,) if limit is None else _num_text(cap),
               _num_text(segment_plan.MSSA)))
    n = int(math.ceil(fa / limit))
    if n < 1:
        n = 1
    # ⚠️ 必须**先判上限再构造列表**（`[fa / n] * n` 会立刻物化 n 个元素）
    over = _too_many_segments(n, "按用户给的每段上限 %s m²" % _num_text(limit))
    if over:
        return None, over
    return ({"areas": [fa / n] * n,
             "note": "用户指定每段不超过 %s m²：层面积 %s m² ÷ 上限 → ceil(%s ÷ %s) = %d 段，"
                     "每段 %s m²（≤ 上限）"
                     % (_num_text(limit), _num_text(fa), _num_text(fa), _num_text(limit),
                        n, _num_text(fa / n))},
            "")


def _normalize_user_rule(user_rule, floor_area):
    """把 `boundary_conditions.segment_rule` 归一到
    `segment_plan.compute_segment_areas(user_rule=...)` 认得的形状；认不出 → `None`。

    ⚠️ **契约：单返回值 `规则 | None`**。`pipeline/scope_inputs.py`
    （`segment_rule_supported()` 的闸门）与 `tests/test_w4u_input_channels.py`
    都按 `... is not None` / 直接下标在用它 —— **不要**改成返回元组。
    需要"为什么没认出来"的留痕时用 `_normalize_user_rule_ex()`。
    """
    return _normalize_user_rule_ex(user_rule, floor_area)[0]


def _normalize_user_rule_ex(user_rule, floor_area):
    """同 `_normalize_user_rule`，另返回**留痕说明**（供 `build_segment_table` 并进 `note`）。

    返回 `(规则 | None, 说明)`；说明非空 = 用户规则被翻译了、或**没能生效的准确原因**。

    支持的五种形状（裁定 E + MSSA 覆盖）：

      · **int / float**：段数 `n` → 均匀切成 n 段（面积 = 层面积 ÷ n）；
      · **`{"segment_count": n}`**：同上；
      · **`{"mssa": v}`**（= 用户写「每段不超过 v m²」）：翻译成
        `n = ceil(层面积 ÷ v)` 段均匀切，**每段 ≤ v**（见 `_translate_mssa_cap`）；
      · **`{"segment_areas": [...]}` / `{"areas": [...]}` / `{"segments": [...]}` /
        `[a, b, …]`**：**原样透传**（`segment_plan` 自己解析；显式段面积优先于 `mssa`）；
      · 其它（含 `{"floor_overrides": {...}}`）→ `None` = **不认，退回 MSSA**（**不猜**）。

    ⚠️ **四条"会推导出段数"的路径一律受 `MAX_USER_SEGMENTS = 200` 约束**
    （int / `segment_count` / `mssa` / 显式段面积**列表长度**）：超限 → `None` + 说明，
    走"不猜"退回 MSSA。**绝不静默截断、绝不夹到 200 段**。
    """
    if user_rule is None or isinstance(user_rule, bool):
        return None, ""
    if isinstance(user_rule, (int, float)):
        n = int(user_rule)
        fa = _pos(floor_area)
        if n >= 1 and fa is not None:
            over = _too_many_segments(n, "按用户给的段数 %d" % n)
            if over:
                return None, over
            return {"areas": [fa / n] * n, "note": "用户指定段数 %d（均匀切）" % n}, ""
        return None, ""
    if isinstance(user_rule, dict):
        if "segment_count" in user_rule:
            n = int(_pos(user_rule.get("segment_count")) or 0)
            fa = _pos(floor_area)
            if n >= 1 and fa is not None:
                over = _too_many_segments(n, "按用户给的段数 %d（segment_count）" % n)
                if over:
                    return None, over
                return {"areas": [fa / n] * n,
                        "note": "用户指定段数 %d（segment_count，均匀切）" % n}, ""
            return None, ""
        # 显式段面积优先于「每段上限」：用户给了逐段面积，就不再按上限反推
        for key in ("segment_areas", "areas", "segments"):
            raw = user_rule.get(key)
            if raw is not None:
                cnt = len(raw) if isinstance(raw, (list, tuple)) else 0
                over = _too_many_segments(
                    cnt, "按用户给的段面积列表（%s 项）" % cnt)
                if over:
                    return None, over
                return user_rule, ""
        if "mssa" in user_rule:
            # ⚠️ 用 `in` 而不是 `is not None`：用户写 `{"mssa": null}`（抽取侧把"说了但没给数"
            # 落成 null）也要走翻译通道，好让 `_translate_mssa_cap` 把"为什么没用上"写进 note。
            return _translate_mssa_cap(user_rule.get("mssa"), floor_area)
        return None, ""
    if isinstance(user_rule, (list, tuple)):
        # 裸序列：段数 = 列表长度，同样受软上限约束
        over = _too_many_segments(
            len(user_rule), "按用户给的段面积列表（%d 项）" % len(user_rule))
        if over:
            return None, over
    return user_rule, ""


def build_segment_table(floor_area, user_rule=None, *, mssa=segment_plan.MSSA):
    """【0】【1】层面积 → 施工段表（**调用 `segment_plan`，不复制分段算法**）。

    返回::

        {"floor_area": float|None, "rule": "mssa"|"mssa_uniform"|"mssa_below"|"user"
                                           |"invalid",
         "note": str, "segment_ids": [...], "segment_areas": [...],
         "segments": [{"segment_id", "index", "area"}, ...], "ok": bool,
         "user_rule_given": bool}

    `user_rule` = 用户显式分段规则（裁定 11/E + MSSA 覆盖：**优先于一切**，
    `boundary_conditions.segment_rule`）；形状归一化见 `_normalize_user_rule`。
    层面积不可用（缺 / <=0）→ `ok=False`、`segment_areas=[]`，调用方**必须报缺**
    而不是编一个面积。

    `note` 会把"用户规则怎么被解释的 / 为什么没能生效"一并留痕
    （例如 `{"mssa": 400}` → 「…ceil(1000 ÷ 400) = 3 段，每段 333.33 m²（≤ 上限）」；
    非法值 → 「…不可用 → **不猜**，退回 MSSA=500」）。
    """
    norm, rule_note = _normalize_user_rule_ex(user_rule, floor_area)
    areas, rule, note = segment_plan.compute_segment_areas_ex(
        floor_area, norm, mssa=mssa)
    if rule_note:
        note = ("%s；%s" % (note, rule_note)) if note else rule_note
    ids = [_segment_id(i) for i in range(len(areas))]
    segs = [{"segment_id": ids[i], "index": i, "area": float(areas[i])}
            for i in range(len(areas))]
    fa = _pos(floor_area)
    return {
        "floor_area": float(fa) if fa is not None else None,
        "rule": str(rule),
        "note": str(note or ""),
        "segment_ids": ids,
        "segment_areas": [float(a) for a in areas],
        "segments": segs,
        "ok": bool(areas),
        "user_rule_given": norm is not None,
    }


def _segment_id(i):
    """0 基序号 → 段号（与 `segment_plan.SEGMENT_IDS` 同形；超出台账按数字兜底）。"""
    if 0 <= i < len(segment_plan.SEGMENT_IDS):
        return segment_plan.SEGMENT_IDS[i]
    return "%d" % (i + 1)


# ======================================================================
# 域 7.11：不展开的活动按施工面积开段（**纯函数、可溯源、确定性**）
# ======================================================================


def _measure_scope_text(value):
    """`measure_scope` 归一：`None` / 空 / 纯空白 → `""`（**当"未填"，不猜**）。"""
    return "" if value is None else str(value).strip()


def _row_field(row, *names):
    """从 `segment_capacity.MWIRow` 或同形 dict 里按优先级读字段；取不到 → `None`。"""
    for name in names:
        if row is None:
            return None
        if isinstance(row, dict):
            val = row.get(name)
        else:
            val = getattr(row, name, None)
        if val is not None:
            return val
    return None


def _floor_sum(floor_areas):
    """层面积合计（`Σ floor_areas`）；**非正 / 取不到 → `None`**（不猜、不编面积）。"""
    if not floor_areas:
        return None
    total = 0.0
    seen = 0
    for area in floor_areas:
        num = _pos(area)
        if num is None:
            continue
        total += num
        seen += 1
    if seen == 0 or total <= 0:
        return None
    return total


def _area_evidence(params, floor_area=None, segment_ids=None, segment_areas=None):
    """可溯源面积证据：按 **`measure_scope` 是唯一真源**（§14.2 裁决 2）取数。

    顺序（**与 §7 判据表 A2–A6 一一对应**）：

      1. `Σ segment_areas` —— 有段号/段面积的**可分层实体工程**（A1）的层面积；
      2. `Σ floor_areas`   —— `measure_scope` 是**面积口径族**、或**为空**时的回退；
      3. `total_area`      —— 层面积取不到时的最后回退（**回退必须留痕**）。

    返回 `{area, source, has_floor_areas, total_area, scope, caliber_notes}`；
    有一条都取不到 → `area is None` 且 `source == ""`（调用方**必须报缺**，A6）。
    """
    params = params if isinstance(params, dict) else {}
    scope = _measure_scope_text(params.get("measure_scope"))
    total = _pos(params.get("total_area"))
    raw_floors = list(params.get("floor_areas") or [])
    floors = _floor_sum(raw_floors)
    seg_areas = _floor_sum(segment_areas)
    notes = []

    if floor_area is None and seg_areas is not None and segment_ids:
        # A1：可分层实体工程 —— 层面积就是"段面积的父集合"。
        # ⚠️ 只有真给了 `segment_ids` 才算，否则 `segment_areas` 可能只是调用方
        # 随手传的列表，把 `Σ` 当层面积会凭空造出一个面积。
        got, source = seg_areas, _AREA_FROM_SEGMENTS
    elif scope == AREA_SCOPE_BUILDING:
        if floors is not None:
            got, source = floors, _AREA_FROM_FLOORS
            notes.append("measure_scope=建筑面积，但给了逐层面积 → 按**层面积合计**取数")
        else:
            got, source = total, _AREA_FROM_TOTAL
            notes.append("measure_scope=建筑面积 → 取 total_area")
            if total is not None:
                notes.append(
                    "⚠️ total_area 与'不展开活动按建筑面积算的施工面积'**数值相同、"
                    "无法区分两种口径**（父代理 2026-09-21 裁决 2 的诚实留痕要求）")
    elif scope in AREA_SCOPE_NON_BUILDING:
        if floors is not None:
            got, source = floors, _AREA_FROM_FLOORS
            notes.append("measure_scope=%s → 按**层面积合计**取数" % scope)
        else:
            got, source = total, _AREA_FROM_TOTAL
            notes.append("measure_scope=%s 但层面积取不到 → **回退 total_area**" % scope)
    elif scope == "":
        # ★ 硬要求 ①：`measure_scope` 为空（实测 98 行）**必须**回退层面积合计，
        #   并在 basis 里写明"回退"，**不许静默**。
        if floors is not None:
            got, source = floors, _AREA_FROM_FLOORS
            notes.append("measure_scope 为空 → **回退层面积合计**（Σ floor_areas）")
        else:
            got, source = total, _AREA_FROM_TOTAL
            notes.append("measure_scope 为空且层面积取不到 → **回退 total_area**")
    elif scope in NON_AREA_SCOPES:
        if floors is not None:
            got, source = floors, _AREA_FROM_FLOORS
        else:
            got, source = total, _AREA_FROM_TOTAL
        notes.append("非面积口径（measure_scope=%s）：容量仍按 MWI 公式 `⌈面积 ÷ MWI⌉`，"
                     "面积取层面积合计（缺则 total_area）" % scope)
    else:
        # 词表外的 `measure_scope`（含单位形态如 `m²` —— 那是 unit，不是计量对象）：
        # **不猜口径**，与"为空"同一条回退链，并如实记下原值。
        if floors is not None:
            got, source = floors, _AREA_FROM_FLOORS
            notes.append("measure_scope=%r 不在受控词表内 → 按层面积合计取数（**不猜口径**）"
                         % scope)
        else:
            got, source = total, _AREA_FROM_TOTAL
            notes.append("measure_scope=%r 不在受控词表内且层面积取不到 → 回退 total_area"
                         % scope)

    return {"area": got, "source": (source if got is not None else ""),
            "has_floor_areas": floors is not None, "total_area": total,
            "scope": scope, "caliber_notes": notes}


def face_area_for_activity(params, *, is_l5_expandable=None, leaf_segment_id=None,
                           scope=None, mwi_row=None, floor_area=None,
                           segment_ids=None, segment_areas=None, trade="",
                           activity_id="", activity_name=""):
    """域 7.11：**不展开的活动按施工面积开段** —— 算出"按哪个面积算容量、切几段"。

    判据（**父代理 2026-09-21 冻结，不许自创**；两条**同时**成立才是"不展开"）
    ----------------------------------------------------------------------
      第一判据 = `L4_Activity_Dictionary.is_l5_expandable == 0`（KB 决定；`None` = 未知 → 不判）；
      第二判据 = 树内叶子没有 `segment_id`（`leaf_segment_id` 为空）。
      ⚠️ `is_standalone_activity` 实测 493 行**全为 NULL**，**本函数不使用它**。

    面积口径（§14.2 裁决 2：**`measure_scope` 是唯一真源**）
    ------------------------------------------------------
      · 不展开（A2–A5）→ **不需要楼层范围**，施工面积一次取定、**段数恒 = 1**；
      · 展开（A1）→ 仍是"按层展开"，`段数 = ceil(层面积 ÷ MSSA)`（复用
        `build_segment_table`，**不复制分段算法**）。
      · `measure_scope` 为空（实测 98 行）→ **回退层面积合计**，`basis` 里写明"回退"；
      · 面积一条都取不到（A6）→ `ok=False` + `basis` 写明"报缺"，**绝不编面积**。

    容量公式（**唯一**）：`容量 = ⌈施工面积 ÷ 对应资源 MWI⌉`（样例：`GD_A11_平整场地`
    `measure_scope='建筑面积'`、`total_area=14200 m²`、履带式推土机 `mwi=667 m2/台`
    → `⌈14200 ÷ 667⌉ = 22 台`）。**本函数只给面积证据 + 台数，不重写取小/工期**：
    段容量与工期一律交给 `plan_capacity_chain`（C10 唯一公式）。

    返回
    ----
    `{"ok", "face_area", "segment_count", "segment_ids", "segment_areas",
      "area_source", "caliber", "caliber_note", "basis", "mwi", "mwi_unit",
      "capacity_units", "expandable"}`
    —— `basis` 是**逐条可溯源**的中文留痕（用了哪个面积 / 依据哪条 `measure_scope` /
    回退没回退 / 无法区分口径时如实说明）。同输入**逐位同输出**（无随机、无时间）。
    """
    params = params if isinstance(params, dict) else {}
    sc = _measure_scope_text(scope) if scope is not None \
        else _measure_scope_text(params.get("measure_scope"))
    if sc != _measure_scope_text(params.get("measure_scope")):
        params = dict(params)
        params["measure_scope"] = sc

    expandable = None if is_l5_expandable is None else bool(is_l5_expandable)
    has_seg = bool(str(leaf_segment_id or "").strip())
    # 父代理冻结的判据：第一判据（is_l5_expandable==0）∧ 第二判据（叶子没有 segment_id）
    standalone = (expandable is False) and (not has_seg)

    mwi = _pos(_row_field(mwi_row, "mwi"))
    mwi_unit = _row_field(mwi_row, "mwi_unit") or org_defaults.MWI_UNIT_AREA
    ident = "活动「%s」" % (activity_name or activity_id or "?")
    if trade:
        ident += "（工种 %s）" % trade

    if expandable is None:
        head = ("%s：`is_l5_expandable` **未知**（KB 未给出）→ **不判「不展开」**，"
                "沿用可分层实体工程口径" % ident)
    elif standalone:
        head = ("%s：`is_l5_expandable=0`（第一判据）+ 树内叶子无 `segment_id`（第二判据）"
                "→ **不展开的一次性活动**：按施工面积开段，**段数 = 1**，不需要楼层范围" % ident)
    elif expandable is False:
        head = ("%s：`is_l5_expandable=0` 但树内叶子**有** `segment_id=%s`（第二判据不成立）"
                "→ 走可分层实体工程口径（按层展开）" % (ident, leaf_segment_id))
    else:
        head = ("%s：`is_l5_expandable=%s`（**可展开**）→ 走可分层实体工程口径（按层展开）"
                % (ident, is_l5_expandable))

    if not standalone:
        # ---------- A1：可分层实体工程（按层展开，复用 build_segment_table） ----------
        fa = _pos(floor_area)
        ev = _area_evidence(params, floor_area=fa, segment_ids=segment_ids,
                            segment_areas=segment_areas)
        if fa is None:
            fa = ev["area"]
        basis = [head]
        basis.extend(ev["caliber_notes"])
        if fa is None:
            basis.append("层面积取不到（`total_area` / `floor_areas` / 段面积全缺）"
                         "→ **报缺**（A6）：不编面积、不编台数，调用方必须报 `reported_missing`")
            return {
                "ok": False, "face_area": None, "segment_count": 0,
                "segment_ids": [], "segment_areas": [],
                "area_source": "", "caliber": "floor_area",
                "caliber_note": "层面积取不到 → 报缺（不猜）",
                "basis": "；".join(basis), "mwi": mwi, "mwi_unit": mwi_unit,
                "capacity_units": None, "expandable": expandable,
            }
        table = build_segment_table(fa)
        areas = [float(a) for a in table["segment_areas"]]
        ids = [str(s) for s in table["segment_ids"]]
        cal = "floor_area"
        note = ("可分层实体工程：层面积 %s m² → 按 MSSA=%s 切 %d 段 → 段容量 = "
                "⌈段面积 ÷ MWI⌉；段容量与工期交给 `plan_capacity_chain`（C10 唯一公式）"
                % (_num_text(fa), _num_text(segment_plan.MSSA), len(ids)))
        basis.append("层面积 %s m²（来源 %s）→ 按 MSSA=%s 切 %d 段 %s"
                     % (_num_text(fa), table["rule"], _num_text(segment_plan.MSSA),
                        len(ids), "、".join("%s=%s m²" % (i, _num_text(a))
                                           for i, a in zip(ids, areas))))
        basis.append(note)
        return {
            "ok": bool(areas), "face_area": float(fa), "segment_count": len(ids),
            "segment_ids": ids, "segment_areas": areas,
            "area_source": ev["source"] or "floor_area", "caliber": cal,
            "caliber_note": note, "basis": "；".join(basis),
            "mwi": mwi, "mwi_unit": mwi_unit, "capacity_units": None,
            "expandable": expandable,
        }

    # ---------- A2–A6：不展开的一次性活动（段数 = 1，不需要楼层范围） ----------
    ev = _area_evidence(params, floor_area=None, segment_ids=None, segment_areas=None)
    area = ev["area"]
    basis = [head]
    if sc:
        basis.append("面积口径依据（唯一真源）：`measure_scope='%s'`" % sc)
    basis.extend(ev["caliber_notes"])

    if area is None:
        basis.append("施工面积一条都取不到（`measure_scope='%s'`，层面积 / total_area 全缺）"
                     "→ **报缺**（A6）：不编面积、不编台数，调用方必须报 `reported_missing`"
                     % sc)
        return {
            "ok": False, "face_area": None, "segment_count": 1,
            "segment_ids": [], "segment_areas": [],
            "area_source": "", "caliber": "construction_area",
            "caliber_note": "施工面积取不到 → 报缺（不猜）",
            "basis": "；".join(basis), "mwi": mwi, "mwi_unit": mwi_unit,
            "capacity_units": None, "expandable": expandable,
        }

    units = None
    if mwi is not None:
        units = int(math.ceil(float(area) / float(mwi)))
    basis.append("施工面积 = %s m²（来源：%s）" % (_num_text(area), ev["source"]))
    if units is None:
        basis.append("MWI 取不到 → **不编台数**（报缺，`capacity_source='reported_missing'`）")
    else:
        basis.append("容量 = ⌈施工面积 %s m² ÷ MWI %s %s⌉ = %d（按 `plan_capacity_chain` "
                     "的唯一公式，段数 = 1）"
                     % (_num_text(area), _num_text(mwi), mwi_unit, units))
    basis.append("不展开的一次性活动**不需要楼层范围**：段数 = 1、施工面积一次取定"
                 "（区别于可分层实体工程的「按层展开、段容量 = ⌈段面积 ÷ MWI⌉」）")
    note = ("不展开（`is_l5_expandable=0` ∧ 叶子无 `segment_id`）→ 施工面积 %s m²"
            "（口径 `measure_scope='%s'`，来源 %s）、段数 = 1"
            % (_num_text(area), sc, ev["source"]))
    return {
        "ok": True, "face_area": float(area), "segment_count": 1,
        "segment_ids": ["Ⅰ"], "segment_areas": [float(area)],
        "area_source": ev["source"], "caliber": "construction_area",
        "caliber_note": note, "basis": "；".join(basis),
        "mwi": mwi, "mwi_unit": mwi_unit, "capacity_units": units,
        "expandable": expandable,
    }


# ======================================================================
# 新链路【2】–【6】：段容量 → 汇总 → 取小 → 工期 → 投入资源
# ======================================================================


def plan_capacity_chain(demand, segment_areas, segment_ids, mwi_row, *,
                        user_cap=None, user_cap_source="", aliases=None,
                        resource_name="", capacity_source="mwi",
                        cadence_days=None, cadence_scope=None, cadence_source=None,
                        extra_warnings=None, daily_share=None):
    """新链路的**唯一实现**：一步算出段容量、有效容量、工期与投入资源。

    参数
    ----
    demand        : 需求量（工日 / 台班）＝ 工程量 × 定额消耗（【3】）。
    segment_areas : 段面积（【1】，`build_segment_table` 的 `segment_areas`）。
    segment_ids   : 段号（与 `segment_areas` 等长）。
    mwi_row       : `segment_capacity.MWIRow` 或同形 dict
                    （`resource_name` / `mwi` / `mwi_unit` / `resource_kind` /
                    `resource_mobility` / `capacity_mode`）。
    user_cap      : 用户同类限额（【4】；**只有 `_source == "user"` 的才该传进来**）。
                    `None` = 用户没给 → **不限**。
    capacity_source : 容量数据的来源标记（缺省 `"mwi"`；调用方走兜底时写字样）。
    daily_share   : **域 7（7.2）新增** —— 该任务窗口内的**逐日份额最小值**
                    （`_daily_share`：`{资源名: {task_id: {day: 份额}}}` 里本任务那次
                    回压给出的最小份额，**正整数**）。语义与纪律：

                      · `None`（缺省）⇒ **没有逐日份额**，行为**逐字段**等同改造前
                        （`effective_capacity_daily(N, None) == N`，老路径的向后兼容保证点）；
                      · 非 `None` ⇒ `有效容量 = min(既有有效容量, daily_share)`，
                        实现上**只调** `segment_capacity.effective_capacity_daily()`
                        （**不在这里重写第二遍取小逻辑**）；
                      · `daily_share <= 0` ⇒ `ValueError`（由 `effective_capacity_daily`
                        抛出 —— 份额为 0 意味着上游回压算错了，**绝不静默算成无穷工期**）；
                      · 口径（父代理 2026-09-21 裁决 7）：总表 1 的
                        `工期 = ⌈需求量 ÷ 有效容量⌉` 是**唯一公式**；逐日累加会造出
                        第二个公式，故这里**只取窗口内的最小值**当标量用，
                        **不做逐日累加**（设计 §13.1 R2：份额只作用在 `_plan_task` 的
                        `eff` 上，**不写进** `serial_sgs` 的 `pool` 闸门）。

    返回
    ----
    `_organization` 字典（键只增不改；交付层字段见 `docs/落地清单_Wave23.md`）。
    域 7 新增键：**`effective_source`** —— `"legacy"`（`daily_share is None`）
    或 `"daily_share"`（份额参与过取小）。老键名与老类型一个都没动。

    语义（方案 §3.1 / §4.2 / §4.3 / §4.4 / §4.5）：
      · fixed  ：段级 `n_i = ceil(段面积 ÷ MWI)`，**逐段取整后相加** `N = Σ n_i`；
      · mobile ：**汇总后取整一次** `N = ceil(Σ面积 ÷ MWI)`，并按最大余数法回分；
      · site   ：**独立**，不进段容量（由调用方接 `_site_equipment`）。
      · `有效容量 = min(N, 用户同类限额, 逐日份额)`；后两者没给 → 该项**不限**。
      · `工期 = duration_days(需求量, 有效容量)` —— **C10 唯一公式**。
      · 节拍（`cadence_days`）**只写进产物作对比展示，不参与任何计算**（裁定 8 / C11）。
    """
    pd = _pos(demand)
    if pd is None:
        pd = 0.0
    areas = [float(a) for a in (segment_areas or [])]
    ids = [str(s) for s in (segment_ids or [])]
    warnings = list(extra_warnings or [])

    plan = segment_capacity.segment_capacity(areas, ids, mwi_row, user_cap=user_cap)
    warnings.extend(plan.warnings)
    # 域 7（7.1）：逐日份额取小。`daily_share is None` → 恒等返回旧有效容量，
    # 故"没有份额"的路径**逐字段**退回旧行为（每日份额 ≤0 → ValueError，不静默）。
    eff = segment_capacity.effective_capacity_daily(plan.effective, daily_share)
    eff_source = "daily_share" if daily_share is not None else "legacy"
    dur = segment_capacity.duration_days(pd, eff)

    # ---- 回分（**仅移动型**；§4.3 最大余数法，总和恒等于有效容量）----
    alloc = None
    if plan.mobility == segment_capacity.MOBILE and eff:
        alloc = segment_capacity.largest_remainder(eff, areas, ids)
        warnings.extend(alloc.warnings)

    # ---- 并行批次（§4.4 边界 2：段数 > 有效容量 → 多余段错开批次）----
    batch_list, batch_count = [], 0
    if plan.segment_demand and eff:
        batch_list, batch_count, bnotes = segment_capacity.parallel_batches(
            ids, eff, segment_index={sid: i for i, sid in enumerate(ids)})
        warnings.extend(bnotes)

    # ---- 逐段容量字段（W3-B 直接渲染；用 `task_capacity_fields` 的唯一实现）----
    alloc_map = dict(zip(ids, alloc.allocated)) if alloc else {}
    segments = []
    for seg in plan.segments:
        row = segment_capacity.task_capacity_fields(
            seg.segment_id, seg.segment_area, seg.capacity_fixed,
            seg.capacity_mobile, allocated=alloc_map.get(seg.segment_id),
            resource_name=resource_name or plan.resource_name)
        row["mwi"] = seg.mwi
        row["mwi_unit"] = seg.mwi_unit
        row["resource_kind"] = seg.resource_kind
        row["resource_mobility"] = seg.resource_mobility
        row["capacity_mode"] = seg.capacity_mode
        row["segment_demand"] = (
            plan.segment_demand[len(segments)]
            if len(segments) < len(plan.segment_demand) else None)
        row["batch"] = (batch_list[len(segments)]
                        if len(segments) < len(batch_list) else None)
        segments.append(row)

    basis_lines = _basis_lines(
        demand=pd, mobility=plan.mobility, plan=plan, eff=eff, dur=dur,
        user_cap=user_cap, user_cap_source=user_cap_source, alloc=alloc,
        resource_name=resource_name or plan.resource_name,
        capacity_source=capacity_source, daily_share=daily_share)

    return {
        # —— 来源与身份 ——
        "source": ORG_SOURCE_WORKFACE_CAPACITY,
        "resource_name": resource_name or plan.resource_name,
        "resource_kind": plan.segments[0].resource_kind if plan.segments else "",
        "resource_mobility": plan.mobility,
        "capacity_source": str(capacity_source),
        # —— 【3】需求量 ——
        "demand": float(pd),
        # 兼容旧键：交付物/资源层读 `_organization.person_days`（本工序工种工日）
        "person_days": float(pd),
        # —— 【4】容量 ——
        "capacity_rollup": int(plan.rollup or 0),
        "capacity_effective": None if eff is None else int(eff),
        # 域 7（7.1）**新增键**：有效容量的来源 —— "legacy"（无逐日份额）
        # 或 "daily_share"（份额参与过取小）。键只增不改。
        "effective_source": eff_source,
        "rollup_kind": plan.rollup_kind,
        # 兼容旧键：`crew_total` = 投入资源数（= 有效容量，【6】）
        "crew_total": None if eff is None else int(eff),
        "effective_crew_total": None if eff is None else int(eff),
        # —— 【5】工期（C10 唯一公式的产物）——
        "duration_days": None if dur is None else int(dur),
        # —— 施工段表（W3-B 渲染）——
        "segment_count": len(ids),
        "n_faces": len(ids),                     # 兼容旧键：作业面数 = 施工段数
        "segment_ids": ids,
        "segment_areas": areas,
        "segments": segments,
        "segment_plan": plan.as_dict(),
        # —— 【4】用户限额（只有 `_source=user` 才有值）——
        "user_cap": None if user_cap is None else int(user_cap),
        "user_cap_source": str(user_cap_source or ""),
        "discarded_caps": list(plan.discarded_caps or []),
        # —— 回分与批次 ——
        "allocation": alloc.as_dict() if alloc else None,
        "batches": dict(zip(ids, batch_list)),
        "batch_count": int(batch_count),
        # —— 兼容旧键：新链路不折减、不排班 ——
        "shifts": 1,
        "eta": 1.0,
        # —— C11：节拍**只作对比展示** ——
        "cadence_days": float(cadence_days) if _pos(cadence_days) else None,
        "cadence_scope": cadence_scope,
        "cadence_source": cadence_source,
        # —— 交付物口径键（键名与旧 `_organization` 保持一致，只增不改）——
        "feasible": True,
        "t_min_days": None if dur is None else int(dur),
        "levers": [],
        "planned_person_days": float(pd),
        "attendance_person_days": (None if (eff is None or dur is None)
                                   else round(float(eff) * float(dur), 2)),
        "basis": "；".join(basis_lines),
        "basis_lines": basis_lines,
        "warnings": warnings,
    }


def _basis_lines(*, demand, mobility, plan, eff, dur, user_cap, user_cap_source,
                 alloc, resource_name, capacity_source, daily_share=None):
    """人可读、可溯源的依据：**为什么是 N 人 / N 台、为什么是 D 天**。

    域 7（7.1）新增：`daily_share` 非 `None` 时，把**逐日份额取小**这一步写进依据
    （`min(既有有效容量, 逐日份额最小值)`），**不许静默** —— 否则交付物上
    "为什么人比 MWI 算出来的少"就成了无据可查。
    """
    unit = "台" if (plan.segments
                    and str(plan.segments[0].resource_kind).lower() == "machine") else "人"
    kind_text = {"fixed": "固定型（驻段）→ 逐段取整后相加",
                 "mobile": "移动型（巡段）→ 汇总后取整一次",
                 "site": "场地级 → 独立（不进段容量）"}.get(str(mobility), str(mobility))
    lines = ["资源「%s」：%s" % (resource_name or "?", kind_text)]

    if mobility == segment_capacity.SITE:
        # 场地级（塔吊 / 施工电梯）：**不进段容量**，由 `_site_equipment` 独立给出台数。
        lines.append("场地级资源不进施工段容量：段容量与工期由 `_site_equipment` 独立给出，"
                     "本表不参与（方案 §4.4 边界 8 / §7）")
        return lines

    if capacity_source != "mwi":
        lines.append("容量数据来源：%s（MWI 表缺该资源，已按兜底口径计并留痕）"
                     % capacity_source)

    for i, seg in enumerate(plan.segments or []):
        cap = (seg.capacity_fixed if mobility == segment_capacity.FIXED
               else seg.capacity_mobile)
        lines.append(
            "段 %s：面积 %s m² ÷ MWI %s %s → %s %s"
            % (seg.segment_id, _num_text(seg.segment_area), _num_text(seg.mwi),
               seg.mwi_unit or org_defaults.MWI_UNIT_AREA, _num_text(cap), unit))

    lines.append("汇总容量 N = %s %s（%s）" % (_num_text(plan.rollup), unit,
                                             plan.rollup_kind))
    if user_cap is None:
        lines.append("用户未给同类限额 → 在该档**不限**")
    else:
        lines.append("用户同类限额 = %s %s（%s）" % (_num_text(user_cap), unit,
                                                user_cap_source or "user"))
        lines.append("取小过程：min(%s, %s) = %s %s"
                     % (_num_text(plan.rollup), _num_text(user_cap),
                        _num_text(plan.effective), unit))
    if daily_share is None:
        if user_cap is None:
            lines.append("用户未给同类限额 → **不限**，有效容量 = N = %s %s"
                         % (_num_text(eff), unit))
        else:
            lines.append("有效容量 = min(N, 限额) = %s %s" % (_num_text(eff), unit))
    else:
        # 域 7（7.1）：逐日份额取小 —— 必须留痕（"为什么比 MWI 算出来的还少"）。
        if user_cap is None:
            cap_stage = ("限额档有效容量 = %s %s（用户未给同类限额 → 该档不限）"
                         % (_num_text(plan.effective), unit))
        else:
            cap_stage = ("限额档有效容量 = min(N, 限额) = %s %s"
                         % (_num_text(plan.effective), unit))
        lines.append(cap_stage)
        lines.append("逐日份额（该任务窗口内的最小值）= %s %s → 有效容量 = "
                     "min(限额档有效容量, 份额) = %s %s（域 7.1；口径总表 1 唯一公式，"
                     "逐日累加**不**另立公式）"
                     % (_num_text(daily_share), unit, _num_text(eff), unit))
        lines.append("逐日份额取小过程：min(%s, %s) = %s %s"
                     % (_num_text(plan.effective), _num_text(daily_share),
                        _num_text(eff), unit))
    if alloc is not None:
        lines.append("回分（最大余数法，仅移动型）：%s；总和 = %s %s"
                     % ("、".join("%s→%s" % (sid, n)
                                  for sid, n in zip(alloc.segment_ids,
                                                    alloc.allocated)),
                        _num_text(sum(alloc.allocated)), unit))
    lines.append("工期 = ceil(需求量 %s ÷ 有效容量 %s %s) = %s 天"
                 % (_num_text(demand), _num_text(eff), unit, _num_text(dur)))
    return lines
