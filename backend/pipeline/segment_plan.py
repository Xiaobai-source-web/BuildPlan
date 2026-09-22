"""施工段划分器（阶段 2｜C 组 C7）—— 依据《资源与工期计算重构方案 v1》。

实施依据（唯一权威）：
  · `docs/资源与工期计算重构方案_v1.md` §1 术语表、§2 裁定 1/2/5/9/11/12、
    §3【1】分段层、§4.1 分段规则 + 验算表、§6 验收标准 #1
  · `docs/修改项总清单_20260921.md` B2 / C7

本模块是**纯逻辑**：不读数据库、不读文件、不发网络请求、不 import
`backend/pipeline/kb.py`，也不 import `beat_configs`。一切数据靠参数注入。

核心口径（勿改）：
  · MSSA = 500 m²（**单一值**，裁定 5；只在这里定义，不外散魔法数）
  · `n = ceil(层面积 ÷ MSSA)`，先满后余切：MSSA, MSSA, …, 余量
  · 余量 < MSSA/3 → 弃用 MSSA：`n' = floor(层面积 ÷ MSSA)`，均匀切成 n' 段
  · 段数**不设上限**（裁定 9）
  · **不考虑结构缝/后浇带**（裁定 1、11）
  · 用户显式分段规则 **优先于一切**（裁定 11、§4.1 层次优先级）
  · 按层划分；**层面积相同的连续层共用一套分段**（裁定 12）
  · 取整一律**向上**（裁定 6）——本模块的段数向上，但段面积保持**精确值不取整**
    （方案 §4.1 只规定段数取整，没有规定段面积取整；见报告 BLOCKERS）

单位：面积一律 `m²`（**严禁写 U+33A1 的方块平米符号**）；数值一律 float。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "MSSA",
    "SEGMENT_IDS",
    "Segment",
    "FloorGroup",
    "USER_RULE_KEY_ALLOWED",
    "compute_segment_areas",
    "compute_segment_areas_ex",
    "segment_floors",
    "suggest_zone_count",
    "suggest_zones",
    "suggest_zones_from_params",
]


# ======================================================================
# 常量（裁定 5：MSSA 单一值；裁定 9：段数不设上限）
# ======================================================================

#: 最大施工段面积 MSSA（m²）。方案 §2 裁定 5 定为单一值 500。
MSSA = 500.0

#: 余量判定阈值 = MSSA / 3 ≈ 166.667 m²（方案 §3【1】、§4.1）。
#: 余量 < 该值 → 弃用 MSSA，段数减一后均匀切。
_MIN_REMAINDER_RATIO = 3.0

#: 段号序列（方案 §4.3 ④ 要求"小数相同按段号 Ⅰ→Ⅱ→Ⅲ"兜底）。
#: 注意：这里用的是罗马数字 U+2160 起，与 `layer_engine._ZONE_NAMES` 同形。
SEGMENT_IDS: Tuple[str, ...] = (
    "Ⅰ", "Ⅱ", "Ⅲ", "Ⅳ", "Ⅴ", "Ⅵ", "Ⅶ", "Ⅷ", "Ⅸ", "Ⅹ",
    "Ⅺ", "Ⅻ", "ⅩⅢ", "ⅩⅣ", "ⅩⅤ", "ⅩⅥ", "ⅩⅦ", "ⅩⅧ", "ⅩⅨ", "ⅩⅩ",
)


# ======================================================================
# 数据结构
# ======================================================================


@dataclass(frozen=True)
class Segment:
    """一个施工段。`batch` 由容量模块按并行段数上限填写，本模块留 0（未分配）。"""

    segment_id: str          # 段号，如 "Ⅰ"
    index: int               # 0 基序号，兼作 §4.3 ④ 的"段号 Ⅰ→Ⅱ→Ⅲ"兜底次序
    area: float              # 段面积（m²，精确值不取整）
    batch: int = 0           # 施工批次（0 = 第一批）；本模块不做批次划分

    def as_dict(self) -> Dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "index": self.index,
            "area": self.area,
            "batch": self.batch,
        }


@dataclass
class FloorGroup:
    """**层面积相同的连续层**共用的那一套分段（裁定 12）。"""

    floors: List[float]              # 参与本组的楼层序号（1 基，升序）
    floor_area: float                # 本组的层面积（m²）
    segments: List[Segment]          # 本组分段结果
    rule: str                        # "user" / "mssa" / "mssa_uniform" / "mssa_below"
    note: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def segment_count(self) -> int:
        return len(self.segments)

    def areas(self) -> List[float]:
        return [s.area for s in self.segments]

    def segment_ids(self) -> List[str]:
        return [s.segment_id for s in self.segments]

    def as_dict(self) -> Dict[str, Any]:
        """`{段号: 面积}` —— 即方案 §5 阶段 2 声明的模块输出形状。"""
        return {s.segment_id: s.area for s in self.segments}

    def to_json(self) -> Dict[str, Any]:
        return {
            "floors": list(self.floors),
            "floor_area": self.floor_area,
            "rule": self.rule,
            "note": self.note,
            "segments": [s.as_dict() for s in self.segments],
            "areas_by_id": self.as_dict(),
        }


# ======================================================================
# 用户显式分段规则（裁定 11：用户规则优先于一切）
# ======================================================================

#: 用户规则字典里允许出现的键（便于父代理接 extractor 时对齐）。
USER_RULE_KEY_ALLOWED = frozenset({
    "areas", "segment_areas", "segments", "mssa", "zones", "note", "floor_area",
})


def _segment_id(i: int) -> str:
    """0 基序号 → 段号。超出台账序列时按 §4.1 序号兜底（段数不设上限）。"""
    if 0 <= i < len(SEGMENT_IDS):
        return SEGMENT_IDS[i]
    return "%d" % (i + 1)


def _positive(value: Any) -> Optional[float]:
    """取正 float；不可解析 / <=0 / NaN / inf → None（不瞎猜）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num) or num <= 0:
        return None
    return num


