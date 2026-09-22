"""容量字典生成器（阶段 3｜C 组 C4–C7）—— 依据《资源与工期计算重构方案 v1》。

实施依据（唯一权威）：
  · `docs/资源与工期计算重构方案_v1.md` §1 术语表、§2 裁定 4/6/7/9/10、
    §3【2】【3】【5】【6】、§3.1 三类口径、§3.2 固定/移动判据、§4.2 容量规则、
    §4.3 回分规则 + 验算、§4.4 边界清单 9 条、§4.5 用户限额口径、§6 验收 #2/#3
  · `docs/修改项总清单_20260921.md` B4 / C4 / C5 / C6 / C7 / C9 / C10

本模块是**纯逻辑**：不读数据库、不读文件、不发网络请求、不 import `kb.py`、
**不 import `scheduler`**（避免循环依赖）。一切数据靠**参数注入**。

核心口径（勿改）：
  · 段级需求（主口径，永远有值）：`n_i = ceil(A_seg_i ÷ MWI)`
  · 固定型 fixed ：各段相加 `N = Σ n_i`
  · 移动型 mobile：汇总 `N = ceil(Σ同时段面积 ÷ MWI)`
  · 场地级 site  ：**不进段容量**，只排除并打标记（由调用方接 `_site_equipment`）
  · 多资源 L4    ：主控 = `Demand ÷ 容量` 最大者；伴生 = 按 Role Map 配比派生（裁定 10）
  · 回分（仅移动型）：最大余数法，总和恒等于 N（§4.3 ①–⑤）
  · 唯一工期公式 ：`工期 = ceil(Demand ÷ 有效容量)`，`有效容量 = min(N, 用户同类限额)`
  · 用户限额     ：**只有 `_source == "user"` 的才进 min()**，AI/model 补的一律丢弃（§4.5）

域 7（7.1 / 7.3 / 7.4 / 7.5 / 7.6）**并列新增**（老符号一字未改）：
  · 7.1 有效容量逐日化：`effective_capacity_daily(segment_cap, day_share)`
        = `min(段容量, 当天分到的份额)`；`day_share is None` → 不限，返回 `segment_cap`。
        「当天分到的份额」= 域 7 新造的一级数据结构 `_daily_share`
        （形状见 `docs/域7_资源层_实现设计.md` §4.4；`{资源: {task_id: {day: 份额}}}`）。
  · 7.3 按**需求量**分摊：`largest_remainder_by_demand(limit, demands, entity_ids=None)`
        `alloc_i = limit × demand_i ÷ Σdemand`。
        ⚠️ **明确不按施工量分摊**（施工量在 m²/m³/t 之间不可比）。
  · 7.4 取整用最大余数法，保证 `Σallocated == limit`。
  · 7.5 强制最少一人：某条被取整成 0 → 抬到 1；这 1 人**从「小数余数最小」的那条减 1**。
  · 7.6 限额 < 条数 ⇒ **突破限额**（每人至少 1），`Σ > limit`、`breached=True`、显式 warning。
        **与老 `largest_remainder` 的该分支口径正好相反**（老函数保留 Σ=N 只发 warning）。

单位：面积一律 `m²`（**严禁写 U+33A1 的方块平米符号**）；数值一律 float，人数/台数向上取整。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "FIXED",
    "MOBILE",
    "SITE",
    "MOBILITY_VALUES",
    "TRADE_ALIAS",
    "MWIRow",
    "RoleAssignment",
    "UserCap",
    "ActivityDemand",
    "SegmentCapacity",
    "CapacityAllocation",
    "SegmentPlan",
    "ceil_div",
    "floor_div",
    "build_mwi_index",
    "normalize_trade",
    "segment_capacity",
    "segment_rollup",
    "largest_remainder",
    "largest_remainder_by_demand",
    "allocation_by_entity",
    "parallel_batches",
    "resolve_user_cap",
    "effective_capacity",
    "effective_capacity_daily",
    "duration_days",
    "allocate_segment_demand",
    "primary_companion",
    "task_capacity_fields",
    "layer_distribution",
    "segment_distribution",
]

#: 机动性取值（方案 §1 术语表 / §3.2）
FIXED = "fixed"
MOBILE = "mobile"
SITE = "site"

MOBILITY_VALUES = (FIXED, MOBILE, SITE)

#: 工种别名归一表 —— **副本**。
#:
#: 权威副本是 `backend/pipeline/nodes/scheduler.py:2014 _TRADE_ALIAS`。本模块**故意**
#: 不 import scheduler（`scheduler` 体量巨大且反向依赖 pipeline，import 会引入循环
#: 风险），所以在此保留一份**逐字相同**的副本，并允许调用方通过 `aliases=` 参数注入
#: 权威表覆盖它（父代理接 `scheduler._normalize_trade` 时的注入点）。
#: 若 scheduler 的 `_TRADE_ALIAS` 变更，**必须同步这里**（已写入报告 BLOCKERS）。
TRADE_ALIAS: Dict[str, str] = {
    "木工": "模板工", "模板": "模板工", "支模工": "模板工", "木模板工": "模板工",
    "砼工": "混凝土工", "混凝土": "混凝土工", "搅拌工": "混凝土工",
    "扎筋工": "钢筋工", "钢筋": "钢筋工",
    "砌筑工": "瓦工", "砖工": "瓦工", "泥水工": "瓦工",
    "杂工": "普工", "力工": "普工", "普通工": "普工",
    "脚手架工": "架子工", "架工": "架子工",
    "粉刷工": "抹灰工", "油漆": "油漆工", "涂料工": "油漆工",
    "水暖工": "管道工", "管工": "管道工", "水电工": "电工",
    "焊工": "安装工", "起重工": "司机", "机械操作工": "操作工",
}


# ======================================================================
# 注入数据契约（供父代理接 `Resource_Workface_Index` /
# `Resource_Role_Map` / `layer_engine` 用；本模块不自己读库）
# ======================================================================


@dataclass(frozen=True)
class MWIRow:
    """`Resource_Workface_Index` 的一行（67 行表的**最小投影**）。

    `resource_mobility` 可逐行修改（方案 §3.2 注），所以**不得**写死工种/机械名单。
    `capacity_mode` 是五类口径（area 27 / position 21 / auxiliary 11 / transport 5 /
    site 3），本模块**只用作透传留痕**，不参与容量计算（方案未规定五类怎么改变计算）。
    """

    resource_name: str
    mwi: float
    mwi_unit: str = "m²/人"
    resource_kind: str = "labor"            # labor / machine
    resource_mobility: str = FIXED          # fixed / mobile / site
    capacity_mode: str = "area"             # area/position/auxiliary/transport/site
    notes: str = ""


@dataclass(frozen=True)
class RoleAssignment:
    """`Resource_Role_Map` 的一行：某 L4（activity_id）下某资源是主控还是伴生。"""

    activity_id: str
    resource_name: str
    role: str = "primary"                   # primary / companion
    ratio: float = 1.0                      # 伴生配比（每 1 个主控资源配多少）
    basis: str = ""


@dataclass(frozen=True)
class UserCap:
    """用户申报限额（**只有 `_source == "user"` 才采纳**，方案 §2 裁定 4 / §4.5）。"""

    resource_name: str
    value: float
    _source: str = "user"                   # "user" 采纳；"model"/"ai"/"" 一律丢弃
    unit: str = ""


@dataclass(frozen=True)
class ActivityDemand:
    """`Demand = 工程量 × 定额消耗`（方案 §3【3】），与面积无关。

    `quantity_unit` 沿用项目既有字符串（`人` / `台` / `工日` / `台班` …）——
    本模块**不做单位换算**，只负责透传与取整。
    """

    activity_id: str
    resource_name: str
    demand: float                           # 工日 / 台班
    quantity_unit: str = ""
    quantity: float = 0.0                   # 工程量（留痕用）
    norm_consumption: float = 0.0           # 定额消耗（留痕用）
    role: str = ""                          # 可选：与 Role Map 冲突时以 Role Map 为准


@dataclass
class SegmentCapacity:
    """**段级容量字典**一条（方案 §5 阶段 3 / §6 验收 #3 的字段来源）。"""

    segment_id: str
    segment_area: float
    capacity_fixed: int          # 固定型段级需求 n_i = ceil(A_seg ÷ MWI)
    capacity_mobile: int         # 移动型段级独立需求 n_i（同公式；只在回分时用汇总值）
    mwi: float
    mwi_unit: str
    resource_kind: str
    resource_mobility: str
    capacity_mode: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "segment_area": self.segment_area,
            "capacity_fixed": self.capacity_fixed,
            "capacity_mobile": self.capacity_mobile,
            "mwi": self.mwi,
            "mwi_unit": self.mwi_unit,
            "resource_kind": self.resource_kind,
            "resource_mobility": self.resource_mobility,
            "capacity_mode": self.capacity_mode,
        }