def _normalize_user_rule(rule: Any) -> Optional[Tuple[List[float], Optional[float], str]]:
    """把各种形状的用户分段规则归一成 `(段面积列表, 覆盖用 MSSA 或 None, 备注)`。

    支持形状：
      · `{"areas": [500, 600]}`              显式段面积（按给定顺序 = Ⅰ→Ⅱ→…）
      · `{"segment_areas": [500, 600]}`
      · `{"Ⅰ": 500, "Ⅱ": 600}`              段号 → 面积
      · `{"segments": [500, 600]}`           段面积列表
      · `{"segments": [{"area": 500}, …]}`   对象列表
      · 裸序列 `[500, 600]`

    解析不出任何有效段面积 → None（**不猜**，退回 MSSA 规则并由调用方留痕）。
    """
    if rule is None:
        return None
    note = ""
    override_mssa: Optional[float] = None

    if isinstance(rule, Mapping):
        override_mssa = _positive(rule.get("mssa"))
        note = str(rule.get("note") or "")
        raw: Any = None
        for key in ("areas", "segment_areas", "segments"):
            if rule.get(key) is not None:
                raw = rule[key]
                break
        if raw is None:
            # 段号 → 面积 的映射（只要值可解析为正数就按段号次序收集）
            pairs: List[Tuple[int, float]] = []
            for key, val in rule.items():
                if key in USER_RULE_KEY_ALLOWED:
                    continue
                num = _positive(val)
                if num is None:
                    continue
                if key in SEGMENT_IDS:
                    pairs.append((SEGMENT_IDS.index(key), num))
                else:
                    pairs.append((len(pairs) + 1000, num))
            if not pairs:
                return None
            pairs.sort(key=lambda kv: kv[0])
            return [a for _, a in pairs], override_mssa, note
    elif isinstance(rule, (list, tuple)):
        raw = rule
    else:
        return None

    if not isinstance(raw, (list, tuple)):
        return None
    areas: List[float] = []
    for item in raw:
        if isinstance(item, Mapping):
            num = _positive(item.get("area"))
        else:
            num = _positive(item)
        if num is None:
            return None
        areas.append(num)
    if not areas:
        return None
    return areas, override_mssa, note