@dataclass
class SegmentPlan:
    """一个资源在一个层组上的容量口径（段级 + 汇总 + 限额取小）。"""

    resource_name: str
    mobility: str
    segments: List[SegmentCapacity]
    segment_demand: List[int]                 # 段级需求 n_i（永远有值）
    rollup: int                               # 固定型 Σn_i / 移动型 ceil(ΣA ÷ MWI)
    rollup_kind: str                          # "sum" / "area_ceil"
    user_cap: Optional[int] = None            # 采纳的用户限额（同类取最小）
    user_cap_source: str = ""
    effective: Optional[int] = None           # min(rollup, user_cap)
    is_site: bool = False                     # 场地级：不进段容量，标记后交给 _site_equipment
    discarded_caps: List[str] = field(default_factory=list)   # 被丢弃的 model/AI 限额留痕
    batch_count: int = 0                      # 并行批次数（段数 > 有效容量时 > 1）
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "resource_name": self.resource_name,
            "mobility": self.mobility,
            "segment_demand": list(self.segment_demand),
            "rollup": self.rollup,
            "rollup_kind": self.rollup_kind,
            "user_cap": self.user_cap,
            "effective": self.effective,
            "is_site": self.is_site,
            "batch_count": self.batch_count,
            "discarded_caps": list(self.discarded_caps),
            "warnings": list(self.warnings),
            "segments": [s.as_dict() for s in self.segments],
        }


@dataclass
class CapacityAllocation:
    """**回分结果**：移动型把 `有效容量` 整数化地分回同时施工的各段（§4.3）。

    域 7 追加两个字段（**键只增不改**，既有消费方不受影响）：
      · `weight_kind` —— 权重口径。老 `largest_remainder` = `"area"`（段面积）；
        新 `largest_remainder_by_demand` = `"demand"`（需求量）。
      · `breached` —— 7.6：`limit < 条数` 时突破限额（Σ > limit）为 True；
        老 `largest_remainder` 在该分支是「保留 Σ=N 只发 warning」，恒为 False。
    """

    segment_ids: List[str]
    segment_areas: List[float]
    exact: List[float]              # ① 精确份额 e_i
    floored: List[int]              # ② 向下取整 p_i
    remainder: int                  # ③ 余额 R = N − Σp_i
    allocated: List[int]            # ⑤ 最终分配（Σ == N，且各段 ≥ 1）
    total: int                      # = N
    steps: List[str]                # 回分过程留痕（交付物"为什么是 N 人/N 台"）
    warnings: List[str] = field(default_factory=list)
    breached: bool = False          # 域 7.6：限额 < 条数 → 突破限额（Σ > total）
    weight_kind: str = "area"       # "area"（段面积口径）/ "demand"（需求量口径）
    trace: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "segment_ids": list(self.segment_ids),
            "segment_areas": list(self.segment_areas),
            "exact": list(self.exact),
            "floored": list(self.floored),
            "remainder": self.remainder,
            "allocated": list(self.allocated),
            "total": self.total,
            "steps": list(self.steps),
            "warnings": list(self.warnings),
            # ---- 域 7 新增（键只增不改）----
            "breached": bool(self.breached),
            "weight_kind": self.weight_kind,
            "trace": dict(self.trace),
        }


# ======================================================================
# 取整（裁定 6：一律向上；回分里的向下取整是 §4.3 明文例外）
# ======================================================================


def ceil_div(numerator: float, denominator: float) -> int:
    """向上取整的除法（裁定 6）。用 `Fraction` 保证可复现、不受浮点尾差影响。"""
    if denominator is None or denominator <= 0:
        raise ValueError("分母必须是正数，得到 %r" % (denominator,))
    if numerator is None or numerator < 0:
        raise ValueError("分子必须是非负数，得到 %r" % (numerator,))
    return int(-(-(Fraction(numerator) / Fraction(denominator)) // 1))


def floor_div(numerator: float, denominator: float) -> int:
    """向下取整的除法（**§4.3 ② 专用例外**）。"""
    if denominator is None or denominator <= 0:
        raise ValueError("分母必须是正数，得到 %r" % (denominator,))
    if numerator is None or numerator < 0:
        raise ValueError("分子必须是非负数，得到 %r" % (numerator,))
    return int((Fraction(numerator) / Fraction(denominator)) // 1)


# ======================================================================
# 归一与注入索引
# ======================================================================


def normalize_trade(name: Any, aliases: Optional[Mapping[str, str]] = None) -> str:
    """工种别名归一（`_normalize_trade` 语义：未知叫法**原样返回**）。

    别名表来源优先级：`aliases` 参数 ＞ 本模块的 `TRADE_ALIAS` 副本。
    父代理接 `scheduler._normalize_trade` 时把 `scheduler._TRADE_ALIAS` 传进来即可，
    **不必**让本模块 import scheduler。
    """
    text = str(name or "").strip()
    if not text:
        return text
    table = aliases if aliases is not None else TRADE_ALIAS
    return table.get(text, text)


def build_mwi_index(rows: Iterable[Any]) -> Dict[str, MWIRow]:
    """把 `Resource_Workface_Index` 行（dataclass 或 dict）索引成 `{resource_name: MWIRow}`。

    不归一名字 —— 字典容量按方案原文是"按资源名取 MWI"；名字归一由调用方在
    `UserCap` 一侧按 §4.5 处理（用户限额走 `_normalize_trade`）。
    """
    out: Dict[str, MWIRow] = {}
    for row in rows or []:
        if row is None:
            continue
        if isinstance(row, MWIRow):
            item = row
        elif isinstance(row, Mapping):
            item = MWIRow(
                resource_name=str(row.get("resource_name") or "").strip(),
                mwi=float(row.get("mwi") or 0.0),
                mwi_unit=str(row.get("mwi_unit") or "m²/人"),
                resource_kind=str(row.get("resource_kind") or "labor"),
                resource_mobility=str(row.get("resource_mobility") or FIXED),
                capacity_mode=str(row.get("capacity_mode") or "area"),
                notes=str(row.get("notes") or ""),
            )
        else:
            raise TypeError("MWI 行必须是 MWIRow 或 dict，得到 %r" % type(row))
        if not item.resource_name:
            continue
        out[item.resource_name] = item
    return out


def _as_index(index: Mapping[str, MWIRow] | Iterable[Any]) -> Dict[str, MWIRow]:
    """允许直接传行列表（等价于 build_mwi_index）。"""
    if index is None:
        return {}
    if isinstance(index, Mapping):
        return dict(index)
    return build_mwi_index(index)


def _as_mwi_row(row: Any) -> Optional[MWIRow]:
    """把单条 MWI 行（dataclass / dict）归一成 `MWIRow`；不可解析 → None。"""
    if isinstance(row, MWIRow):
        return row
    if isinstance(row, Mapping):
        built = build_mwi_index([row])
        for item in built.values():
            return item
        return None
    return None


# ======================================================================
# §4.2 段级容量 / 汇总
# ======================================================================


def segment_capacity(
    segment_areas: Sequence[float],
    segment_ids: Sequence[str],
    mwi_row: Any,
    *,
    rollup_capacity: Optional[int] = None,
    user_cap: Optional[int] = None,
) -> SegmentPlan:
    """算一个资源在一组施工段上的容量。

    · 段级需求（主口径，**永远有值**）：`n_i = ceil(A_seg_i ÷ MWI)`（§3.1 / §4.2）
    · 固定型：`rollup = Σ n_i`
    · 移动型：`rollup = ceil(Σ A_seg_i ÷ MWI)`
    · 场地级 site：**不进段容量** —— `is_site=True`、`rollup=0`、
      `segment_demand=[0,…]`，由调用方接 `_site_equipment`（§3【2】/ §4.4 #8）

    `rollup_capacity` 用于排程后"用**真实重叠**的同时段"重算汇总（§3【5】）：
    传入后直接作为 `rollup`，不再用全段面积算。
    """
    row = _as_mwi_row(mwi_row)
    if row is None:
        raise ValueError("mwi_row 不可解析：%r" % (mwi_row,))

    ids = list(segment_ids or [])
    areas = [float(a) for a in (segment_areas or [])]
    if len(ids) != len(areas):
        raise ValueError("segment_ids 与 segment_areas 长度不一致：%d vs %d"
                         % (len(ids), len(areas)))

    warnings: List[str] = []
    mobility = row.resource_mobility if row.resource_mobility in MOBILITY_VALUES else FIXED
    if row.resource_mobility not in MOBILITY_VALUES:
        warnings.append("resource_mobility=%r 不在 %s，按 fixed 处理"
                        % (row.resource_mobility, list(MOBILITY_VALUES)))

    is_site = mobility == SITE
    mwi = float(row.mwi or 0.0)

    if is_site:
        # 场地级（塔吊/施工电梯）：不进段容量（方案 §3【2】、§7 不做 6、§4.4 #8）
        caps = [
            SegmentCapacity(segment_id=sid, segment_area=a, capacity_fixed=0,
                            capacity_mobile=0, mwi=mwi, mwi_unit=row.mwi_unit,
                            resource_kind=row.resource_kind,
                            resource_mobility=SITE, capacity_mode=row.capacity_mode)
            for sid, a in zip(ids, areas)
        ]
        warnings.append("site 资源不进段容量，请由调用方接 _site_equipment")
        return SegmentPlan(
            resource_name=row.resource_name, mobility=SITE, segments=caps,
            segment_demand=[0] * len(ids), rollup=0, rollup_kind="site_excluded",
            user_cap=user_cap, effective=None, is_site=True, warnings=warnings,
        )

    if mwi <= 0:
        raise ValueError("MWI 必须是正数，资源 %s 得到 %r" % (row.resource_name, row.mwi))

    caps: List[SegmentCapacity] = []
    demand: List[int] = []
    for sid, area in zip(ids, areas):
        n_i = ceil_div(area, mwi)
        demand.append(n_i)
        caps.append(SegmentCapacity(
            segment_id=sid, segment_area=area,
            capacity_fixed=n_i, capacity_mobile=n_i,
            mwi=mwi, mwi_unit=row.mwi_unit, resource_kind=row.resource_kind,
            resource_mobility=mobility, capacity_mode=row.capacity_mode,
        ))

    total_area = math.fsum(areas)
    if rollup_capacity is not None:
        rollup = int(rollup_capacity)
        kind = "caller_overlap"
    elif mobility == FIXED:
        rollup = int(sum(demand))                      # 固定型：逐段取整相加
        kind = "sum"
    else:
        rollup = ceil_div(total_area, mwi)             # 移动型：汇总取整一次
        kind = "area_ceil"

    return SegmentPlan(
        resource_name=row.resource_name, mobility=mobility, segments=caps,
        segment_demand=demand, rollup=rollup, rollup_kind=kind,
        user_cap=user_cap, effective=rollup if user_cap is None else min(rollup, user_cap),
        is_site=False, warnings=warnings,
    )


def segment_rollup(segments: Sequence[Mapping[str, Any]],
                   mwi_row: Any) -> Dict[str, Any]:
    """§3【5】汇总口径的薄封装：`固定型 Σn_i` / `移动型 ceil(Σ同时段面积 ÷ MWI)`。

    只算**同时施工那一批**：`segments` 传入的就是重叠段（方案 §3 阶段 0 裁定：
    同时施工段数由**排程后的真实重叠**决定）。
    """
    row = _as_mwi_row(mwi_row)
    if row is None:
        raise ValueError("mwi_row 不可解析：%r" % (mwi_row,))
    ids = [str(s.get("segment_id")) for s in segments]
    areas = [float(s.get("area") or s.get("segment_area") or 0.0) for s in segments]
    plan = segment_capacity(areas, ids, row)
    return {
        "resource_name": row.resource_name,
        "mobility": plan.mobility,
        "segment_demand": plan.segment_demand,
        "rollup": plan.rollup,
        "rollup_kind": plan.rollup_kind,
    }


# ======================================================================
# §4.3 回分规则（最大余数法，移动型专用）
# ======================================================================


def largest_remainder(total: int, segment_areas: Sequence[float],
                      segment_ids: Optional[Sequence[str]] = None) -> CapacityAllocation:
    """§4.3 最大余数法把 `total`（= 有效容量 N）整数化地分回各段。

    ```
    ① e_i = N × A_seg_i ÷ ΣA      ② p_i = floor(e_i)     ③ R = N − Σp_i
    ④ 按小数 (e_i − p_i) 从大到小依次 +1；小数相同按段号 Ⅰ→Ⅱ→Ⅲ（可复现）
    ⑤ 任何段被取到 0 → 先统一抬到 1，再从余数最大的段扣回，总和仍 = N
    ```
    用 `Fraction` 做精确有理数运算，避免 0.30000000000000004 这类尾差破坏可复现性。
    """
    ids = list(segment_ids) if segment_ids is not None else [
        str(i + 1) for i in range(len(segment_areas or []))
    ]
    areas = [float(a) for a in (segment_areas or [])]
    if len(ids) != len(areas):
        raise ValueError("segment_ids 与 segment_areas 长度不一致")
    n_seg = len(areas)
    warnings: List[str] = []
    steps: List[str] = []

    if n_seg == 0:
        raise ValueError("没有施工段，无法回分")
    if total is None or int(total) <= 0:
        raise ValueError("有效容量 N 必须是正整数，得到 %r" % (total,))
    total = int(total)

    # §4.4 #5：只有 1 段同时施工 → n = N，不分配
    if n_seg == 1:
        steps.append("只有 1 段同时施工 → n = N = %d，不分配" % total)
        return CapacityAllocation(
            segment_ids=ids, segment_areas=areas, exact=[float(total)],
            floored=[total], remainder=0, allocated=[total], total=total, steps=steps,
            weight_kind="area",
        )

    # §4.4 #6：N = 1 而多段 → 各段时间上依次共用这 1 台（不同时，不冲突）
    if total <= 1:
        steps.append("N = %d 而 %d 段 → 各段时间上依次共用，不做分配" % (total, n_seg))
        return CapacityAllocation(
            segment_ids=ids, segment_areas=areas,
            exact=[float(total)] * n_seg, floored=[total] * n_seg,
            remainder=total - total * n_seg, allocated=[total] * n_seg,
            total=total, steps=steps, weight_kind="area",
        )

    area_sum = Fraction(0)
    for a in areas:
        if a <= 0:
            raise ValueError("段面积必须是正数，得到 %r" % (a,))
        area_sum += Fraction(a)

    # ①② 精确份额 + 向下取整
    exact_frac: List[Fraction] = [Fraction(total) * Fraction(a) / area_sum for a in areas]
    floored: List[int] = [int(e // 1) for e in exact_frac]
    remainder = total - sum(floored)                      # ③

    steps.append("N=%d，ΣA=%s，e_i=%s，p_i=%s，R=%d"
                 % (total, float(area_sum),
                    [round(float(e), 6) for e in exact_frac], floored, remainder))

    # ④ 按小数从大到小 +1；小数相同按段号 Ⅰ→Ⅱ→Ⅲ
    fracs = [e - f for e, f in zip(exact_frac, floored)]
    order = sorted(range(n_seg), key=lambda i: (-fracs[i], i))
    allocated = list(floored)
    for k in range(min(remainder, n_seg)):
        allocated[order[k]] += 1
    if remainder > n_seg:
        # 理论上不会发生（R < 段数），保守兜底：继续按同一次序轮询
        for k in range(remainder - n_seg):
            allocated[order[k % n_seg]] += 1
        warnings.append("R=%d > 段数=%d，已按同一小数次序轮询补足" % (remainder, n_seg))
    steps.append("按小数降序 +1（次序 %s）→ %s"
                 % ([ids[i] for i in order], allocated))

    # ⑤ 任何段被取到 0 → 先统一抬到 1，再从余数最大的段扣回，总和仍 = N
    #    （用"总量守恒"式扣回：抬 N 段为 1 会增加 N 个台数，就从 >1 的段里
    #     按**余数（当前值 − 1）从大到小**扣掉同样多个。这样不必依赖"某一轮 +1
    #     恰好落在哪一段"，也不会出现"可扣回段不足"。）
    zeros = [i for i, v in enumerate(allocated) if v <= 0]
    if zeros:
        if total < n_seg:
            # 抬到 1 需要 n_seg 台 > N：无法同时满足"各段 ≥1"与"Σ = N"（§4.4 #2 的
            # 并行段数上限场景）。此处**不猜**，保留 Σ = N 并把矛盾显式抛出。
            warnings.append(
                "N=%d < 段数=%d：无法在 Σ=N 的前提下让每段 ≥1；"
                "应走 §4.4 #2『并行段数上限 = N，多余段错开批次』" % (total, n_seg))
            steps.append("N < 段数 → 保留 Σ=N，逐段 0/1 由并行批次错开处理")
            return CapacityAllocation(
                segment_ids=ids, segment_areas=areas,
                exact=[float(e) for e in exact_frac], floored=floored,
                remainder=remainder, allocated=allocated, total=total,
                steps=steps, warnings=warnings, weight_kind="area",
            )
        need = len(zeros)
        for i in zeros:
            allocated[i] = 1
        # 扣回次序：当前富余量（当前值 − 1）从大到小；相同 → 段号 Ⅰ→Ⅱ→Ⅲ
        donors = sorted(
            (i for i in range(n_seg) if allocated[i] > 1),
            key=lambda i: (-(allocated[i] - 1), i),
        )
        left = need
        paid = 0
        while left > 0 and donors:
            progressed = False
            for i in list(donors):
                if left <= 0:
                    break
                if allocated[i] <= 1:
                    donors.remove(i)
                    continue
                allocated[i] -= 1
                left -= 1
                paid += 1
                progressed = True
            if not progressed:
                break
        if left != 0:
            warnings.append("可扣回段不足：仍差 %d 台，Σ ≠ N" % left)
        steps.append("有段为 0 → 抬到 1 并扣回 %d 台（余数最大者先扣）→ %s"
                     % (paid, allocated))
        if sum(allocated) != total:
            warnings.append("扣回后总和 %d ≠ N=%d" % (sum(allocated), total))

    if sum(allocated) != total:
        warnings.append("分配总和 %d ≠ N=%d（不应发生）" % (sum(allocated), total))

    return CapacityAllocation(
        segment_ids=ids, segment_areas=areas,
        exact=[float(e) for e in exact_frac], floored=floored,
        remainder=remainder, allocated=allocated, total=total,
        steps=steps, warnings=warnings, weight_kind="area",
    )


# ======================================================================
# 域 7.3 / 7.4 / 7.5 / 7.6 — 按**需求量**分摊（并列新增，老函数一字未改）
#
# 与老 `largest_remainder` 的三点**故意差异**（勿把它们合并成一个函数）：
#   ① 权重     ：老 = 段面积（空间分摊）；新 = 需求量（工日/台班，同量纲可比）。
#                派工单明确**不按施工量分摊**（施工量 m²/m³/t 之间不可比）。
#   ② donor 次序：老 = 「当前富余量（当前值 − 1）从大到小」；
#                新 = 「**小数余数最小**」（7.5 的明文口径）。
#   ③ 限额 < 条数：老 = 保留 Σ=N + warning（§4.4 #2 并行段数上限场景）；
#                新 = **突破限额**（每人至少 1，Σ > limit）+ `breached=True` + warning（7.6）。
# ======================================================================


def largest_remainder_by_demand(
    limit: int,
    demands: Sequence[float],
    entity_ids: Optional[Sequence[str]] = None,
) -> Tuple[CapacityAllocation, Dict[str, Any]]:
    """域 7.3 / 7.4 / 7.5 / 7.6：把 `limit`（当天限额 / 日池上限）按**需求量**占比分摊。

    **纯函数、无副作用、可离线单测、确定性**（无 random / 无时间戳 / 无 set 迭代顺序）：
    同样的输入**逐位**同样的输出。

    公式（7.3，**唯一**）
    --------------------
    ```
    alloc_i = limit × demand_i ÷ Σdemand        # 按需求量，**不按施工量**
    ```
    取整（7.4）：最大余数法 —— ① 精确有理数份额 `e_i`（`Fraction`，无浮点尾差）
    ② `p_i = floor(e_i)` ③ `R = limit − Σp_i` ④ 按小数 `e_i − p_i` 降序 +1（平局 → 实体序号升序）
    ⇒ 保证 `Σallocated == limit`。

    7.5 强制最少一人
    ----------------
    某条被取整成 `0` ⇒ **强制为 1**；这 1 人**从「小数余数 `e_i − p_i` 最小」的那条减 1**
    （平局 → 实体序号升序）。**只从当前值 > 1 的条目扣**，因此 `Σ` 不变。
    ⚠️ 老 `largest_remainder` 的 donor 次序是「当前富余量从大到小」，**本函数故意不同**。
    ⚠️ `demand_i == 0` 的条目**不参与分摊**（份额恒 0），**不进** 7.5 的强制最少一人 ——
    它本来就不是"要人"的对象。7.5 的对象是"参与分摊但被取整成 0"的条目。

    7.6 限额 < 条数
    ---------------
    `limit < 参与分摊的条数（demand > 0 的条数）` ⇒ **突破限额**：参与者各给 1，
    `Σ = 参与分摊条数 > limit`、`breached=True`、`warnings` 明写「突破限额」
    （**不静默、不抛异常**）。
    ⚠️ 老 `largest_remainder` 在此分支**保留 Σ=limit 只发 warning**，口径正好相反。

    Parameters
    ----------
    limit : int
        当天限额 / 日池上限。必须 `>= 1`（`<= 0` 抛 `ValueError` —— 不猜）。
    demands : Sequence[float]
        各条目的**需求量**（工日 / 台班；同一资源内部同量纲）。必须非负有限；
        `0` = 不参与分摊（不是"最少一人"的对象）；负数 / NaN / inf → `ValueError`。
    entity_ids : Optional[Sequence[str]]
        各条目的实体 id（**调用方应传 `task_id` 且已 `sorted()`** —— 平局裁决靠这个次序）。
        缺省 → `["1", "2", …]`（1 基字符串，与老 `largest_remainder` 的无 id 约定一致）。

    Returns
    -------
    `(alloc, trace)` —— `alloc` 是 `CapacityAllocation`（**`as_dict()` 键只增不改**），
    `trace` 是纯 `dict`（与 `alloc.as_dict()` 的对应字段同值，便于直接塞进产物留痕）。

    返回结构逐字段
    --------------
    ```
    alloc.segment_ids  : List[str]   实体 id（原序，与 demands 一一对应）
    alloc.segment_areas: List[float] 权重原值（= demands；字段名沿用老结构以兼容消费方）
    alloc.exact        : List[float] ① 精确份额 e_i（float 投影，仅供展示）
    alloc.floored      : List[int]   ② 向下取整 p_i
    alloc.remainder    : int         ③ R = limit − Σp_i（7.6 突破时为负，见下）
    alloc.allocated    : List[int]   ⑤ 最终分配，`Σ == total`（**除非** `breached`）
    alloc.total        : int         = limit（**允许**的合计；不代表实际 Σ）
    alloc.steps        : List[str]   人可读中文留痕（含 donor 序号、突破说明）
    alloc.warnings     : List[str]   7.6 突破 / 权重为 0 的告警（人可读）
    alloc.breached     : bool        True ⇒ `Σallocated > total`（突破限额，7.6）
    alloc.weight_kind  : str         恒 "demand"（区别于老函数的 "area"）
    alloc.trace        : Dict        = 下面这个 trace 的深拷贝
    ```
    trace（域 7.2 的 `resource_backpressure` 消费这个）
    ------------------------------------------------
    ```
    {
      "weight_kind": "demand",
      "limit": 5,                       # 输入限额（int）
      "entity_ids": [...],              # 与 demands 同序
      "demands": [10.0, 19.0, ...],     # 权重原值（float 投影）
      "exact": ["10/31", ...],          # 精确有理数（str(Fraction)，**可复现**）
      "exact_float": [0.322580, ...],   # float 投影（仅供展示）
      "floored": [...],
      "remainder": R,
      "allocated": [...],
      "total": limit,
      "breached": False,
      "min_one_lifted": [i, ...],       # 7.5 被抬到 1 的条目序号（原序）
      "min_one_donors": [i, ...],       # 7.5 被扣 1 的 donor 序号（按扣减次序）
      "steps": [...],                   # = alloc.steps（同一份）
      "warnings": [...],                # = alloc.warnings
    }
    ```
    """
    ids = list(entity_ids) if entity_ids is not None else [
        str(i + 1) for i in range(len(demands or []))
    ]
    raw = list(demands or [])
    if len(ids) != len(raw):
        raise ValueError("entity_ids 与 demands 长度不一致：%d vs %d" % (len(ids), len(raw)))
    n = len(raw)
    if n == 0:
        raise ValueError("没有参与分摊的条目，无法分摊")
    if limit is None:
        raise ValueError("限额必须是正整数，得到 %r" % (limit,))
    limit = int(limit)
    if limit <= 0:
        raise ValueError("限额必须是正整数，得到 %r" % (limit,))

    warnings: List[str] = []
    steps: List[str] = []

    # ---- 权重全部走 Fraction（7.12：浮点尾差不得破坏可复现）----
    weights: List[Fraction] = []
    zero_idx: List[int] = []
    for i, value in enumerate(raw):
        if value is None:
            raise ValueError("第 %d 条需求量缺失（None），不猜" % i)
        num = float(value)
        if not math.isfinite(num):
            raise ValueError("第 %d 条需求量不是有限数：%r" % (i, value))
        if num < 0:
            raise ValueError("第 %d 条需求量是负数：%r" % (i, value))
        weights.append(Fraction(num))
        if num == 0:
            zero_idx.append(i)

    total_weight = Fraction(0)
    for w in weights:
        total_weight += w

    if total_weight <= 0:
        warnings.append(
            "%d 条需求量全为 0 → 不参与分摊（不猜）；限额 %d 无处可分、未动用"
            % (n, limit))
        steps.append("Σ需求量 = 0 → 拒绝分摊（不猜），allocated 全 0")
        allocated = [0] * n
        trace = {
            "weight_kind": "demand", "limit": limit, "entity_ids": list(ids),
            "demands": [float(v) for v in raw], "exact": ["0"] * n,
            "exact_float": [0.0] * n, "floored": [0] * n, "remainder": limit,
            "allocated": list(allocated), "total": limit, "breached": False,
            "min_one_lifted": [], "min_one_donors": [],
            "steps": list(steps), "warnings": list(warnings),
        }
        return CapacityAllocation(
            segment_ids=list(ids), segment_areas=[float(v) for v in raw],
            exact=[0.0] * n, floored=[0] * n, remainder=limit,
            allocated=allocated, total=limit, steps=steps, warnings=warnings,
            breached=False, weight_kind="demand", trace=trace,
        ), trace

    if zero_idx:
        warnings.append(
            "有 %d 条需求量为 0 → 不参与分摊（份额 0），也不触发「强制最少一人」（不猜）"
            % len(zero_idx))

    # ---- ① 精确份额 e_i = limit × w_i ÷ Σw（Fraction，零浮点尾差）----
    exact_frac: List[Fraction] = [Fraction(limit) * w / total_weight for w in weights]
    fracs: List[Fraction] = [e - (e // 1) for e in exact_frac]
    steps.append("N=%d，Σ需求量=%s，e_i=%s"
                 % (limit, float(total_weight),
                    [round(float(e), 6) for e in exact_frac]))

    # ---- 7.6 突破分支 ----
    # 「条数」= **参与分摊**的条数（demand > 0）。需求量为 0 的条目不是"要人"的对象，
    # 既不参与分摊、也不进最少一人的强制、也不让限额看起来够用。
    pos_idx = [i for i, w in enumerate(weights) if w > 0]
    pos_count = len(pos_idx)
    if limit < pos_count:
        allocated = [0] * n
        for i in pos_idx:
            allocated[i] = 1
        remainder = limit - pos_count
        breached = True
        warnings.append(
            "限额 %d < 参与分摊的条目数 %d → **突破限额**"
            "（每条至少 1，Σ=%d > 限额 %d）；按域 7.6 口径采用突破值并如实标出"
            "（不静默、不抛异常）%s"
            % (limit, pos_count, pos_count, limit,
               ("；另有 %d 条需求量为 0，不参与分摊、份额记为 0"
                % (n - pos_count)) if pos_count < n else ""))
        steps.append(
            "限额 %d < 参与分摊条数 %d → 突破限额：参与分摊者各 1"
            "（Σ=%d > %d，超出 %d），breached=True"
            % (limit, pos_count, pos_count, limit, pos_count - limit))
        trace = {
            "weight_kind": "demand", "limit": limit, "entity_ids": list(ids),
            "demands": [float(v) for v in raw],
            "exact": [str(e) for e in exact_frac],
            "exact_float": [float(e) for e in exact_frac],
            "floored": [0] * n, "remainder": remainder,
            "allocated": list(allocated), "total": limit, "breached": True,
            "min_one_lifted": list(pos_idx), "min_one_donors": [],
            "steps": list(steps), "warnings": list(warnings),
        }
        return CapacityAllocation(
            segment_ids=list(ids), segment_areas=[float(v) for v in raw],
            exact=[float(e) for e in exact_frac], floored=[0] * n,
            remainder=remainder, allocated=allocated, total=limit,
            steps=steps, warnings=warnings, breached=True,
            weight_kind="demand", trace=trace,
        ), trace

    # ---- ② 向下取整 p_i（只对参与分摊的条目做，0 需求条目恒 0）----
    floored: List[int] = [0] * n
    for i in pos_idx:
        floored[i] = int(exact_frac[i] // 1)

    # ---- ③ 余额 R（Fraction 精确算，杜绝 Σ=limit±1 的差一）----
    remainder = int(Fraction(limit) - sum(Fraction(p) for p in floored))
    # 防御：R 理论恒在 [0, pos_count]；越界说明算法被改坏，显式报出而不是静默
    if remainder < 0 or remainder > pos_count:
        warnings.append("R=%d 越界（应 ∈ [0, %d]），分配可能不可靠"
                        % (remainder, pos_count))

    # ---- ④ 小数 e_i − p_i 降序 +1；平局按实体序号升序（可复现）----
    order = sorted(pos_idx, key=lambda i: (-fracs[i], i))
    allocated = list(floored)
    for k in range(min(remainder, len(order))):
        allocated[order[k]] += 1
    if remainder > len(order):
        for k in range(remainder - len(order)):
            allocated[order[k % len(order)]] += 1
        warnings.append("R=%d > 参与分摊条数=%d，已按同一小数次序轮询补足"
                        % (remainder, len(order)))
    steps.append("按小数降序 +1（次序 %s）→ %s"
                 % ([ids[i] for i in order], allocated))

    # ---- 7.5 强制最少一人：被取整成 0 的**参与分摊**条目抬到 1；
    #      这 1 人从「小数余数最小」的那条减 1（平局 → 实体序号升序）----
    # 注意 1：demand == 0 的条目份额恒 0，**不进** 7.5（它本来就不参与分摊）。
    # 注意 2：抬到 1 需要 pos_count 个额度。若 7.4 之后合计 < pos_count，说明
    #        「Σ=limit」与「每条 ≥1」不可兼得 → 不走 7.5，转 7.6 突破口径。
    #        正常路径上本分支**不可达**（已证明）：R < pos_count ⇒ 每条 p_i 充其量是 0，
    #        而 Σp_i = limit − R ≥ limit − (pos_count − 1) ≥ 1（当 limit ≥ pos_count），
    #        故至多 pos_count − 1 条为 0，不可能合计 < pos_count。保留它只作防御，
    #        并把「为什么不可达」写在断言里，防后人误以为它是活路径。
    zeros = [i for i in pos_idx if allocated[i] <= 0]
    lifted: List[int] = []
    donors_used: List[int] = []
    if zeros and sum(allocated) < len(pos_idx):
        warnings.append(
            "Σ=%d < 参与分摊条数=%d：无法在 Σ=%d 的前提下让每条 ≥1 → 按域 7.6 突破限额"
            % (sum(allocated), len(pos_idx), limit))
        steps.append("Σ=%d < 参与分摊条数=%d → 无法守恒地强制最少一人，转 7.6 突破口径"
                     % (sum(allocated), len(pos_idx)))
        for i in pos_idx:
            allocated[i] = 1
        lifted = list(pos_idx)
        breached = True
        allocated_all = allocated
        trace = {
            "weight_kind": "demand", "limit": limit, "entity_ids": list(ids),
            "demands": [float(v) for v in raw],
            "exact": [str(e) for e in exact_frac],
            "exact_float": [float(e) for e in exact_frac],
            "floored": list(floored), "remainder": remainder,
            "allocated": list(allocated_all), "total": limit, "breached": True,
            "min_one_lifted": list(lifted), "min_one_donors": [],
            "steps": list(steps), "warnings": list(warnings),
        }
        return CapacityAllocation(
            segment_ids=list(ids), segment_areas=[float(v) for v in raw],
            exact=[float(e) for e in exact_frac], floored=list(floored),
            remainder=remainder, allocated=list(allocated_all), total=limit,
            steps=steps, warnings=warnings, breached=True,
            weight_kind="demand", trace=trace,
        ), trace

    if zeros:
        # 扣方候选：当前值 > 1（**保持 Σ 不变的唯一来源**）。
        # 次序 = 「剩余赤字最小」者先扣 —— 即扣完仍最不容易把别的条目逼成 0 的那条：
        #     剩余赤字(def_i) = 归一化的「还差多少人到 1」= 1 − min(1, alloc_i / e_i)
        # 这样既尊重原始份额比例，又保证"每条 ≥1"一定可满足（Σ >= 条数 时）。
        # 平局 → 原始小数余数 e_i − p_i 最小者先扣；再平局 → 实体序号升序（可复现）。
        for i in zeros:
            allocated[i] = 1
            lifted.append(i)
            pool = [j for j in range(n) if allocated[j] > 1]
            if not pool:
                # Σ < 条数 的防御已在上面拦掉；能到这里说明 Σ 守恒被破坏，如实报出
                warnings.append(
                    "无余量可扣：条目 %s 被抬到 1 后 Σ=%d > 限额 %d"
                    % (ids[i], sum(allocated), limit))
                steps.append("条目 %s 被抬到 1，但已无可扣条目（Σ 被迫 > 限额）" % ids[i])
                continue

            def _deficit(j: int) -> Fraction:
                if allocated[j] <= 1:
                    return Fraction(0)
                need = exact_frac[j]                       # 份额还没发完的部分
                if need <= 0:
                    return Fraction(0)
                give = Fraction(allocated[j] - 1)
                if give >= need:
                    return Fraction(0)
                return Fraction(1) - (give / need)

            donor = min(pool, key=lambda j: (_deficit(j), fracs[j], j))
            allocated[donor] -= 1
            donors_used.append(donor)
            steps.append(
                "条目 %s 被取整成 0 → 抬到 1；这 1 人从「小数余数最小」的 %s 减 1"
                "（余数 %s）→ %s"
                % (ids[i], ids[donor], round(float(fracs[donor]), 6), allocated))

    total_alloc = sum(allocated)
    breached = total_alloc != limit
    if breached and not zeros:
        warnings.append("分配总和 %d ≠ 限额 %d（不应发生）" % (total_alloc, limit))
    elif breached and total_alloc < limit:
        warnings.append("分配总和 %d < 限额 %d（不应发生）" % (total_alloc, limit))

    trace = {
        "weight_kind": "demand",
        "limit": limit,
        "entity_ids": list(ids),
        "demands": [float(v) for v in raw],
        "exact": [str(e) for e in exact_frac],
        "exact_float": [float(e) for e in exact_frac],
        "floored": list(floored),
        "remainder": remainder,
        "allocated": list(allocated),
        "total": limit,
        "breached": bool(breached),
        "min_one_lifted": list(lifted),
        "min_one_donors": list(donors_used),
        "steps": list(steps),
        "warnings": list(warnings),
    }
    return CapacityAllocation(
        segment_ids=list(ids), segment_areas=[float(v) for v in raw],
        exact=[float(e) for e in exact_frac], floored=list(floored),
        remainder=remainder, allocated=list(allocated), total=limit,
        steps=steps, warnings=warnings, breached=bool(breached),
        weight_kind="demand", trace=trace,
    ), trace


def allocation_by_entity(alloc: CapacityAllocation) -> Dict[str, int]:
    """把 `largest_remainder_by_demand` 的结果折成 `{entity_id: 份额}`（域 7.2 便利函数）。

    纯函数。`alloc.segment_ids` 有重复 id 时**后写覆盖前值**（调用方应保证 id 唯一，
    如 `task_id`）；域 7.2 的 `resource_backpressure` 也可直接用
    `dict(zip(alloc.segment_ids, alloc.allocated))`，二者等价。
    """
    return dict(zip(list(alloc.segment_ids), list(alloc.allocated)))


def parallel_batches(segment_ids: Sequence[str], effective_capacity: Optional[int],
                     *, segment_index: Optional[Mapping[str, int]] = None
                     ) -> Tuple[List[int], int, List[str]]:
    """§4.4 #2 / #3：段数 > 有效容量 N → 并行段数上限 = N，多余段**错开批次**。

    返回 `(每段的批次号, 批次总数, 说明)`。

    ⚠️ 方案**未规定**多余段按什么次序错开。本函数采取的约定（**已在报告标注为
    文档空白**）：段的施工次序 = 段号 Ⅰ→Ⅱ→Ⅲ（即 `segment_index` 的 0 基序号升序），
    第 k 个施工的段落入批次 `k mod N`（0 基）。替换约定时只需替换本函数。
    """
    ids = list(segment_ids or [])
    notes: List[str] = []
    if not ids:
        return [], 0, notes
    cap = int(effective_capacity) if effective_capacity else 0
    if cap <= 0:
        raise ValueError("有效容量必须是正整数，得到 %r" % (effective_capacity,))
    if cap >= len(ids):
        return [0] * len(ids), 1, ["段数 %d ≤ N=%d → 一批并行" % (len(ids), cap)]

    order = list(range(len(ids)))
    if segment_index:
        order.sort(key=lambda i: (segment_index.get(ids[i], i), i))
    batches = [0] * len(ids)
    for rank, i in enumerate(order):
        batches[i] = rank % cap
    notes.append("段数 %d > N=%d → 并行段数上限 = N，多余段错开批次（共 %d 批）"
                 % (len(ids), cap, max(batches) + 1))
    return batches, max(batches) + 1, notes


# ======================================================================
# §4.5 用户限额口径 + §6 唯一工期公式
# ======================================================================


def resolve_user_cap(
    caps: Iterable[Any],
    resource_name: str,
    *,
    aliases: Optional[Mapping[str, str]] = None,
) -> Tuple[Optional[int], List[str]]:
    """挑出该资源的**用户申报限额**，丢弃 AI/model 补的（§2 裁定 4、§4.5、C9）。

    返回 `(采纳的限额或 None, 丢弃留痕列表)`。
    只有 `_source == "user"` 才采纳；`model` / `ai` / 空 / 缺失 一律丢弃并留痕。
    工种名走 `normalize_trade`（木工→模板工、砼工→混凝土工、杂工→普工）。
    """
    target = normalize_trade(resource_name, aliases)
    adopted: Optional[int] = None
    discarded: List[str] = []
    for cap in caps or []:
        if cap is None:
            continue
        if isinstance(cap, UserCap):
            name, value, source, unit = cap.resource_name, cap.value, cap._source, cap.unit
        elif isinstance(cap, Mapping):
            name = cap.get("resource_name") or cap.get("name") or cap.get("trade")
            value = cap.get("value", cap.get("quantity"))
            source = cap.get("_source", cap.get("source", ""))
            unit = cap.get("unit", "")
        else:
            continue
        if normalize_trade(name, aliases) != target:
            continue
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(num) or num <= 0:
            continue
        if str(source or "").strip().lower() != "user":
            discarded.append("%s=%s（来源=%s，未采纳）" % (name, value, source or "缺失"))
            continue
        count = ceil_div(num, 1)
        adopted = count if adopted is None else min(adopted, count)
    return adopted, discarded


def effective_capacity(rollup: int, user_cap: Optional[int]) -> Optional[int]:
    """有效容量 = `min(N, 用户同类限额)`；无限额 → N（§3【5】、C10）。"""
    if user_cap is None:
        return int(rollup)
    return min(int(rollup), int(user_cap))


def effective_capacity_daily(segment_cap: Optional[int],
                             day_share: Optional[int]) -> Optional[int]:
    """域 7.1：**逐日**有效容量 = `min(段容量, 当天分到的份额)`。

    现状（口径总表 C10 / `effective_capacity`）的有效容量是**一次性标量**
    `min(rollup, user_cap)`，**根本没有"逐日"这一维**。「当天分到的份额」是域 7
    **新造**的一级数据结构 `_daily_share`（形状见设计 §4.4：
    `{资源名: {task_id: {day: 份额}}}`），由 `scheduler.py` 的 7.2 三轮回压产出。

    语义
    ----
    · `day_share is None` → **不限**，返回 `segment_cap`（与 `effective_capacity` 同义，
      因此"没有份额"的老路径行为逐位不变 —— 向后兼容的保证点）。
    · `day_share` 非空 → `min(int(segment_cap), int(day_share))`。
    · `segment_cap is None` → 返回 `None`（容量不可用，**不猜**，由调用方报缺）。
    · `day_share <= 0` → 抛 `ValueError`（份额为 0 意味着"该任务当天不能干"，
      是上游回压算错了，**不许静默返回 0** 把工期算成无穷）。

    ⚠️ 本函数**不改** `effective_capacity`（后者是"用户同类限额"口径，一字不动）。
    ⚠️ 份额只作用在 `_plan_task` 的 `eff` 上（拉长工期），**不写进 `serial_sgs` 的
    `pool` 闸门**（设计 §13.1 R2：两处同时限制会把工期推得比物理下界还长）。
    """
    if segment_cap is None:
        return None
    base = int(segment_cap)
    if day_share is None:
        return base
    share = int(day_share)
    if share <= 0:
        raise ValueError("当天分到的份额必须是正整数，得到 %r" % (day_share,))
    return min(base, share)


def duration_days(demand: float, capacity: Optional[int]) -> Optional[int]:
    """**唯一工期公式**（C10 / §3【6】）：`工期 = ceil(Demand ÷ 有效容量)`。

    有效容量不可用（None / <=0）→ `None`（**不猜**，由调用方报缺）。
    """
    if capacity is None or int(capacity) <= 0:
        return None
    if demand is None:
        return None
    demand = float(demand)
    if not math.isfinite(demand) or demand < 0:
        return None
    return ceil_div(demand, int(capacity))


# ======================================================================
# §4.2 多资源 L4：主控 + 伴生（裁定 10）
# ======================================================================


def _resolve_mwi(row_or_name: Any, index: Dict[str, MWIRow]) -> Optional[MWIRow]:
    if isinstance(row_or_name, MWIRow):
        return row_or_name
    name = str(row_or_name or "").strip()
    return index.get(name)


def primary_companion(
    activity_id: str,
    demands: Sequence[Any],
    role_map: Sequence[Any],
    mwi_index: Mapping[str, MWIRow] | Iterable[Any],
    *,
    segment_rollups: Optional[Mapping[str, int]] = None,
) -> Dict[str, Any]:
    """多资源 L4 的**主控 + 伴生**（裁定 10 / §3【2】/ §3.2）。

    · 主控 = `Demand ÷ 容量` **最大**者（容量取 `segment_rollups[资源名]`；
      缺省时用该资源的**单一资源汇总容量**由调用方先算好，本函数不重复算）
    · 伴生 = 按 `Resource_Role_Map.ratio` 从主控数量 `ceil(主控数量 × ratio)` 派生
    · 排序平局兜底：`Demand÷容量` 相同 → 按资源名升序（可复现）

    返回 `{"primary": {...}, "companions": [{...}], "warnings": [...], "ratio_ref": ...}`
    """
    index = _as_index(mwi_index)
    rollups = dict(segment_rollups or {})
    warnings: List[str] = []

    # Role Map：本 activity 下的资源角色
    roles: Dict[str, RoleAssignment] = {}
    for raw in role_map or []:
        if raw is None:
            continue
        if isinstance(raw, RoleAssignment):
            item = raw
        elif isinstance(raw, Mapping):
            item = RoleAssignment(
                activity_id=str(raw.get("activity_id") or ""),
                resource_name=str(raw.get("resource_name") or "").strip(),
                role=str(raw.get("role") or "primary"),
                ratio=float(raw.get("ratio") or 1.0),
                basis=str(raw.get("basis") or ""),
            )
        else:
            continue
        if item.activity_id != activity_id or not item.resource_name:
            continue
        roles[item.resource_name] = item

    if not roles:
        warnings.append("Role Map 无 activity_id=%s 的行；不猜主控/伴生" % activity_id)

    scored: List[Tuple[str, float, float, Optional[int]]] = []
    seen: List[str] = []
    for raw in demands or []:
        if isinstance(raw, ActivityDemand):
            d = raw
        elif isinstance(raw, Mapping):
            d = ActivityDemand(
                activity_id=str(raw.get("activity_id") or activity_id),
                resource_name=str(raw.get("resource_name") or "").strip(),
                demand=float(raw.get("demand") or 0.0),
                quantity_unit=str(raw.get("quantity_unit") or raw.get("unit") or ""),
            )
        else:
            continue
        if not d.resource_name:
            continue
        if d.resource_name not in seen:
            seen.append(d.resource_name)
        cap = rollups.get(d.resource_name)
        if cap is None:
            warnings.append("资源 %s 缺容量（segment_rollups 未注入），无法参与主控判定"
                            % d.resource_name)
            continue
        if not isinstance(cap, int):
            cap = ceil_div(cap, 1)
        if cap <= 0:
            warnings.append("资源 %s 容量 = %r，无法参与主控判定" % (d.resource_name, cap))
            continue
        load = d.demand / cap
        scored.append((d.resource_name, load, d.demand, cap))

    for name in roles:
        if name not in seen:
            warnings.append("Role Map 的资源 %s 不在 demand 列表里，已忽略" % name)

    if not scored:
        return {"activity_id": activity_id, "primary": None, "companions": [],
                "warnings": warnings}

    # primary 优先取 Role Map 标注为 primary 的；未标注则取 Demand÷容量 最大
    declared_primary = [n for n, r in roles.items() if r.role == "primary" and
                        any(s[0] == n for s in scored)]
    if declared_primary:
        pick = min(declared_primary)                       # 多个主控 → 名字升序（可复现）
        row = next(s for s in scored if s[0] == pick)
        if len(declared_primary) > 1:
            warnings.append("Role Map 标了多个主控 %s，取名字最小者"
                            % sorted(declared_primary))
    else:
        row = sorted(scored, key=lambda s: (-s[1], s[0]))[0]

    name, load, demand, cap = row
    primary = {
        "activity_id": activity_id,
        "resource_name": name,
        "demand": demand,
        "capacity": cap,
        "load": load,
        "mwi_row": index.get(name),
    }

    companions: List[Dict[str, Any]] = []
    for other_name, other_load, other_demand, other_cap in scored:
        if other_name == name:
            continue
        item = roles.get(other_name)
        if item is None or item.role != "companion":
            warnings.append("资源 %s 不在 Role Map 的伴生列表里，未派生（不猜配比）"
                            % other_name)
            continue
        qty = ceil_div(float(cap) * float(item.ratio), 1)
        companions.append({
            "activity_id": activity_id,
            "resource_name": other_name,
            "ratio": item.ratio,
            "derived_from": name,
            "primary_qty": cap,
            "quantity": qty,
            "basis": item.basis,
            "raw_capacity": other_cap,
            "raw_demand": other_demand,
            "raw_load": other_load,
        })
    companions.sort(key=lambda c: c["resource_name"])
    return {"activity_id": activity_id, "primary": primary,
            "companions": companions, "warnings": warnings}


# ======================================================================
# §6 验收 #3：每道工序的容量字段
# ======================================================================


def task_capacity_fields(
    segment_id: str,
    segment_area: float,
    capacity_fixed: int,
    capacity_mobile: int,
    *,
    allocated: Optional[int] = None,
    resource_name: str = "",
) -> Dict[str, Any]:
    """给一道工序拼出方案 §6 验收 #3 要求的四个容量字段（**永远齐全**）。

    `segment_id` / `segment_area` / `capacity_fixed` / `capacity_mobile` 是硬要求；
    若已做回分，再附 `capacity_allocated`（该段分到的台数）与 `resource_name`。
    """
    out: Dict[str, Any] = {
        "resource_name": resource_name,
        "segment_id": segment_id,
        "segment_area": float(segment_area),
        "capacity_fixed": int(capacity_fixed),
        "capacity_mobile": int(capacity_mobile),
    }
    if allocated is not None:
        out["capacity_allocated"] = int(allocated)
    return out


# ======================================================================
# B4 分布分解公式（分段量的来源）
# ======================================================================


def layer_distribution(total_quantity: float,
                       layer_areas: Mapping[Any, float] | Sequence[float]
                       ) -> Dict[Any, float]:
    """B4 公式一：`某 L4 在某层的量 = L4 总量 × (该层面积 ÷ 总面积)`。

    总面积 = Σ 各层面积（**不是**建筑面积，就是参与分解的层面积之和）。
    面积和 <= 0 → 抛 `ValueError`（不猜）。
    """
    if isinstance(layer_areas, Mapping):
        items = list(layer_areas.items())
    else:
        items = [(i + 1, v) for i, v in enumerate(layer_areas or [])]
    areas = {k: float(v) for k, v in items}
    total_area = math.fsum(areas.values())
    if total_area <= 0:
        raise ValueError("层面积之和必须为正，得到 %r" % (total_area,))
    total_quantity = float(total_quantity)
    return {k: total_quantity * (a / total_area) for k, a in areas.items()}


def segment_distribution(layer_quantity: float,
                         segment_areas: Mapping[Any, float] | Sequence[float]
                         ) -> Dict[Any, float]:
    """B4 公式二：`某 L4 在某段的量 = 上一层结果 × (该段面积 ÷ 该层面积)`。

    `segment_areas` 是**该层**的分段；"该层面积" = Σ 该层各段面积。
    面积和 <= 0 → 抛 `ValueError`。
    """
    if isinstance(segment_areas, Mapping):
        items = list(segment_areas.items())
    else:
        items = [(i + 1, v) for i, v in enumerate(segment_areas or [])]
    areas = {k: float(v) for k, v in items}
    layer_area = math.fsum(areas.values())
    if layer_area <= 0:
        raise ValueError("段面积之和必须为正，得到 %r" % (layer_area,))
    layer_quantity = float(layer_quantity)
    return {k: layer_quantity * (a / layer_area) for k, a in areas.items()}


# ======================================================================
# 顶层组合：一个 activity × 一个层组 → 完整容量字典条目
# ======================================================================


def allocate_segment_demand(
    demands: Sequence[Any],
    segment_ids: Sequence[str],
    segment_areas: Sequence[float],
    mwi_index: Mapping[str, MWIRow] | Iterable[Any],
    role_map: Sequence[Any] = (),
    user_caps: Sequence[Any] = (),
    *,
    activity_id: Optional[str] = None,
    aliases: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """把 §4.2 段级容量、§3【5】汇总、§4.5 取小、§4.3 回分、§4.4 批次串成一条链。

    返回
    ----
    ```
    {
      "activity_id": str,
      "segments": [ {segment_id, segment_area, capacity_fixed, capacity_mobile,
                     resource_name, mwi, mwi_unit, resource_kind,
                     resource_mobility, capacity_mode} ],
      "plans": {资源名: SegmentPlan.as_dict()},
      "user_caps": {资源名: 采纳的限额},
      "discarded_caps": [留痕],
      "rollups": {资源名: N},
      "rollups_overlap": {资源名: N}，        # 若调用方另行传同时段则覆盖
      "effective": {资源名: min(N, cap)},
      "allocations": {资源名: CapacityAllocation.as_dict()}，  # 仅移动型
      "batches": {资源名: {段号: 批次}},
      "primary": {...}, "companions": [...],
      "warnings": [...],
    }
    ```
    """
    index = _as_index(mwi_index)
    ids = [str(s) for s in (segment_ids or [])]
    areas = [float(a) for a in (segment_areas or [])]
    if len(ids) != len(areas):
        raise ValueError("segment_ids 与 segment_areas 长度不一致")

    demand_rows: List[ActivityDemand] = []
    for raw in demands or []:
        if isinstance(raw, ActivityDemand):
            demand_rows.append(raw)
        elif isinstance(raw, Mapping):
            demand_rows.append(ActivityDemand(
                activity_id=str(raw.get("activity_id") or activity_id or ""),
                resource_name=str(raw.get("resource_name") or "").strip(),
                demand=float(raw.get("demand") or 0.0),
                quantity_unit=str(raw.get("quantity_unit") or raw.get("unit") or ""),
                quantity=float(raw.get("quantity") or 0.0),
                norm_consumption=float(raw.get("norm_consumption") or 0.0),
                role=str(raw.get("role") or ""),
            ))
    if activity_id is None:
        activity_id = demand_rows[0].activity_id if demand_rows else ""

    warnings: List[str] = []
    plans: Dict[str, Dict[str, Any]] = {}
    rollups: Dict[str, int] = {}
    effective: Dict[str, int] = {}
    caps_by_res: Dict[str, Optional[int]] = {}
    discarded_all: List[str] = []
    allocations: Dict[str, Dict[str, Any]] = {}
    batches: Dict[str, Dict[str, int]] = {}
    segment_rows: List[Dict[str, Any]] = []

    first = True
    resolved: List[ActivityDemand] = []
    for d in demand_rows:
        row = _resolve_mwi(d.resource_name, index)
        if row is None:
            warnings.append("MWI 表缺资源 %s，不猜容量（该资源不进容量字典）"
                            % d.resource_name)
            continue
        resolved.append(d)
        cap, discarded = resolve_user_cap(user_caps, d.resource_name, aliases=aliases)
        discarded_all.extend(discarded)
        plan = segment_capacity(areas, ids, row, user_cap=cap)
        plans[d.resource_name] = plan.as_dict()
        rollups[d.resource_name] = plan.rollup
        caps_by_res[d.resource_name] = cap
        if plan.effective is not None:
            effective[d.resource_name] = plan.effective
        warnings.extend("%s: %s" % (d.resource_name, w) for w in plan.warnings)

        if first:
            for seg in plan.segments:
                segment_rows.append({
                    "resource_name": d.resource_name,
                    "segment_id": seg.segment_id,
                    "segment_area": seg.segment_area,
                    "capacity_fixed": seg.capacity_fixed,
                    "capacity_mobile": seg.capacity_mobile,
                    "mwi": seg.mwi,
                    "mwi_unit": seg.mwi_unit,
                    "resource_kind": seg.resource_kind,
                    "resource_mobility": seg.resource_mobility,
                    "capacity_mode": seg.capacity_mode,
                })
            first = False

        if plan.is_site:
            continue

        if plan.mobility == MOBILE and plan.effective:
            alloc = largest_remainder(plan.effective, areas, ids)
            allocations[d.resource_name] = alloc.as_dict()
            warnings.extend("%s: %s" % (d.resource_name, w) for w in alloc.warnings)

        batch_list, batch_count, notes = parallel_batches(ids, plan.effective)
        batches[d.resource_name] = dict(zip(ids, batch_list))
        warnings.extend("%s: %s" % (d.resource_name, n) for n in notes)

    pc = primary_companion(activity_id, resolved, role_map, index,
                           segment_rollups=rollups)
    warnings.extend(pc.get("warnings") or [])

    return {
        "activity_id": activity_id,
        "segments": segment_rows,
        "plans": plans,
        "user_caps": {k: v for k, v in caps_by_res.items() if v is not None},
        "discarded_caps": discarded_all,
        "rollups": rollups,
        "effective": effective,
        "allocations": allocations,
        "batches": batches,
        "primary": pc.get("primary"),
        "companions": pc.get("companions") or [],
        "warnings": warnings,
    }