# ======================================================================
# 核心：§4.1 分段规则
# ======================================================================


def compute_segment_areas(
    floor_area: float,
    user_rule: Any = None,
    *,
    mssa: float = MSSA,
    strict: bool = False,
) -> List[float]:
    """按方案 §4.1 把一个层的面积切成段面积列表（升序段号）。

    规则（`mssa` 缺省 500.0）：
      ```
      n = ceil(层面积 ÷ mssa)
      先满后余切段：mssa, mssa, …, 余量
      若 余量 < mssa/3 :
          n' = floor(层面积 ÷ mssa)
          均匀切成 n' 段（每段 = 层面积 ÷ n'）
      ```
    `user_rule` 非空且能解析出段面积 → **用户规则优先**，直接返回该序列。
    层面积 > 0 但 < mssa → 1 段（余量即整层面积本身，不触发弃用）。
    层面积解析不出 / <=0 → 抛 `ValueError`（`strict=True`）或返回 `[]`。
    """
    return compute_segment_areas_ex(
        floor_area, user_rule, mssa=mssa, strict=strict
    )[0]


def compute_segment_areas_ex(
    floor_area: float,
    user_rule: Any = None,
    *,
    mssa: float = MSSA,
    strict: bool = False,
) -> Tuple[List[float], str, str]:
    """同 `compute_segment_areas`，另返回 `(段面积, rule, note)`。

    `rule` ∈ {"user", "mssa", "mssa_uniform", "mssa_below"}，供交付物留痕。
    """
    rule = _normalize_user_rule(user_rule)
    if rule is not None:
        areas, _override, note = rule
        return list(areas), "user", note or "用户显式分段规则（裁定 11：优先于一切）"

    area = _positive(floor_area)
    if area is None:
        if strict:
            raise ValueError("层面积必须是正数，得到 %r" % (floor_area,))
        return [], "invalid", "层面积不可用，不猜"

    cut = _positive(mssa) or MSSA
    if area <= cut:
        # 一层装得下：1 段，面积 = 整层面积（不足 MSSA 也合法，余量判定不适用）
        return [area], "mssa_below", "层面积 ≤ MSSA，1 段"

    n = int(math.ceil(area / cut))
    remainder = area - cut * (n - 1)
    if remainder >= cut / _MIN_REMAINDER_RATIO:
        return [cut] * (n - 1) + [remainder], "mssa", "先满后余：%d × MSSA + 余量" % (n - 1)

    # 余量 < MSSA/3 → 弃用 MSSA，段数减一、均匀切（裁定 2）
    n_prime = int(math.floor(area / cut))
    if n_prime < 1:
        n_prime = 1
    each = area / n_prime
    return [each] * n_prime, "mssa_uniform", "余量 < MSSA/3，弃用 MSSA，%d 段均匀切" % n_prime


def segment_floors(
    floor_areas: Mapping[Any, Any] | Sequence[Any],
    user_rule: Any = None,
    *,
    mssa: float = MSSA,
) -> List[FloorGroup]:
    """逐层分段并按**连续同面积**归组（裁定 12）。

    参数
    ----
    floor_areas : `{楼层号: 面积}` 或 `[面积, …]`（后者楼层号按 1..N 自动编号，
                  顺序即输入顺序，不做排序）。
    user_rule   : 用户显式分段规则（**整组统一**生效；用户规则优先于一切）。

    返回
    ----
    `List[FloorGroup]`，按楼层出现顺序；每个 FloorGroup 是一段**连续同面积**的层。
    面积不可用（<=0 / 非数）的层**不猜**：跳过、**不**并入任何组，并且**打断当前分组**
    （前一组到此为止，后一组重新起组）。理由：无法判断该层与相邻层是否"同面积连续"，
    继续合并会做出未经验证的延续假设（方案未规定此情形，见报告 BLOCKERS）。
    """
    items: List[Tuple[Any, Any]]
    if isinstance(floor_areas, Mapping):
        items = list(floor_areas.items())
    else:
        items = [(i + 1, v) for i, v in enumerate(floor_areas or [])]

    groups: List[FloorGroup] = []
    cur: Optional[FloorGroup] = None
    for floor_no, raw_area in items:
        area = _positive(raw_area)
        if area is None:
            cur = None          # 面积不可用 → 打断分组，不做延续假设
            continue
        if cur is not None and math.isclose(cur.floor_area, area, rel_tol=0.0, abs_tol=1e-9):
            cur.floors.append(floor_no)
            continue
        areas, rule, note = compute_segment_areas_ex(area, user_rule, mssa=mssa)
        segs = [
            Segment(segment_id=_segment_id(i), index=i, area=a)
            for i, a in enumerate(areas)
        ]
        cur = FloorGroup(floors=[floor_no], floor_area=area, segments=segs,
                         rule=rule, note=note)
        groups.append(cur)
    return groups


# ======================================================================
# 同名兼容 `beat_configs.suggest_zones()`（方案 §5 阶段 2 / C7）
# ======================================================================


def suggest_zone_count(floor_area: float, *, mssa: float = MSSA) -> Optional[int]:
    """按 §4.1 规则给出**平面段数**（等价于 `len(compute_segment_areas(...))`）。

    面积不可用 → `None`（沿用既有约定：**不瞎猜**，调用方回落配置 zones）。
    """
    area = _positive(floor_area)
    if area is None:
        return None
    return len(compute_segment_areas(area, mssa=mssa))


def suggest_zones(standard_floor_area: float, *, mssa: float = MSSA) -> Optional[int]:
    """`beat_configs.suggest_zones()` 的**同名兼容替身**——但口径已换成 §4.1（MSSA）。

    与旧实现的差异（**重要**）：旧实现是四档经验值（<800→1 / 800~1500→2 /
    1500~2500→3 / >2500→4），本实现严格按 MSSA=500 的 `ceil(面积÷500)` + 余量判定。
    两者在若干面积上会给出**不同段数**，例如 833 m²：旧=1，新=2。

    签名保持 `(standard_floor_area, *, mssa)`；旧调用 `suggest_zones(area)` 不受影响
    （新增参数是 keyword-only）。
    """
    return suggest_zone_count(standard_floor_area, mssa=mssa)


def suggest_zones_from_params(params: Any, *, mssa: float = MSSA) -> Optional[int]:
    """`beat_configs.suggest_zones_from_params()` 的**同名兼容替身**。

    面积口径沿用旧实现（不得改动，否则 12 栋合计面积会被当成一栋超大平层）：
    `total_area ÷ 栋数 ÷ 层数`；取不到 → `None`。

    栋数键沿用 `beat_configs.building_count(params)` 的口径：
    `buildings` / `building_count` / `building_num` 任一可取正整数（缺省 1）。
    """
    area = standard_floor_area_from_params(params)
    if area is None:
        return None
    return suggest_zone_count(area, mssa=mssa)


def standard_floor_area_from_params(params: Any) -> Optional[float]:
    """`total_area ÷ 栋数 ÷ 层数`；缺任一 / 非法 → `None`（不瞎猜）。"""
    if not isinstance(params, Mapping):
        return None
    total = _positive(params.get("total_area"))
    floors = _positive(params.get("floors"))
    if total is None or floors is None:
        return None
    count = 1.0
    for key in ("buildings", "building_count", "building_num"):
        num = _positive(params.get(key))
        if num is not None:
            count = num
            break
    return total / count / floors
